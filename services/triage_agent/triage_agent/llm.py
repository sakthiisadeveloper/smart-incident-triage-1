"""Model client interface plus a deterministic mock and an OpenAI / Azure OpenAI client.

Messages use the OpenAI chat format (role/content/tool_calls/tool_call_id) so
any compatible endpoint can be plugged in behind ``ModelClient``.
"""

from __future__ import annotations

import abc
import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings


class ModelError(Exception):
    pass


class ModelUnavailable(ModelError):
    pass


class ModelTimeout(ModelError):
    pass


class RateLimited(ModelError):
    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelResponse:
    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0


def estimate_tokens(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return max(1, len(text) // 4)


class ModelClient(abc.ABC):
    name: str = "unknown"

    @abc.abstractmethod
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None, *, max_output_tokens: int
    ) -> ModelResponse:
        """``tools=None`` means a final answer is required (no further tool calls)."""


# --------------------------------------------------------------------------- mock


_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("CERTIFICATE_EXPIRY", ("certificate", "x509", "ssl", "tls handshake", "cert expir")),
    ("DATABASE_CONNECTIVITY", ("-db", "database", "connection pool", "deadlock", "sql", "postgres", "oracle", "jdbc")),
    ("RESOURCE_EXHAUSTION", ("out of memory", "oomkilled", "oom", "disk", "no space", "cpu", "memory limit", "quota")),
    ("AUTHENTICATION_AUTHORIZATION", ("401", "403", "unauthorized", "forbidden", "login", "credential", "signature", "token expired")),
    ("NETWORK_CONNECTIVITY", ("dns", "unreachable", "connection refused", "network", "socket", "packet loss")),
    ("DEPENDENCY_FAILURE", ("503", "502", "upstream", "third-party", "salesforce", "sap ", "webhook", "service unavailable")),
    ("DATA_QUALITY", ("validation", "schema", "malformed", "null value", "duplicate key", "parse error", "invalid record")),
    ("CONFIGURATION_ERROR", ("config", "missing environment", "not set", "feature flag", "misconfigur")),
    ("BATCH_JOB_FAILURE", ("job failed", "batch", "exit code", "cron", "scheduler")),
    ("APPLICATION_ERROR", ("exception", "stack trace", "nullpointer", "traceback", "panic", "http 500")),
]

_DEFAULT_ACTIONS = {
    "DATABASE_CONNECTIVITY": [
        "Check database health and connection pool usage",
        "Verify network path from app host to database",
        "Re-run the failed job once the database is healthy",
    ],
    "RESOURCE_EXHAUSTION": [
        "Check memory/disk/CPU trends on the affected host",
        "Free or scale the exhausted resource",
        "Look for a recent release that changed resource usage",
    ],
    "AUTHENTICATION_AUTHORIZATION": [
        "Check identity provider status and recent key/secret rotations",
        "Verify the service principal or credentials have not expired",
    ],
    "CERTIFICATE_EXPIRY": ["Confirm certificate expiry date", "Renew and rotate the certificate via the certificate store"],
    "DEPENDENCY_FAILURE": [
        "Check the upstream provider status page",
        "Confirm retries/back-off are working; escalate to vendor if prolonged",
    ],
    "NETWORK_CONNECTIVITY": ["Check DNS resolution and firewall/NSG changes", "Test connectivity from the affected host"],
    "BATCH_JOB_FAILURE": ["Inspect the job log for the first error", "Re-run the job after fixing the root cause"],
    "DATA_QUALITY": ["Identify the offending records", "Quarantine bad input and notify the data owner"],
    "CONFIGURATION_ERROR": ["Diff configuration against the last known good release", "Restore the missing setting"],
    "APPLICATION_ERROR": ["Inspect the stack trace and recent deployments", "Roll back if the error started after a release"],
    "UNCLASSIFIED": ["Needs human review"],
}

_MOCK_DIRECTIVE = re.compile(r"\[mock:([a-z_]+)\]")


