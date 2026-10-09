from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import sessionmaker

from triage_common.events import utcnow

from .models import Alert, OutboxEvent

logger = logging.getLogger(__name__)


def purge_expired(session_factory: sessionmaker, retention_days: int) -> int:
    cutoff = utcnow() - timedelta(days=retention_days)
    with session_factory() as session:
        alerts = session.execute(delete(Alert).where(Alert.received_at < cutoff)).rowcount
        session.execute(
            delete(OutboxEvent).where(OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < utcnow() - timedelta(days=1))
        )
        session.commit()
    return alerts or 0


async def run_retention(session_factory: sessionmaker, retention_days: int, stop: asyncio.Event, interval_seconds: float = 3600) -> None:
    while not stop.is_set():
        try:
            purged = await asyncio.to_thread(purge_expired, session_factory, retention_days)
            if purged:
                logger.info("purged expired raw alerts", extra={"fields": {"count": purged}})
        except Exception:  # noqa: BLE001
            logger.exception("retention job failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass
