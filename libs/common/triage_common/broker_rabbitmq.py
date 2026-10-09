"""RabbitMQ implementation of the broker abstraction.

Topology per consumer queue ``Q`` (all durable):

* ``Q``            bound to the ``triage.events`` topic exchange
* ``Q.retry.N``    one queue per back-off step with ``x-message-ttl``; expired
                   messages dead-letter back into ``Q``
* ``Q.dlq``        terminal dead-letter queue for poison messages

Publishing uses publisher confirms and persistent messages. A message is only
acked after it was handled or safely moved to a retry/DLQ queue.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence

import aio_pika
from aio_pika.abc import AbstractIncomingMessage
from pydantic import ValidationError

from .broker import Broker, Handler, NonRetryableError
from .events import EventEnvelope
from .logging import correlation_id_var

logger = logging.getLogger(__name__)


class RabbitMQBroker(Broker):
    def __init__(
        self,
        url: str,
        exchange: str = "triage.events",
        retry_delays_ms: Sequence[int] = (2_000, 10_000, 60_000),
        prefetch: int = 10,
    ) -> None:
        self.url = url
        self.exchange_name = exchange
        self.retry_delays_ms = tuple(retry_delays_ms)
        self.prefetch = prefetch
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None
        self._exchange: aio_pika.abc.AbstractExchange | None = None

    async def connect(self) -> None:
        delay = 1.0
        while True:
            try:
                self._connection = await aio_pika.connect_robust(self.url)
                break
            except Exception as exc:  # noqa: BLE001 - broker may still be starting
                logger.warning("broker not reachable, retrying", extra={"fields": {"error": str(exc), "retry_in_s": delay}})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.set_qos(prefetch_count=self.prefetch)
        self._exchange = await self._channel.declare_exchange(self.exchange_name, aio_pika.ExchangeType.TOPIC, durable=True)
        logger.info("connected to broker", extra={"fields": {"exchange": self.exchange_name}})

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    def is_healthy(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def publish(self, envelope: EventEnvelope) -> None:
        assert self._exchange is not None, "broker not connected"
        message = aio_pika.Message(
            body=envelope.model_dump_json().encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(envelope.event_id),
            correlation_id=envelope.correlation_id,
            type=envelope.event_type,
            headers={"event_version": envelope.event_version},
        )
        # mandatory + publisher confirms: an unroutable message raises, so the outbox row stays unpublished and is retried.
        await self._exchange.publish(message, routing_key=envelope.routing_key, mandatory=True)

    async def subscribe(self, queue: str, routing_keys: Sequence[str], handler: Handler) -> None:
        assert self._channel is not None and self._exchange is not None, "broker not connected"
        channel = self._channel
        main_queue = await channel.declare_queue(queue, durable=True)
        for key in routing_keys:
            await main_queue.bind(self._exchange, routing_key=key)
        for step, delay in enumerate(self.retry_delays_ms, start=1):
            await channel.declare_queue(
                f"{queue}.retry.{step}",
                durable=True,
                arguments={
                    "x-message-ttl": delay,
                    "x-dead-letter-exchange": "",
                    "x-dead-letter-routing-key": queue,
                },
            )
        await channel.declare_queue(f"{queue}.dlq", durable=True)

        async def on_message(message: AbstractIncomingMessage) -> None:
            retries = int((message.headers or {}).get("x-retry-count", 0))
            try:
                try:
                    envelope = EventEnvelope.model_validate_json(message.body)
                except ValidationError as exc:
                    await self._forward(f"{queue}.dlq", message, retries, f"invalid envelope: {exc}")
                    await message.ack()
                    return

                token = correlation_id_var.set(envelope.correlation_id)
                try:
                    await handler(envelope)
                except NonRetryableError as exc:
                    await self._forward(f"{queue}.dlq", message, retries, repr(exc))
                except Exception as exc:  # noqa: BLE001 - schedule a retry
                    if retries < len(self.retry_delays_ms):
                        logger.warning(
                            "handler failed, scheduling retry",
                            extra={"fields": {"queue": queue, "retry": retries + 1, "error": repr(exc)}},
                        )
                        await self._forward(f"{queue}.retry.{retries + 1}", message, retries + 1, repr(exc))
                    else:
                        logger.error("retries exhausted, dead-lettering", extra={"fields": {"queue": queue}})
                        await self._forward(f"{queue}.dlq", message, retries, repr(exc))
                finally:
                    correlation_id_var.reset(token)
                await message.ack()
            except Exception:  # noqa: BLE001 - could not even park the message: let the broker redeliver
                logger.exception("failed to process or park message; requeueing")
                await message.nack(requeue=True)

        await main_queue.consume(on_message)
        logger.info("subscribed", extra={"fields": {"queue": queue, "routing_keys": list(routing_keys)}})

    async def _forward(self, target_queue: str, message: AbstractIncomingMessage, retries: int, error: str) -> None:
        assert self._channel is not None
        headers = dict(message.headers or {})
        headers["x-retry-count"] = retries
        headers["x-last-error"] = error[:1000]
        await self._channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                content_type=message.content_type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message.message_id,
                correlation_id=message.correlation_id,
                type=message.type,
                headers=headers,
            ),
            routing_key=target_queue,
        )