def classify_text(text: str) -> tuple[str, int]:
    lowered = text.lower()
    best, best_hits = "UNCLASSIFIED", 0
    for category, keywords in _CATEGORY_RULES:
        hits = sum(1 for k in keywords if k in lowered)
        if hits > best_hits:
            best, best_hits = category, hits
    return best, best_hits


class MockModelClient(ModelClient):
    """Deterministic stand-in for an LLM that exercises the real tool loop.

    Behaviour can be forced globally (``AGENT_MOCK_BEHAVIOUR``) or per alert by
    putting ``[mock:<behaviour>]`` in the alert title, which is handy in demos:
    normal | invalid_json | invalid_schema | low_confidence | hallucinate_runbook |
    outage | rate_limited | timeout | tool_loop
    """

    name = "mock-triage-model-v1"

    def __init__(self, behaviour: str = "normal") -> None:
        self.behaviour = behaviour

    async def complete(self, messages, tools, *, max_output_tokens):
        behaviour = self._behaviour(messages)
        if behaviour == "outage":
            raise ModelUnavailable("mock model outage")
        if behaviour == "rate_limited":
            raise RateLimited(retry_after=120)
        if behaviour == "timeout":
            await asyncio.sleep(3600)

        results = self._tool_results(messages)
        input_tokens = estimate_tokens(messages)

        if behaviour == "tool_loop":
            return self._call("get_runbook", {"app": "anything"}, input_tokens)
        if tools is not None:
            if "get_incident_details" not in results:
                incident_id = self._incident_id(messages)
                return self._call("get_incident_details", {"incident_id": incident_id}, input_tokens)
            if "search_similar_incidents" not in results:
                details = results["get_incident_details"]
                alert = (details.get("recent_alerts") or [{}])[0]
                query = f"{details.get('title', '')} {alert.get('message', '')}"[:500]
                return ModelResponse(
                    content=None,
                    tool_calls=[
                        ToolCall(_id(), "search_similar_incidents", {"query": query, "source": details.get("source")}),
                        ToolCall(_id(), "get_runbook", {"app": (details.get("tags") or {}).get("app"), "error_text": query}),
                    ],
                    input_tokens=input_tokens,
                    output_tokens=60,
                )

        if behaviour == "invalid_json":
            content = "Sure! This looks like a database problem, probably severity P2."
        elif behaviour == "invalid_schema":
            content = json.dumps({"category": "DB_STUFF", "severity": "HIGH", "confidence": 1.7, "summary": "", "proposed_actions": []})
        else:
            content = json.dumps(self._final(results, behaviour))
        return ModelResponse(content=content, input_tokens=input_tokens, output_tokens=estimate_tokens(content))

    def _final(self, results: dict[str, Any], behaviour: str) -> dict[str, Any]:
        details = results.get("get_incident_details") or {}
        alerts = details.get("recent_alerts") or [{}]
        text = f"{details.get('title', '')} {alerts[0].get('message', '')}"
        category, hits = classify_text(text)
        similar = [s["id"] for s in (results.get("search_similar_incidents") or {}).get("incidents", [])][:3]
        runbook = (results.get("get_runbook") or {}).get("runbook") or {}
        runbook_id = runbook.get("runbook_id")
        if behaviour == "hallucinate_runbook":
            runbook_id = "RB-made-up-99"
        confidence = (
            0.3 if category == "UNCLASSIFIED" else min(0.95, 0.5 + 0.12 * hits + (0.08 if similar else 0) + (0.05 if runbook_id else 0))
        )
        if behaviour == "low_confidence":
            confidence = 0.35
        actions = (runbook.get("steps") or _DEFAULT_ACTIONS[category])[:3]
        what = category.replace("_", " ").lower()
        summary = f"{details.get('title', 'Incident')}: likely {what}"
        summary += f"; matches {len(similar)} past incident(s)." if similar else "; no similar past incidents found."
        return {
            "category": category,
            "severity": details.get("severity", "P3"),
            "confidence": round(confidence, 2),
            "summary": summary[:300],
            "proposed_actions": actions,
            "evidence": {"similar_incident_ids": similar, "runbook": runbook_id},
        }

    def _behaviour(self, messages) -> str:
        for message in messages:
            if message.get("role") in ("user", "tool"):
                match = _MOCK_DIRECTIVE.search(str(message.get("content") or ""))
                if match:
                    return match.group(1)
        return self.behaviour

    @staticmethod
    def _tool_results(messages) -> dict[str, Any]:
        names = {}
        for message in messages:
            for call in message.get("tool_calls") or []:
                names[call["id"]] = call["function"]["name"]
        results: dict[str, Any] = {}
        for message in messages:
            if message.get("role") == "tool":
                content = str(message.get("content") or "")
                content = content.removeprefix("<untrusted_data>").removesuffix("</untrusted_data>")
                try:
                    results[names.get(message["tool_call_id"], "?")] = json.loads(content)
                except json.JSONDecodeError:
                    results[names.get(message["tool_call_id"], "?")] = {}
        return results

    @staticmethod
    def _incident_id(messages) -> int:
        for message in messages:
            if message.get("role") == "user":
                match = re.search(r"incident_id[\"']?\s*[:=]\s*(\d+)", str(message.get("content")))
                if match:
                    return int(match.group(1))
        return 0

    @staticmethod
    def _call(name: str, args: dict, input_tokens: int) -> ModelResponse:
        return ModelResponse(content=None, tool_calls=[ToolCall(_id(), name, args)], input_tokens=input_tokens, output_tokens=30)


