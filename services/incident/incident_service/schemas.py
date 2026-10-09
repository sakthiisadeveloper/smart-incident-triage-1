from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Status = Literal["OPEN", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED"]
Severity = Literal["P1", "P2", "P3", "P4"]
SortField = Literal["created_at", "-created_at", "updated_at", "-updated_at", "sla_due_at", "-sla_due_at", "severity", "-severity"]


class IncidentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source: str
    environment: str
    title: str
    severity: Severity
    category: str
    status: Status
    assignee: str | None
    sla_due_at: AwareDatetime
    sla_breached: bool
    alert_count: int
    first_seen_at: AwareDatetime
    last_seen_at: AwareDatetime
    created_at: AwareDatetime
    updated_at: AwareDatetime


class PageInfo(BaseModel):
    limit: int
    next_cursor: str | None
    has_more: bool


class IncidentPage(BaseModel):
    items: list[IncidentSummary]
    page: PageInfo


class TimelineEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    from_status: str | None
    to_status: str | None
    actor: str
    actor_type: str
    comment: str | None
    data: dict[str, Any]
    created_at: AwareDatetime


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    alert_id: uuid.UUID
    severity_hint: str | None
    title: str
    message: str
    tags: dict[str, str]
    occurred_at: AwareDatetime
    received_at: AwareDatetime


class SuggestionMetrics(BaseModel):
    steps: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


class SuggestionOut(BaseModel):
    id: int
    incident_id: int
    outcome: Literal["CLASSIFIED", "FALLBACK"]
    fallback_reason: str | None
    category: str
    severity: Severity
    confidence: float
    summary: str
    proposed_actions: list[str]
    evidence: dict[str, Any]
    guardrail_flags: list[str]
    model: str
    prompt_version: str
    decision: Literal["PENDING_REVIEW", "ACCEPTED", "REJECTED"]
    decided_by: str | None
    decided_at: AwareDatetime | None
    decision_comment: str | None
    created_at: AwareDatetime
    metrics: SuggestionMetrics

    @classmethod
    def from_model(cls, s) -> SuggestionOut:
        return cls(
            id=s.id,
            incident_id=s.incident_id,
            outcome=s.outcome,
            fallback_reason=s.fallback_reason,
            category=s.category,
            severity=s.severity,
            confidence=s.confidence,
            summary=s.summary,
            proposed_actions=s.proposed_actions,
            evidence=s.evidence,
            guardrail_flags=s.guardrail_flags,
            model=s.model,
            prompt_version=s.prompt_version,
            decision=s.decision,
            decided_by=s.decided_by,
            decided_at=s.decided_at,
            decision_comment=s.decision_comment,
            created_at=s.created_at,
            metrics=SuggestionMetrics(
                steps=s.steps, latency_ms=s.latency_ms, input_tokens=s.input_tokens, output_tokens=s.output_tokens, cost_usd=s.cost_usd
            ),
        )


class IncidentDetail(IncidentSummary):
    fingerprint: str
    tags: dict[str, str]
    last_message: str
    resolved_at: AwareDatetime | None
    resolution_note: str | None
    version: int
    recent_alerts: list[AlertOut]
    timeline: list[TimelineEntry]
    latest_suggestion: SuggestionOut | None


class StatusChangeIn(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"status": "ACKNOWLEDGED", "comment": "Looking into it"}]})

    status: Status
    comment: str | None = Field(default=None, max_length=2000)
    assignee: str | None = Field(default=None, max_length=200)


class DecisionIn(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_extra={"examples": [{"decision": "ACCEPT", "comment": "Matches what I see"}]})

    decision: Literal["ACCEPT", "REJECT"]
    comment: str | None = Field(default=None, max_length=2000)


class AgentQuality(BaseModel):
    total_suggestions: int
    decided: int
    accepted: int
    rejected: int
    pending: int
    fallback: int
    accept_rate: float | None
    override_rate: float | None
    fallback_rate: float | None
    avg_latency_ms: float | None
    avg_cost_usd: float | None
    total_tokens: int


class SimilarIncident(BaseModel):
    id: int
    title: str
    source: str
    category: str
    severity: str
    resolution_note: str | None
    resolved_at: AwareDatetime | None
    score: float


class DevTokenIn(BaseModel):
    subject: str = Field(default="alice@example.com", max_length=200)
    roles: list[Literal["VIEWER", "ENGINEER", "ADMIN"]] = Field(default_factory=lambda: ["ENGINEER"])


class DevTokenOut(BaseModel):
    access_token: str
    token_type: str = "Bearer"
    expires_in: int
