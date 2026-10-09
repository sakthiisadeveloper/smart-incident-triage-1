from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from triage_common.broker import Broker
from triage_common.db import make_engine
from triage_common.errors import install_error_handlers
from triage_common.health import install_health_endpoints
from triage_common.logging import configure_logging, install_correlation_middleware
from triage_common.outbox import run_outbox_relay

from .api import dev, internal, public
from .config import Settings
from .consumers import register_consumers
from .models import Base, OutboxEvent
from .seed import seed_history

logger = logging.getLogger(__name__)


def init_db(engine: Engine, session_factory: sessionmaker, seed: bool) -> None:
    Base.metadata.create_all(engine)
    if seed:
        with session_factory() as session:
            count = seed_history(session)
            if count:
                logger.info("seeded historical incidents", extra={"fields": {"count": count}})


def create_app(
    settings: Settings | None = None,
    engine: Engine | None = None,
    broker: Broker | None = None,
    run_background: bool = True,
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    engine = engine or make_engine(settings.database_url)
    session_factory = sessionmaker(engine, expire_on_commit=False)
    if broker is None:
        from triage_common.broker_rabbitmq import RabbitMQBroker

        broker = RabbitMQBroker(settings.rabbitmq_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(engine, session_factory, settings.seed_demo_data)
        stop = asyncio.Event()
        tasks: list[asyncio.Task] = []
        if run_background:
            await broker.connect()
            await register_consumers(broker, session_factory)
            tasks.append(asyncio.create_task(run_outbox_relay(session_factory, OutboxEvent, broker, stop, settings.relay_interval_seconds)))
        yield
        stop.set()
        for task in tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if run_background:
            await broker.close()

    app = FastAPI(
        title="Incident Service",
        version="1.0.0",
        description=(
            "System of record for incidents: lifecycle, audit trail, AI suggestions and human decisions.\n\n"
            "Local demo: call `POST /dev/token` to mint an operator JWT, then click **Authorize** (OperatorJWT)."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.broker = broker

    install_correlation_middleware(app)
    install_error_handlers(app)
    app.include_router(public)
    app.include_router(internal)
    if settings.enable_dev_token_endpoint:
        app.include_router(dev)

    def db_ok() -> bool:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True

    install_health_endpoints(app, {"database": db_ok, "broker": broker.is_healthy})
    return app
