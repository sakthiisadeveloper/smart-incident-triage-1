from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from triage_common.db import JSONType, UTCDateTime
from triage_common.outbox import OutboxMixin


class Base(DeclarativeBase):
    pass


class Alert(Base):
    """Raw alert store: idempotency + short de-dup window. Purged after N days."""

    __tablename__ = "alerts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    client_id: Mapped[str] = mapped_column(String(100), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONType, nullable=False)
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_alerts_client_idempotency_key"),
        CheckConstraint("status IN ('ACCEPTED','DUPLICATE_SUPPRESSED')", name="ck_alerts_status"),
        Index("ix_alerts_fingerprint_received", "fingerprint", "received_at"),
        Index("ix_alerts_received", "received_at"),
    )


class OutboxEvent(OutboxMixin, Base):
    __tablename__ = "outbox"
