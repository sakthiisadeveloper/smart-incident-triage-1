from __future__ import annotations

import asyncio

from sqlalchemy.orm import sessionmaker

from triage_common.broker import Broker
from triage_common.events import EventEnvelope, EventType, routing_key_for
from triage_common.logging import correlation_id_var

from . import service

ALERT_QUEUE = "incident-service.alert-received"
TRIAGE_QUEUE = "incident-service.triage-completed"


def _run(session_factory: sessionmaker, fn, envelope: EventEnvelope) -> None:
    token = correlation_id_var.set(envelope.correlation_id)
    try:
        with session_factory() as session:
            fn(session, envelope)
    finally:
        correlation_id_var.reset(token)


async def register_consumers(broker: Broker, session_factory: sessionmaker) -> None:
    async def on_alert(envelope: EventEnvelope) -> None:
        await asyncio.to_thread(_run, session_factory, service.handle_alert_received, envelope)

    async def on_triage(envelope: EventEnvelope) -> None:
        await asyncio.to_thread(_run, session_factory, service.handle_triage_completed, envelope)

    await broker.subscribe(ALERT_QUEUE, [routing_key_for(EventType.ALERT_RECEIVED, 1)], on_alert)
    await broker.subscribe(TRIAGE_QUEUE, [routing_key_for(EventType.TRIAGE_COMPLETED, 1)], on_triage)
