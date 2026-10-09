"""Transactional outbox.

Services write the business change and the outgoing event in the same DB
transaction; a relay publishes unpublished rows and marks them afterwards.
A crash between publish and mark causes a re-publish (never a loss), which
idempotent consumers absorb.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime

from sqlalchemy import Index, Integer, String, Text, Uuid, select, text
from sqlalchemy.orm import Mapped, Session, declared_attr, mapped_column, sessionmaker

from .broker import Broker
from .db import BigIntPK, JSONType, UTCDateTime
from .events import EventEnvelope, utcnow

logger = logging.getLogger(__name__)


class OutboxMixin:
    id: Mapped[int] = mapped_column(BigIntPK, primary_key=True, autoincrement=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    routing_key: Mapped[str] = mapped_column(String(200), nullable=False)
    envelope: Mapped[dict] = mapped_column(JSONType, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    @declared_attr.directive
    def __table_args__(cls):
        return (
            Index(
                f"ix_{cls.__tablename__}_unpublished",
                "id",
                postgresql_where=text("published_at IS NULL"),
                sqlite_where=text("published_at IS NULL"),
            ),
        )


def add_to_outbox(session: Session, model: type[OutboxMixin], envelope: EventEnvelope) -> None:
    session.add(
        model(
            event_id=envelope.event_id,
            event_type=envelope.event_type,
            routing_key=envelope.routing_key,
            envelope=envelope.model_dump(mode="json"),
        )
    )


async def relay_outbox_once(session_factory: sessionmaker, model: type[OutboxMixin], broker: Broker, batch_size: int = 100) -> int:
    published = 0
    with session_factory() as session:
        rows = session.scalars(
            select(model).where(model.published_at.is_(None)).order_by(model.id).limit(batch_size).with_for_update(skip_locked=True)
        ).all()
        for row in rows:
            try:
                await broker.publish(EventEnvelope.model_validate(row.envelope))
            except Exception as exc:  # noqa: BLE001 - keep order; retry on next tick
                row.attempts += 1
                row.last_error = str(exc)[:1000]
                logger.warning("outbox publish failed", extra={"fields": {"event_id": str(row.event_id), "error": str(exc)}})
                break
            row.published_at = utcnow()
            published += 1
        session.commit()
    return published


async def run_outbox_relay(
    session_factory: sessionmaker,
    model: type[OutboxMixin],
    broker: Broker,
    stop: asyncio.Event,
    interval_seconds: float = 0.5,
) -> None:
    while not stop.is_set():
        try:
            count = await relay_outbox_once(session_factory, model, broker)
        except Exception:  # noqa: BLE001 - relay must survive DB/broker hiccups
            logger.exception("outbox relay iteration failed")
            count = 0
        if count == 0:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
            except TimeoutError:
                pass
