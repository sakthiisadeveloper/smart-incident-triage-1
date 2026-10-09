"""The triage agent: one short tool-calling loop with guardrails and a safe fallback.

The agent only recommends. Whatever happens (model down, timeout, budget hit,
invalid or low-confidence output) it returns a valid result; on failure that
result is UNCLASSIFIED / "Needs human review".
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from triage_common.events import IncidentCreatedV1, SuggestionV1, TriageCompletedV1, TriageMetricsV1
from triage_common.taxonomy import SEVERITIES

from .config import Settings
from .injection import looks_like_prompt_injection
from .llm import ModelClient, ModelResponse, ModelTimeout, ModelUnavailable, RateLimited, estimate_tokens
from .prompts import system_prompt
from .redaction import redact
from .schemas import Evidence, TriageOutput
from .tools import ToolRegistry, ToolTrace

logger = logging.getLogger(__name__)

FALLBACK_REASONS = {
    "TIMEOUT": "the model did not answer within the time limit",
    "MODEL_UNAVAILABLE": "the model endpoint is unavailable",
    "RATE_LIMITED": "the model endpoint is rate limiting requests",
    "CIRCUIT_OPEN": "the model endpoint is failing repeatedly and calls are paused",
    "TOKEN_BUDGET_EXCEEDED": "the token budget for this incident was reached",
    "COST_BUDGET_EXCEEDED": "the cost budget for this incident was reached",
    "INVALID_OUTPUT": "the model returned output that failed schema validation",
    "NO_FINAL_ANSWER": "the model did not produce a final answer within the step limit",
    "LOW_CONFIDENCE": "the agent's confidence is below the review threshold",
    "AGENT_ERROR": "an unexpected agent error occurred",
}


class CircuitOpen(Exception):
    pass


class BudgetExceeded(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CircuitBreaker:
    """Opens after N consecutive model failures; half-opens after a cool-down."""

    def __init__(self, threshold: int, reset_seconds: float, clock=time.monotonic) -> None:
        self.threshold = threshold
        self.reset_seconds = reset_seconds
        self.clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "open" if self.clock() - self.opened_at < self.reset_seconds else "half-open"

    def allow(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = self.clock()


@dataclass
class TriageResult:
    outcome: str
    output: TriageOutput
    fallback_reason: str | None
    guardrail_flags: list[str]
    model: str
    prompt_version: str
    steps: int
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int = 0
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    def to_event_payload(self, incident_id: int) -> TriageCompletedV1:
        return TriageCompletedV1(
            incident_id=incident_id,
            outcome=self.outcome,  # type: ignore[arg-type]
            fallback_reason=self.fallback_reason,
            suggestion=SuggestionV1(**self.output.model_dump()),
            guardrail_flags=self.guardrail_flags,
            model=self.model,
            prompt_version=self.prompt_version,
            metrics=TriageMetricsV1(
                steps=self.steps,
                latency_ms=self.latency_ms,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                estimated_cost_usd=round(self.cost_usd, 6),
            ),
        )


@dataclass
class _RunState:
    incident_id: int
    incident_severity: str
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    redactions: int = 0
    injection_suspected: bool = False
    flags: list[str] = field(default_factory=list)
    trace: ToolTrace = field(default_factory=ToolTrace)
    draft: TriageOutput | None = None


def _wrap_untrusted(value: Any) -> str:
    # Escape angle brackets so data can never close the delimiter early.
    body = json.dumps(value, default=str).replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<untrusted_data>{body}</untrusted_data>"


def _extract_json(content: str) -> dict[str, Any]:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in model output")
    data = json.loads(text[start : end + 1])
    if not isinstance(data, dict):
        raise ValueError("model output is not a JSON object")
    return data


class TriageAgent:
    def __init__(self, settings: Settings, model: ModelClient, tools: ToolRegistry, breaker: CircuitBreaker | None = None) -> None:
        self.settings = settings
        self.model = model
        self.tools = tools
        self.breaker = breaker or CircuitBreaker(settings.circuit_breaker_failures, settings.circuit_breaker_reset_seconds)

    # ------------------------------------------------------------------ public

    async def run(self, incident: IncidentCreatedV1) -> TriageResult:
        started = time.monotonic()
        severity = incident.severity if incident.severity in SEVERITIES else "P3"
        state = _RunState(incident_id=incident.incident_id, incident_severity=severity)
        try:
            result = await asyncio.wait_for(self._loop(incident, state), timeout=self.settings.triage_timeout_seconds)
        except (TimeoutError, ModelTimeout):
            result = self._fallback(state, "TIMEOUT")
        except CircuitOpen:
            result = self._fallback(state, "CIRCUIT_OPEN")
        except RateLimited:
            result = self._fallback(state, "RATE_LIMITED")
        except ModelUnavailable:
            result = self._fallback(state, "MODEL_UNAVAILABLE")
        except BudgetExceeded as exc:
            result = self._fallback(state, exc.reason)
        except Exception:  # noqa: BLE001 - the agent must always return a safe result
            logger.exception("agent run failed")
            result = self._fallback(state, "AGENT_ERROR")

        result.latency_ms = int((time.monotonic() - started) * 1000)
        logger.info(
            "triage completed",
            extra={
                "fields": {
                    "incident_id": incident.incident_id,
                    "outcome": result.outcome,
                    "fallback_reason": result.fallback_reason,
                    "category": result.output.category,
                    "confidence": result.output.confidence,
                    "steps": result.steps,
                    "latency_ms": result.latency_ms,
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cost_usd": round(result.cost_usd, 6),
                    "redactions": state.redactions,
                    "guardrail_flags": result.guardrail_flags,
                    "model": result.model,
                    "prompt_version": result.prompt_version,
                    "tool_calls": result.tool_calls,
                }
            },
        )
        return result

    # ------------------------------------------------------------------ loop

    async def _loop(self, incident: IncidentCreatedV1, state: _RunState) -> TriageResult:
        metadata, n = redact(
            {
                "incident_id": incident.incident_id,
                "title": incident.title,
                "source": incident.source,
                "environment": incident.environment,
                "severity": incident.severity,
                "tags": incident.tags,
            }
        )
        state.redactions += n
        if looks_like_prompt_injection(incident.title):
            state.injection_suspected = True

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(self.settings.prompt_version, self.settings.max_steps)},
            {"role": "user", "content": f"Triage incident_id={incident.incident_id}. Incident metadata:\n{_wrap_untrusted(metadata)}"},
        ]

        for step in range(1, self.settings.max_steps + 1):
            final_step = step == self.settings.max_steps
            response = await self._call_model(messages, None if final_step else self.tools.schemas, state)
            state.steps = step

            if not response.tool_calls:
                return self._finalize(response.content, state)
            if final_step:
                break

            calls = response.tool_calls[: self.settings.max_tool_calls_per_step]
            if len(response.tool_calls) > len(calls):
                state.flags.append("TOOL_CALLS_TRUNCATED")
            messages.append(
                {
                    "role": "assistant",
                    "content": response.content,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}} for c in calls
                    ],
                }
            )
            for call in calls:
                result = await self.tools.execute(call.name, call.arguments, incident_id=incident.incident_id, trace=state.trace)
                if call.name == "get_incident_details" and "error" not in result:
                    self._scan_for_injection(result, state)
                redacted, n = redact(result)
                state.redactions += n
                messages.append({"role": "tool", "tool_call_id": call.id, "content": _wrap_untrusted(redacted)})

        return self._fallback(state, "NO_FINAL_ANSWER")

    def _scan_for_injection(self, incident: dict[str, Any], state: _RunState) -> None:
        texts = [str(incident.get("title", ""))]
        texts += [f"{a.get('title', '')} {a.get('message', '')}" for a in incident.get("recent_alerts") or []]
        texts += [str(v) for v in (incident.get("tags") or {}).values()]
        if any(looks_like_prompt_injection(t) for t in texts):
            state.injection_suspected = True

    async def _call_model(self, messages: list[dict[str, Any]], tools, state: _RunState) -> ModelResponse:
        s = self.settings
        estimated_in = estimate_tokens(messages)
        if state.input_tokens + state.output_tokens + estimated_in + s.max_output_tokens > s.max_total_tokens:
            raise BudgetExceeded("TOKEN_BUDGET_EXCEEDED")
        if self._cost(state.input_tokens + estimated_in, state.output_tokens + s.max_output_tokens) > s.max_cost_usd_per_incident:
            raise BudgetExceeded("COST_BUDGET_EXCEEDED")

        for attempt in (1, 2):
            if not self.breaker.allow():
                raise CircuitOpen()
            try:
                response = await asyncio.wait_for(
                    self.model.complete(messages, tools, max_output_tokens=s.max_output_tokens), timeout=s.model_call_timeout_seconds
                )
            except (TimeoutError, ModelTimeout) as exc:
                self.breaker.record_failure()
                raise ModelTimeout("model call timed out") from exc
            except RateLimited as exc:
                if attempt == 1 and exc.retry_after <= 2:
                    await asyncio.sleep(exc.retry_after)
                    continue
                raise
            except ModelUnavailable:
                self.breaker.record_failure()
                if attempt == 1:
                    await asyncio.sleep(0.5)
                    continue
                raise
            self.breaker.record_success()
            state.input_tokens += response.input_tokens
            state.output_tokens += response.output_tokens
            return response
        raise ModelUnavailable("model call failed after retry")

    # ------------------------------------------------------------------ output handling

    def _finalize(self, content: str | None, state: _RunState) -> TriageResult:
        try:
            output = TriageOutput.model_validate(_extract_json(content or ""))
        except (ValueError, ValidationError) as exc:
            logger.warning("invalid model output", extra={"fields": {"incident_id": state.incident_id, "error": str(exc)[:500]}})
            return self._fallback(state, "INVALID_OUTPUT")

        confidence = output.confidence
        ids = output.evidence.similar_incident_ids
        grounded_ids = [i for i in ids if i in state.trace.similar_ids]
        if len(grounded_ids) != len(ids):
            state.flags.append("UNGROUNDED_INCIDENT_IDS_REMOVED")
        runbook = output.evidence.runbook
        if runbook and runbook not in state.trace.runbook_ids:
            state.flags.append("HALLUCINATED_RUNBOOK_REMOVED")
            runbook = None
            confidence = max(0.0, confidence - 0.2)
        if state.injection_suspected:
            state.flags.append("POSSIBLE_PROMPT_INJECTION")
            confidence = min(confidence, 0.5)

        output = output.model_copy(
            update={
                "confidence": round(confidence, 3),
                "evidence": Evidence(similar_incident_ids=grounded_ids, runbook=runbook),
            }
        )
        state.draft = output
        if output.category == "UNCLASSIFIED" or output.confidence < self.settings.confidence_threshold:
            return self._fallback(state, "LOW_CONFIDENCE")
        return self._result(state, "CLASSIFIED", output, None)

    def _fallback(self, state: _RunState, reason: str) -> TriageResult:
        draft = state.draft
        summary = f"Needs human review: {FALLBACK_REASONS[reason]}."
        actions = ["Needs human review"]
        if draft is not None:
            summary += f" Agent draft: {draft.category} (confidence {draft.confidence:.2f}) - {draft.summary}"
            actions += [a for a in draft.proposed_actions if a != "Needs human review"][:3]
        else:
            actions.append("Follow the standard manual triage procedure")
        output = TriageOutput(
            category="UNCLASSIFIED",
            severity=state.incident_severity,  # type: ignore[arg-type]
            confidence=draft.confidence if draft else 0.0,
            summary=summary[:600],
            proposed_actions=actions,
            evidence=draft.evidence if draft else Evidence(),
        )
        return self._result(state, "FALLBACK", output, reason)

    def _result(self, state: _RunState, outcome: str, output: TriageOutput, reason: str | None) -> TriageResult:
        return TriageResult(
            outcome=outcome,
            output=output,
            fallback_reason=reason,
            guardrail_flags=list(dict.fromkeys(state.flags)),
            model=self.model.name,
            prompt_version=self.settings.prompt_version,
            steps=state.steps,
            input_tokens=state.input_tokens,
            output_tokens=state.output_tokens,
            cost_usd=self._cost(state.input_tokens, state.output_tokens),
            tool_calls=list(state.trace.calls),
        )

    def _cost(self, input_tokens: int, output_tokens: int) -> float:
        s = self.settings
        return input_tokens / 1000 * s.cost_per_1k_input_tokens + output_tokens / 1000 * s.cost_per_1k_output_tokens
