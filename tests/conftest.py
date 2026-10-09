from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from incident_service.config import Settings as IncidentSettings
from incident_service.main import create_app as create_incident_app
from ingestion_service.config import Settings as IngestionSettings
from ingestion_service.main import create_app as create_ingestion_app
from triage_common.auth import issue_token
from triage_common.broker import InMemoryBroker

INGEST_KEY = "test-ingest-key"
JWT_SECRET = "test-operator-secret-at-least-32-bytes!!"
SERVICE_SECRET = "test-service-secret-at-least-32-bytes!!!"


def alert_payload(**overrides) -> dict:
    payload = {
        "source": "payments-batch",
        "environment": "prod",
        "severity_hint": "high",
        "title": "Nightly settlement job failed",
        "message": "TimeoutError: connection to ledger-db exceeded 30s",
        "occurred_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        "tags": {"app": "settlement", "host": "vm-prd-07"},
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def broker() -> InMemoryBroker:
    return InMemoryBroker()


@pytest.fixture
def ingestion_settings() -> IngestionSettings:
    return IngestionSettings(
        database_url="sqlite://",
        api_keys=json.dumps(
            {INGEST_KEY: {"client_id": "monitoring", "sources": ["*"]}, "narrow-key": {"client_id": "narrow", "sources": ["crm-sync"]}}
        ),
        dedup_window_seconds=60,
    )


@pytest.fixture
def ingestion(ingestion_settings, broker):
    app = create_ingestion_app(ingestion_settings, broker=broker, run_background=False)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def incident_settings() -> IncidentSettings:
    return IncidentSettings(
        database_url="sqlite://",
        jwt_secret=JWT_SECRET,
        service_jwt_secret=SERVICE_SECRET,
        enable_dev_token_endpoint=True,
        seed_demo_data=True,
    )


@pytest.fixture
def incident(incident_settings, broker):
    app = create_incident_app(incident_settings, broker=broker, run_background=False)
    with TestClient(app) as client:
        yield client


def operator_headers(*roles: str, subject: str = "alice@example.com") -> dict[str, str]:
    token = issue_token(
        secret=JWT_SECRET,
        subject=subject,
        audience="incident-api",
        issuer="incident-triage-dev",
        ttl=timedelta(hours=1),
        roles=list(roles),
    )
    return {"Authorization": f"Bearer {token}"}


def service_headers(scope: str = "incidents:read") -> dict[str, str]:
    token = issue_token(
        secret=SERVICE_SECRET,
        subject="triage-agent",
        audience="incident-internal",
        issuer="incident-triage-dev",
        ttl=timedelta(minutes=5),
        scope=scope,
    )
    return {"Authorization": f"Bearer {token}"}
