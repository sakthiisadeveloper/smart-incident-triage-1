from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict

from pydantic import ValidationError

from triage_common.broker import Broker, NonRetryableError
from triage_common.events import EventEnvelope, EventType, IncidentCreatedV1, make_event, routing_key_for

from .agent import TriageAgent

logger = logging.getLogger(__name__)

QUEUE = "triage-agent.incident-created"
_RESULT_NAMESPACE = uuid.UUID("6f1c2a52-9a43-4d55-8a3e-0c7f6d1b2e10")


class TriageWorker:
    """Consumes IncidentCreated, runs the agent, publishes TriageCompleted.

    The TriageCompleted event id is derived from the triggering event id and
    the prompt version, so a redelivered IncidentCreated yields the same event
    id and the Incident Service stores the suggestion only once. A small LRU
    avoids paying for a second model run on quick redeliveries.
    """

    def __init__(self, agent: TriageAgent, broker: Broker, producer: str, max_concurrency: int = 4, lru_size: int = 2048) -> None:
        self.agent = agent
        self.broker = broker
        self.producer = producer
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._recent: OrderedDict[uuid.UUID, None] = OrderedDict()
        self._lru_size = lru_size
        self.stats = {"processed": 0, "fallback": 0, "skipped_duplicates": 0}

    async def start(self) -> None:
        await self.broker.subscribe(QUEUE, [routing_key_for(EventType.INCIDENT_CREATED, 1)], self.handle)

    async def handle(self, envelope: EventEnvelope) -> None:
        try:
            incident = IncidentCreatedV1.model_validate(envelope.payload)
        except ValidationError as exc:
            raise NonRetryableError(f"invalid IncidentCreated payload: {exc}") from exc

        if envelope.event_id in self._recent:
            self.stats["skipped_duplicates"] += 1
            return

        async with self._semaphore:
            result = await self.agent.run(incident)

        event = make_event(
            EventType.TRIAGE_COMPLETED,
            result.to_event_payload(incident.incident_id),
            producer=self.producer,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.event_id,
            event_id=uuid.uuid5(_RESULT_NAMESPACE, f"{envelope.event_id}:{self.agent.settings.prompt_version}"),
        )
        await self.broker.publish(event)

        self._recent[envelope.event_id] = None
        if len(self._recent) > self._lru_size:
            self._recent.popitem(last=False)
        self.stats["processed"] += 1
        if result.outcome == "FALLBACK":
            self.stats["fallback"] += 1
