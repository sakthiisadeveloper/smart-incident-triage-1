"""Full vertical slice in-process: Ingestion -> broker -> Incident -> broker -> Agent -> broker -> Incident.

Same code paths as ``docker compose up`` except the broker (in-memory with the
same retry/DLQ semantics) and the database (SQLite instead of PostgreSQL).
"""

from __future__ import annotations

import asyncio

import httpx

from conftest import INGEST_KEY, SERVICE_SECRET, alert_payload, operator_headers
from incident_service.consumers import register_consumers
from incident_service.models import OutboxEvent as IncidentOutbox
from ingestion_service.models import OutboxEvent as IngestionOutbox
from triage_agent.config import Settings as AgentSettings
from triage_agent.main import build_agent
from triage_agent.tools import IncidentServiceClient, ServiceTokenProvider
from triage_agent.worker import TriageWorker
from triage_common.outbox import relay_outbox_once


def test_alert_to_incident_to_ai_suggestion_to_human_decision(ingestion, incident, broker):
    agent_settings = AgentSettings(service_jwt_secret=SERVICE_SECRET, incident_service_url="http://incident")

    async def wire_and_flow() -> None:
        reader = IncidentServiceClient(
            "http://incident", ServiceTokenProvider(agent_settings), transport=httpx.ASGITransport(app=incident.app)
        )
        worker = TriageWorker(build_agent(agent_settings, incidents=reader), broker, "triage-agent")
        await register_consumers(broker, incident.app.state.session_factory)
        await worker.start()

        # 1. Source system posts an alert (twice, simulating a client retry).
        headers = {"X-API-Key": INGEST_KEY, "Idempotency-Key": "nightly-2026-10-05"}
        first = ingestion.post("/api/v1/alerts", json=alert_payload(), headers=headers)
        retry = ingestion.post(
            "/api/v1/alerts", json=alert_payload() | {"occurred_at": first.json()["received_at"]}, headers={"X-API-Key": INGEST_KEY}
        )
        assert first.status_code == 202 and retry.json()["status"] == "DUPLICATE_SUPPRESSED"

        # 2. Outbox relays + broker deliveries drive the asynchronous chain.
        await relay_outbox_once(ingestion.app.state.session_factory, IngestionOutbox, broker)
        await broker.drain()  # AlertReceived -> Incident Service
        await relay_outbox_once(incident.app.state.session_factory, IncidentOutbox, broker)
        await broker.drain()  # IncidentCreated -> Agent -> TriageCompleted -> Incident Service

        # 3. Redelivery of IncidentCreated must not create a second suggestion.
        created = next(e for e in broker.published if e.event_type == "IncidentCreated")
        await broker.redeliver(created)
        assert broker.dead_letters == []

    asyncio.run(wire_and_flow())

    engineer = operator_headers("ENGINEER", subject="erin@example.com")
    listing = incident.get("/api/v1/incidents", params={"status": "OPEN"}, headers=engineer).json()
    assert len(listing["items"]) == 1
    incident_id = listing["items"][0]["id"]

    detail = incident.get(f"/api/v1/incidents/{incident_id}", headers=engineer).json()
    suggestion = detail["latest_suggestion"]
    assert suggestion["outcome"] == "CLASSIFIED"
    assert suggestion["category"] == "DATABASE_CONNECTIVITY"
    assert suggestion["evidence"]["runbook"] == "RB-settlement-03"
    assert set(suggestion["evidence"]["similar_incident_ids"]) <= {1, 2, 3, 4, 5, 6, 7}  # seeded resolved history
    assert suggestion["evidence"]["similar_incident_ids"]
    assert suggestion["decision"] == "PENDING_REVIEW"
    assert [e["event_type"] for e in detail["timeline"]] == ["CREATED", "SUGGESTION_ADDED"]

    decision = incident.post(
        f"/api/v1/incidents/{incident_id}/suggestions/{suggestion['id']}/decision",
        json={"decision": "ACCEPT", "comment": "Pool exhaustion confirmed"},
        headers=engineer,
    )
    assert decision.status_code == 200
    assert decision.json()["decision"] == "ACCEPTED"

    final = incident.get(f"/api/v1/incidents/{incident_id}", headers=engineer).json()
    assert final["category"] == "DATABASE_CONNECTIVITY"
    assert final["timeline"][-1]["event_type"] == "SUGGESTION_DECIDED"
    assert final["timeline"][-1]["actor"] == "erin@example.com"
