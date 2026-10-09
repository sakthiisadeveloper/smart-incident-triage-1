from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from triage_common.db import BigIntPK, JSONType, UTCDateTime
from triage_common.events import utcnow
from triage_common.outbox import OutboxMixin


class Base(DeclarativeBase):
    pass


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    last_message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    tags: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    severity: Mapped[str] = mapped_column(String(2), nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False, default="UNCLASSIFIED")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="OPEN")
    assignee: Mapped[str | None] = mapped_column(String(200), nullable=True)
    sla_due_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    alert_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    __mapper_args__ = {"version_id_col": version}
    __table_args__ = (
        CheckConstraint("status IN ('OPEN','ACKNOWLEDGED','IN_PROGRESS','RESOLVED')", name="ck_incidents_status"),
        CheckConstraint("severity IN ('P1','P2','P3','P4')", name="ck_incidents_severity"),
        CheckConstraint("alert_count >= 1", name="ck_incidents_alert_count"),
        # De-duplication rule: at most one non-resolved incident per fingerprint.
        Index(
            "uq_incidents_open_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status <> 'RESOLVED'"),
            sqlite_where=text("status <> 'RESOLVED'"),
        ),
        Index("ix_incidents_status_created", "status", "created_at"),
        Index("ix_incidents_created", "created_at"),
        Index("ix_incidents_severity_created", "severity", "created_at"),
        Index("ix_incidents_source_created", "source", "created_at"),
        Index("ix_incidents_category_created", "category", "created_at"),
    )


class IncidentAlert(Base):
    __tablename__ = "incident_alerts"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    alert_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    severity_hint: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (Index("ix_incident_alerts_incident_received", "incident_id", "received_at"),)


class IncidentEvent(Base):
    """Append-only audit trail (no UPDATE/DELETE grants in production)."""

    __tablename__ = "incident_events"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="RESTRICT"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    data: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("actor_type IN ('SYSTEM','USER','AGENT')", name="ck_incident_events_actor_type"),
        Index("ix_incident_events_incident_created", "incident_id", "created_at"),
        Index("ix_incident_events_created", "created_at"),
    )


class AgentSuggestion(Base):
    __tablename__ = "agent_suggestions"

    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False)
    source_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    fallback_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(2), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    proposed_actions: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    evidence: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    guardrail_flags: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(50), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING_REVIEW")
    decided_by: Mapped[str | None] = mapped_column(String(200), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_agent_suggestions_confidence"),
        CheckConstraint("decision IN ('PENDING_REVIEW','ACCEPTED','REJECTED')", name="ck_agent_suggestions_decision"),
        CheckConstraint("outcome IN ('CLASSIFIED','FALLBACK')", name="ck_agent_suggestions_outcome"),
        CheckConstraint("severity IN ('P1','P2','P3','P4')", name="ck_agent_suggestions_severity"),
        Index("ix_agent_suggestions_incident_created", "incident_id", "created_at"),
        Index("ix_agent_suggestions_decision_created", "decision", "created_at"),
    )


class ProcessedEvent(Base):
    """Idempotent-consumer ledger: one row per (consumer, event_id) handled."""

    __tablename__ = "processed_events"

    consumer: Mapped[str] = mapped_column(String(100), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)

    __table_args__ = (PrimaryKeyConstraint("consumer", "event_id", name="pk_processed_events"),)


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"
