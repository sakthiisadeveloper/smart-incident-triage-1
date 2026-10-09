from __future__ import annotations

import asyncio

from sqlalchemy import func, select

from conftest import INGEST_KEY, alert_payload
from ingestion_service.fingerprint import normalise
from ingestion_service.models import OutboxEvent
from triage_common.outbox import relay_outbox_once

HEADERS = {"X-API-Key": INGEST_KEY}


def _outbox_count(client) -> int:
    with client.app.state.session_factory() as session:
        return session.scalar(select(func.count()).select_from(OutboxEvent))


def test_valid_alert_is_accepted_and_published_via_outbox(ingestion, broker):
    response = ingestion.post("/api/v1/alerts", json=alert_payload(), headers=HEADERS)

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "ACCEPTED"
    assert body["alert_id"]
    assert response.headers["X-Correlation-ID"]

    published = asyncio.run(relay_outbox_once(ingestion.app.state.session_factory, OutboxEvent, broker))
    assert published == 1
    event = broker.published[0]
    assert event.event_type == "AlertReceived"
    assert event.routing_key == "alert.received.v1"
    assert event.payload["alert_id"] == body["alert_id"]
    assert event.correlation_id == response.headers["X-Correlation-ID"]


def test_idempotency_key_replay_returns_same_alert_without_republishing(ingestion):
    payload = alert_payload()
    headers = {**HEADERS, "Idempotency-Key": "evt-456"}

    first = ingestion.post("/api/v1/alerts", json=payload, headers=headers)
    retry = ingestion.post("/api/v1/alerts", json=payload, headers=headers)

    assert first.status_code == retry.status_code == 202
    assert retry.json()["alert_id"] == first.json()["alert_id"]
    assert retry.headers["Idempotent-Replayed"] == "true"
    assert _outbox_count(ingestion) == 1

    # Same key with a different payload is a client bug -> 409 problem+json.
    conflict = ingestion.post("/api/v1/alerts", json={**payload, "message": "something else"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.headers["content-type"].startswith("application/problem+json")


def test_duplicate_alert_within_window_is_suppressed(ingestion):
    first = ingestion.post("/api/v1/alerts", json=alert_payload(), headers=HEADERS)
    # Same problem, different host and job number -> same fingerprint.
    dup = ingestion.post(
        "/api/v1/alerts",
        json=alert_payload(title="Nightly settlement job failed", tags={"app": "settlement", "host": "vm-prd-08"}),
        headers=HEADERS,
    )

    assert first.json()["status"] == "ACCEPTED"
    assert dup.status_code == 202
    assert dup.json()["status"] == "DUPLICATE_SUPPRESSED"
    assert dup.json()["duplicate_of"] == first.json()["alert_id"]
    assert dup.json()["fingerprint"] == first.json()["fingerprint"]
    assert _outbox_count(ingestion) == 1  # only one AlertReceived event


def test_invalid_payload_returns_structured_422(ingestion):
    bad = alert_payload(environment="moon", title="")
    bad.pop("message")
    response = ingestion.post("/api/v1/alerts", json=bad, headers=HEADERS)

    assert response.status_code == 422
    body = response.json()
    assert response.headers["content-type"].startswith("application/problem+json")
    assert body["type"].endswith("/validation-error")
    fields = {e["field"] for e in body["errors"]}
    assert {"environment", "title", "message"} <= fields
    assert _outbox_count(ingestion) == 0


def test_api_key_is_required_and_scoped_to_sources(ingestion):
    assert ingestion.post("/api/v1/alerts", json=alert_payload()).status_code == 401
    assert ingestion.post("/api/v1/alerts", json=alert_payload(), headers={"X-API-Key": "wrong"}).status_code == 401
    forbidden = ingestion.post("/api/v1/alerts", json=alert_payload(), headers={"X-API-Key": "narrow-key"})
    assert forbidden.status_code == 403


def test_fingerprint_normalisation_masks_volatile_parts():
    assert normalise("Job 4711 failed on 0xDEADBEEF") == normalise("job 4712  failed on 0xcafe")
