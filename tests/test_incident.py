from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from conftest import operator_headers, service_headers
from incident_service import service
from incident_service.models import AgentSuggestion, Incident, OutboxEvent
from triage_common.events import (
    AlertReceivedV1,
    EventType,
    SuggestionV1,
    TriageCompletedV1,
    TriageMetricsV1,
    make_event,
)
from triage_common.outbox import relay_outbox_once

ENGINEER = operator_headers("ENGINEER")
VIEWER = operator_headers("VIEWER", subject="victor@example.com")


def alert_event(fingerprint: str = "fp-settlement", severity_hint: str = "high", **overrides):
    now = datetime.now(UTC)
    payload = AlertReceivedV1(
        alert_id=uuid.uuid4(),
        source="payments-batch",
        environment="prod",
        severity_hint=severity_hint,
        title="Nightly settlement job failed",
        message="TimeoutError: connection to ledger-db exceeded 30s",
        occurred_at=now,
        received_at=now,
        tags={"app": "settlement"},
        fingerprint=fingerprint,
    ).model_copy(update=overrides)
    return make_event(EventType.ALERT_RECEIVED, payload, producer="test", correlation_id="corr-1")


def triage_event(incident_id: int, category: str = "DATABASE_CONNECTIVITY"):
    payload = TriageCompletedV1(
        incident_id=incident_id,
        outcome="CLASSIFIED",
        suggestion=SuggestionV1(
            category=category,
            severity="P1",
            confidence=0.82,
            summary="ledger-db timeouts",
            proposed_actions=["Check ledger-db connection pool"],
            evidence={"similar_incident_ids": [1], "runbook": "RB-settlement-03"},
        ),
        model="mock",
        prompt_version="triage-v1",
        metrics=TriageMetricsV1(steps=3, latency_ms=120, input_tokens=900, output_tokens=150, estimated_cost_usd=0.0002),
    )
    return make_event(EventType.TRIAGE_COMPLETED, payload, producer="test", correlation_id="corr-1")


def _consume(client, handler, envelope) -> None:
    with client.app.state.session_factory() as session:
        handler(session, envelope)


def _new_incident(client) -> int:
    _consume(client, service.handle_alert_received, alert_event())
    with client.app.state.session_factory() as session:
        return session.scalar(select(Incident.id).where(Incident.fingerprint == "fp-settlement"))


def test_alert_creates_incident_with_audit_and_outbox_event(incident, broker):
    incident_id = _new_incident(incident)

    detail = incident.get(f"/api/v1/incidents/{incident_id}", headers=VIEWER).json()
    assert detail["status"] == "OPEN"
    assert detail["severity"] == "P2"
    assert detail["timeline"][0]["event_type"] == "CREATED"
    assert detail["recent_alerts"][0]["message"].startswith("TimeoutError")

    asyncio.run(relay_outbox_once(incident.app.state.session_factory, OutboxEvent, broker))
    created = [e for e in broker.published if e.event_type == "IncidentCreated"]
    assert len(created) == 1
    assert created[0].payload["incident_id"] == incident_id
    assert created[0].correlation_id == "corr-1"


def test_duplicate_alerts_correlate_to_one_open_incident(incident):
    first = alert_event()
    _consume(incident, service.handle_alert_received, first)
    _consume(incident, service.handle_alert_received, first)  # broker redelivery: no effect
    _consume(incident, service.handle_alert_received, alert_event(severity_hint="critical"))  # same fingerprint, new alert

    with incident.app.state.session_factory() as session:
        incidents = session.scalars(select(Incident).where(Incident.fingerprint == "fp-settlement")).all()
        assert len(incidents) == 1
        assert incidents[0].alert_count == 2
        assert incidents[0].severity == "P1"  # escalated by the more severe repeat
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1  # one IncidentCreated only

    timeline = incident.get(f"/api/v1/incidents/{incidents[0].id}", headers=VIEWER).json()["timeline"]
    assert [e["event_type"] for e in timeline] == ["CREATED", "ALERT_CORRELATED"]
    assert timeline[1]["data"]["severity_escalated"] == {"from": "P2", "to": "P1"}


def test_lifecycle_rejects_invalid_transitions_and_audits_valid_ones(incident):
    incident_id = _new_incident(incident)
    url = f"/api/v1/incidents/{incident_id}/status"

    bad = incident.patch(url, json={"status": "IN_PROGRESS"}, headers=ENGINEER)
    assert bad.status_code == 409
    assert bad.json()["current_status"] == "OPEN"
    assert bad.json()["allowed_transitions"] == ["ACKNOWLEDGED", "RESOLVED"]

    for status in ("ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED"):
        ok = incident.patch(url, json={"status": status, "comment": f"to {status}"}, headers=ENGINEER)
        assert ok.status_code == 200, ok.text

    body = ok.json()
    assert body["assignee"] == "alice@example.com"
    assert body["resolution_note"] == "to RESOLVED"
    changes = [(e["from_status"], e["to_status"], e["actor"]) for e in body["timeline"] if e["event_type"] == "STATUS_CHANGED"]
    assert changes == [
        ("OPEN", "ACKNOWLEDGED", "alice@example.com"),
        ("ACKNOWLEDGED", "IN_PROGRESS", "alice@example.com"),
        ("IN_PROGRESS", "RESOLVED", "alice@example.com"),
    ]
    assert incident.patch(url, json={"status": "OPEN"}, headers=ENGINEER).status_code == 409

    # After resolution the same fingerprint opens a brand-new incident.
    _consume(incident, service.handle_alert_received, alert_event())
    with incident.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Incident).where(Incident.fingerprint == "fp-settlement")) == 2


