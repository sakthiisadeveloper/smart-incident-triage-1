"""Resolved historical incidents so ``search_similar_incidents`` has something to find in the demo."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from triage_common.events import utcnow

from .domain import sla_due
from .models import Incident, IncidentEvent

HISTORY = [
    (
        "payments-batch",
        "settlement",
        "Nightly settlement job failed",
        "TimeoutError: connection to ledger-db exceeded 30s",
        "DATABASE_CONNECTIVITY",
        "P2",
        "ledger-db connection pool exhausted by month-end reporting; raised pool size to 50 and re-ran settlement.",
    ),
    (
        "payments-batch",
        "settlement",
        "Settlement job failed: ledger-db timeout",
        "psycopg OperationalError: timeout expired connecting to ledger-db",
        "DATABASE_CONNECTIVITY",
        "P2",
        "ledger-db primary failed over to replica; job re-run after failover completed.",
    ),
    (
        "orders-api",
        "orders",
        "orders-api pods OOMKilled",
        "Container orders-api exceeded memory limit 512Mi and was OOMKilled",
        "RESOURCE_EXHAUSTION",
        "P2",
        "Memory leak in cache layer after release 4.2; rolled back and raised limit to 1Gi.",
    ),
    (
        "crm-sync",
        "crm-integration",
        "Salesforce sync returning HTTP 503",
        "Upstream Salesforce API returned 503 Service Unavailable for 15 minutes",
        "DEPENDENCY_FAILURE",
        "P3",
        "Salesforce maintenance window; sync retried automatically once upstream recovered.",
    ),
    (
        "reporting-etl",
        "reporting",
        "Disk usage 95% on /data",
        "Filesystem /data on vm-etl-02 is 95% full; no space left soon",
        "RESOURCE_EXHAUSTION",
        "P3",
        "Old staging extracts purged; added a 14-day retention job.",
    ),
    (
        "customer-portal",
        "portal",
        "TLS certificate expiring in 3 days",
        "x509 certificate for portal.example.com expires soon",
        "CERTIFICATE_EXPIRY",
        "P2",
        "Certificate renewed in Key Vault and rotated on the gateway.",
    ),
    (
        "hr-portal",
        "hr",
        "Login failures spike: invalid token signature",
        "401 Unauthorized: JWT signature verification failed for 30% of logins",
        "AUTHENTICATION_AUTHORIZATION",
        "P2",
        "IdP signing key rolled over; refreshed JWKS cache on the gateway.",
    ),
]


def seed_history(session: Session) -> int:
    if session.scalar(select(func.count()).select_from(Incident)):
        return 0
    now = utcnow()
    for index, (source, app, title, message, category, severity, note) in enumerate(HISTORY, start=1):
        created = now - timedelta(days=30 - index * 3)
        incident = Incident(
            source=source,
            environment="prod",
            fingerprint=f"seed-{index}",
            title=title,
            last_message=message,
            tags={"app": app},
            severity=severity,
            category=category,
            status="RESOLVED",
            assignee="seed@example.com",
            sla_due_at=sla_due(created, severity),
            alert_count=1,
            first_seen_at=created,
            last_seen_at=created,
            created_at=created,
            updated_at=created + timedelta(hours=2),
            resolved_at=created + timedelta(hours=2),
            resolution_note=note,
        )
        session.add(incident)
        session.flush()
        session.add(
            IncidentEvent(
                incident_id=incident.id,
                event_type="CREATED",
                to_status="OPEN",
                actor="seed",
                actor_type="SYSTEM",
                data={},
                created_at=created,
            )
        )
        session.add(
            IncidentEvent(
                incident_id=incident.id,
                event_type="STATUS_CHANGED",
                from_status="OPEN",
                to_status="RESOLVED",
                actor="seed@example.com",
                actor_type="USER",
                comment=note,
                data={},
                created_at=created + timedelta(hours=2),
            )
        )
    session.commit()
    return len(HISTORY)
