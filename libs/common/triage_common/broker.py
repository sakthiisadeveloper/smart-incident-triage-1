"""Broker abstraction.

Delivery semantics are at-least-once, so every consumer must be idempotent.
Handlers signal "do not retry" by raising ``NonRetryableError``; any other
exception is retried with back-off and finally dead-lettered.
"""

from __future__ import annotations

import abc
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

from .events import EventEnvelope

logger = logging.getLogger(__name__)

Handler = Callable[[EventEnvelope], Awaitable[None]]


class NonRetryableError(Exception):
    """The message can never succeed (bad payload, unknown entity): dead-letter it."""


class Broker(abc.ABC):
    async def connect(self) -> None:  # pragma: no cover - trivial default
        return None

    async def close(self) -> None:  # pragma: no cover - trivial default
        return None

    def is_healthy(self) -> bool:
        return True

    @abc.abstractmethod
    async def publish(self, envelope: EventEnvelope) -> None: ...

    @abc.abstractmethod
    async def subscribe(self, queue: str, routing_keys: Sequence[str], handler: Handler) -> None: ...


@dataclass
class _Subscription:
    queue: str
    routing_keys: set[str]
    handler: Handler


@dataclass
class DeadLetter:
    queue: str
    envelope: EventEnvelope
    error: str


@dataclass
class InMemoryBroker(Broker):
    """Test/dev broker with the same retry and dead-letter semantics as RabbitMQ.

    Messages are queued on ``publish`` and only delivered on ``drain()`` so
    tests control exactly when consumers run.
    """

    max_retries: int = 3
    published: list[EventEnvelope] = field(default_factory=list)
    dead_letters: list[DeadLetter] = field(default_factory=list)
    _pending: list[EventEnvelope] = field(default_factory=list)
    _subs: list[_Subscription] = field(default_factory=list)

    async def publish(self, envelope: EventEnvelope) -> None:
        self.published.append(envelope)
        self._pending.append(envelope)

    async def subscribe(self, queue: str, routing_keys: Sequence[str], handler: Handler) -> None:
        self._subs.append(_Subscription(queue, set(routing_keys), handler))

    async def drain(self) -> int:
        delivered = 0
        while self._pending:
            envelope = self._pending.pop(0)
            for sub in list(self._subs):
                if envelope.routing_key in sub.routing_keys:
                    await self._deliver(sub, envelope)
                    delivered += 1
        return delivered

    async def redeliver(self, envelope: EventEnvelope) -> None:
        """Simulate a duplicate delivery of an already published message."""
        self._pending.append(envelope)
        await self.drain()

    async def _deliver(self, sub: _Subscription, envelope: EventEnvelope) -> None:
        last_error: Exception | None = None
        for _attempt in range(self.max_retries + 1):
            try:
                await sub.handler(envelope)
                return
            except NonRetryableError as exc:
                last_error = exc
                break
            except Exception as exc:  # noqa: BLE001 - mirrors broker retry behaviour
                last_error = exc
        logger.warning("dead-lettering message", extra={"fields": {"queue": sub.queue, "error": repr(last_error)}})
        self.dead_letters.append(DeadLetter(sub.queue, envelope, repr(last_error)))
