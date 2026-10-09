from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from triage_common.broker import Broker
from triage_common.errors import install_error_handlers
from triage_common.health import install_health_endpoints
from triage_common.logging import configure_logging, install_correlation_middleware

from .agent import TriageAgent
from .config import Settings
from .llm import ModelClient, build_model_client
from .tools import IncidentReader, IncidentServiceClient, RunbookStore, ServiceOwnerDirectory, ServiceTokenProvider, ToolRegistry
from .worker import TriageWorker


def build_agent(settings: Settings, model: ModelClient | None = None, incidents: IncidentReader | None = None) -> TriageAgent:
    incidents = incidents or IncidentServiceClient(settings.incident_service_url, ServiceTokenProvider(settings))
    tools = ToolRegistry(incidents, RunbookStore(), ServiceOwnerDirectory())
    return TriageAgent(settings, model or build_model_client(settings), tools)


def create_app(
    settings: Settings | None = None, broker: Broker | None = None, agent: TriageAgent | None = None, run_background: bool = True
) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.service_name, settings.log_level)
    agent = agent or build_agent(settings)
    if broker is None:
        from triage_common.broker_rabbitmq import RabbitMQBroker

        broker = RabbitMQBroker(settings.rabbitmq_url)
    worker = TriageWorker(agent, broker, settings.service_name, settings.max_concurrent_triages)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_background:
            await broker.connect()
            await worker.start()
        yield
        if run_background:
            await broker.close()

    app = FastAPI(
        title="Triage Agent Service",
        version="1.0.0",
        description="Consumes IncidentCreated, runs the AI triage agent, publishes TriageCompleted.",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.worker = worker
    app.state.agent = agent
    install_correlation_middleware(app)
    install_error_handlers(app)
    install_health_endpoints(app, {"broker": broker.is_healthy})

    @app.get("/status", tags=["ops"], summary="Agent configuration, circuit-breaker state and counters")
    def status() -> dict:
        return {
            "model": agent.model.name,
            "provider": settings.model_provider,
            "prompt_version": settings.prompt_version,
            "circuit_breaker": agent.breaker.state,
            "limits": {
                "max_steps": settings.max_steps,
                "triage_timeout_seconds": settings.triage_timeout_seconds,
                "max_total_tokens": settings.max_total_tokens,
                "max_cost_usd_per_incident": settings.max_cost_usd_per_incident,
                "confidence_threshold": settings.confidence_threshold,
            },
            "stats": worker.stats,
        }

    return app