def test_suggestion_decision_is_idempotent_audited_and_applied(incident):
    incident_id = _new_incident(incident)
    event = triage_event(incident_id)
    _consume(incident, service.handle_triage_completed, event)
    _consume(incident, service.handle_triage_completed, event)  # duplicate delivery

    with incident.app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(AgentSuggestion)) == 1

    detail = incident.get(f"/api/v1/incidents/{incident_id}", headers=VIEWER).json()
    suggestion = detail["latest_suggestion"]
    assert suggestion["decision"] == "PENDING_REVIEW"
    url = f"/api/v1/incidents/{incident_id}/suggestions/{suggestion['id']}/decision"

    first = incident.post(url, json={"decision": "ACCEPT", "comment": "matches"}, headers=ENGINEER)
    again = incident.post(url, json={"decision": "ACCEPT"}, headers=ENGINEER)
    flip = incident.post(url, json={"decision": "REJECT"}, headers=ENGINEER)

    assert first.status_code == again.status_code == 200
    assert again.headers["Idempotent-Replayed"] == "true"
    assert first.json()["decided_by"] == "alice@example.com"
    assert flip.status_code == 409

    detail = incident.get(f"/api/v1/incidents/{incident_id}", headers=VIEWER).json()
    assert detail["category"] == "DATABASE_CONNECTIVITY"
    assert detail["severity"] == "P1"
    decided = [e for e in detail["timeline"] if e["event_type"] == "SUGGESTION_DECIDED"]
    assert len(decided) == 1
    assert decided[0]["data"]["applied"]["category"]["to"] == "DATABASE_CONNECTIVITY"

    quality = incident.get("/api/v1/agent/quality", headers=VIEWER).json()
    assert quality["accepted"] == 1 and quality["accept_rate"] == 1.0


def test_list_filters_sorting_and_cursor_pagination(incident):
    for i in range(5):
        _consume(incident, service.handle_alert_received, alert_event(fingerprint=f"fp-{i}", severity_hint="high" if i % 2 else "low"))

    page1 = incident.get("/api/v1/incidents", params={"status": "OPEN", "limit": 2}, headers=VIEWER).json()
    assert len(page1["items"]) == 2 and page1["page"]["has_more"]
    page2 = incident.get(
        "/api/v1/incidents", params={"status": "OPEN", "limit": 2, "cursor": page1["page"]["next_cursor"]}, headers=VIEWER
    ).json()
    page3 = incident.get(
        "/api/v1/incidents", params={"status": "OPEN", "limit": 2, "cursor": page2["page"]["next_cursor"]}, headers=VIEWER
    ).json()
    ids = [i["id"] for p in (page1, page2, page3) for i in p["items"]]
    assert len(ids) == len(set(ids)) == 5
    assert ids == sorted(ids, reverse=True)
    assert page3["page"]["has_more"] is False

    p2 = incident.get("/api/v1/incidents", params={"severity": "P2", "status": "OPEN"}, headers=VIEWER).json()
    assert {i["severity"] for i in p2["items"]} == {"P2"} and len(p2["items"]) == 2

    resolved = incident.get("/api/v1/incidents", params={"status": "RESOLVED", "source": "payments-batch"}, headers=VIEWER).json()
    assert len(resolved["items"]) == 2  # seeded history

    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert incident.get("/api/v1/incidents", params={"created_from": future}, headers=VIEWER).json()["items"] == []
    assert incident.get("/api/v1/incidents", params={"status": "BOGUS"}, headers=VIEWER).status_code == 422
    assert incident.get("/api/v1/incidents", params={"cursor": "garbage"}, headers=VIEWER).status_code == 400


def test_role_and_service_token_enforcement(incident):
    incident_id = _new_incident(incident)

    assert incident.get("/api/v1/incidents").status_code == 401
    assert incident.patch(f"/api/v1/incidents/{incident_id}/status", json={"status": "ACKNOWLEDGED"}, headers=VIEWER).status_code == 403
    # Operator tokens are not valid on the internal agent API and vice versa.
    assert incident.get(f"/internal/v1/incidents/{incident_id}", headers=ENGINEER).status_code == 401
    assert incident.get("/api/v1/incidents", headers=service_headers()).status_code == 401
    assert incident.get(f"/internal/v1/incidents/{incident_id}", headers=service_headers(scope="other")).status_code == 403

    internal = incident.get(f"/internal/v1/incidents/{incident_id}", headers=service_headers())
    assert internal.status_code == 200
    similar = incident.get(
        "/internal/v1/similar-incidents", params={"q": internal.json()["title"] + " ledger-db timeout"}, headers=service_headers()
    )
    assert similar.status_code == 200
    assert similar.json()[0]["source"] == "payments-batch"
