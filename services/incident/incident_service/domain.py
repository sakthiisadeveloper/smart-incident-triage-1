"""Lifecycle rules and severity/SLA policy for incidents."""

from __future__ import annotations

from datetime import datetime, timedelta

from triage_common.taxonomy import SEVERITIES

STATUSES = ("OPEN", "ACKNOWLEDGED", "IN_PROGRESS", "RESOLVED")

# RESOLVED is terminal: a recurrence opens a new incident (the unique
# fingerprint index only covers non-resolved incidents).
ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "OPEN": frozenset({"ACKNOWLEDGED", "RESOLVED"}),
    "ACKNOWLEDGED": frozenset({"IN_PROGRESS", "RESOLVED"}),
    "IN_PROGRESS": frozenset({"RESOLVED"}),
    "RESOLVED": frozenset(),
}

DECISIONS = ("PENDING_REVIEW", "ACCEPTED", "REJECTED")

HINT_TO_SEVERITY = {"critical": "P1", "high": "P2", "medium": "P3", "low": "P4", "info": "P4"}

SLA_MINUTES = {"P1": 60, "P2": 4 * 60, "P3": 24 * 60, "P4": 72 * 60}


def severity_from_hint(hint: str | None, environment: str) -> str:
    severity = HINT_TO_SEVERITY.get((hint or "").lower(), "P3")
    if environment != "prod":
        severity = SEVERITIES[min(SEVERITIES.index(severity) + 1, len(SEVERITIES) - 1)]
    return severity


def sla_due(created_at: datetime, severity: str) -> datetime:
    return created_at + timedelta(minutes=SLA_MINUTES[severity])


def can_transition(current: str, target: str) -> bool:
    return target in ALLOWED_TRANSITIONS.get(current, frozenset())