def _id() -> str:
    return f"call_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------- real endpoint


class OpenAICompatibleClient(ModelClient):
    def __init__(
        self,
        *,
        url: str,
        headers: dict[str, str],
        model: str,
        send_model_field: bool,
        timeout: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.headers = headers
        self.name = model
        self.send_model_field = send_model_field
        self._client = httpx.AsyncClient(timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings) -> OpenAICompatibleClient:
        if settings.model_provider == "azure_openai":
            url = (
                f"{settings.azure_openai_endpoint.rstrip('/')}/openai/deployments/{settings.azure_openai_deployment}"
                f"/chat/completions?api-version={settings.azure_openai_api_version}"
            )
            return cls(
                url=url,
                headers={"api-key": settings.azure_openai_api_key},
                model=f"azure:{settings.azure_openai_deployment}",
                send_model_field=False,
                timeout=settings.model_call_timeout_seconds,
            )
        return cls(
            url=f"{settings.openai_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            model=settings.openai_model,
            send_model_field=True,
            timeout=settings.model_call_timeout_seconds,
        )

    async def complete(self, messages, tools, *, max_output_tokens):
        body: dict[str, Any] = {"messages": messages, "max_tokens": max_output_tokens, "temperature": 0}
        if self.send_model_field:
            body["model"] = self.name
        if tools is not None:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        else:
            body["response_format"] = {"type": "json_object"}
        try:
            response = await self._client.post(self.url, json=body, headers=self.headers)
        except httpx.TimeoutException as exc:
            raise ModelTimeout(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ModelUnavailable(str(exc)) from exc

        if response.status_code == 429:
            raise RateLimited(retry_after=float(response.headers.get("retry-after", "10") or 10))
        if response.status_code >= 400:
            raise ModelUnavailable(f"model endpoint returned HTTP {response.status_code}")

        data = response.json()
        message = data["choices"][0]["message"]
        calls = []
        for raw in message.get("tool_calls") or []:
            try:
                args = json.loads(raw["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw["function"].get("arguments")}
            calls.append(ToolCall(raw["id"], raw["function"]["name"], args if isinstance(args, dict) else {}))
        usage = data.get("usage") or {}
        return ModelResponse(
            content=message.get("content"),
            tool_calls=calls,
            input_tokens=int(usage.get("prompt_tokens") or estimate_tokens(messages)),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


def build_model_client(settings: Settings) -> ModelClient:
    if settings.model_provider == "mock":
        return MockModelClient(settings.mock_behaviour)
    return OpenAICompatibleClient.from_settings(settings)
