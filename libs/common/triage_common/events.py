"""Event envelope and versioned payload contracts.

Every message on the broker is an ``EventEnvelope``. Payload schemas are
versioned independently; the routing key carries the major version so a
breaking change can be rolled out side by side (``alert.received.v2``).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(UTC)


class EventType:
    ALERT_RECEIVED = "AlertReceived"
    INCIDENT_CREATED = "IncidentCreated"
    TRIAGE_COMPLETED = "TriageCompleted"


_ROUTING_BASE = {
    EventType.ALERT_RECEIVED: "alert.received",
    EventType.INCIDENT_CREATED: "incident.created",
    EventType.TRIAGE_COMPLETED: "triage.completed",
}


def routing_key_for(event_type: str, version: int) -> str:
    return f"{_ROUTING_BASE[event_type]}.v{version}"


class EventEnvelope(BaseModel):
    # Tolerant reader: additive fields must not break older consumers.
    model_config = ConfigDict(extra="ignore")

    event_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    event_type: str
    event_version: int = 1
    occurred_at: AwareDatetime = Field(default_factory=utcnow)
    producer: str
    correlation_id: str
    causation_id: uuid.UUID | None = None
    payload: dict[str, Any]

    @property
    def routing_key(self) -> str:
        return routing_key_for(self.event_type, self.event_version)


class AlertReceivedV1(BaseModel):
    alert_id: uuid.UUID
    source: str
    environment: str
    severity_hint: str | None = None
    title: str
    message: str
    occurred_at: AwareDatetime
    received_at: AwareDatetime
    tags: dict[str, str] = Field(default_factory=dict)
    fingerprint: str


class IncidentCreatedV1(BaseModel):
    incident_id: int
    fingerprint: str
    source: str
    environment: str
    title: str
    severity: str
    status: str
    first_alert_id: uuid.UUID
    created_at: AwareDatetime
    tags: dict[str, str] = Field(default_factory=dict)


class SuggestionV1(BaseModel):
    category: str
    severity: str
    confidence: float
    summary: str
    proposed_actions: list[str]
    evidence: dict[str, Any] = Field(default_factory=dict)


class TriageMetricsV1(BaseModel):
    steps: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class TriageCompletedV1(BaseModel):
    incident_id: int
    outcome: Literal["CLASSIFIED", "FALLBACK"]
    fallback_reason: str | None = None
    suggestion: SuggestionV1
    guardrail_flags: list[str] = Field(default_factory=list)
    model: str
    prompt_version: str
    metrics: TriageMetricsV1


def make_event(
    event_type: str,
    payload: BaseModel,
    *,
    producer: str,
    correlation_id: str,
    causation_id: uuid.UUID | None = None,
    event_id: uuid.UUID | None = None,
    version: int = 1,
) -> EventEnvelope:
    return EventEnvelope(
        event_id=event_id or uuid.uuid4(),
        event_type=event_type,
        event_version=version,
        producer=producer,
        correlation_id=correlation_id,
        causation_id=causation_id,
        payload=payload.model_dump(mode="json"),
    )
