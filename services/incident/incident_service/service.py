"""Incident domain operations. Every state change writes an audit event in the same transaction."""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from triage_common.broker import NonRetryableError
from triage_common.errors import ProblemError
from triage_common.events import (
    AlertReceivedV1,
    EventEnvelope,
    EventType,
    IncidentCreatedV1,
    TriageCompletedV1,
    make_event,
    utcnow,
)
from triage_common.outbox import add_to_outbox
from triage_common.taxonomy import SEVERITIES, severity_rank

from .domain import ALLOWED_TRANSITIONS, can_transition, severity_from_hint, sla_due
from .models import AgentSuggestion, Incident, IncidentAlert, IncidentEvent, OutboxEvent, ProcessedEvent
from .schemas import IncidentSummary, SimilarIncident

logger = logging.getLogger(__name__)

SERVICE_NAME = "incident-service"
ALERT_CONSUMER = "incident-service.alert-received"
TRIAGE_CONSUMER = "incident-service.triage-completed"


# --------------------------------------------------------------------------- consumers


def handle_alert_received(session: Session, envelope: EventEnvelope) -> None:
    try:
        alert = AlertReceivedV1.model_validate(envelope.payload)
    except ValidationError as exc:
        raise NonRetryableError(f"invalid AlertReceived payload: {exc}") from exc

    for attempt in (1, 2):
        try:
            if _already_processed(session, ALERT_CONSUMER, envelope):
                return
            _apply_alert(session, envelope, alert)
            session.commit()
            return
        except IntegrityError:
            # Lost a race (same fingerprint created concurrently, or duplicate delivery).
            # Retry once: the second pass correlates to the winner or sees the event as processed.
            session.rollback()
            if attempt == 2:
                raise


def _already_processed(session: Session, consumer: str, envelope: EventEnvelope) -> bool:
    return session.get(ProcessedEvent, (consumer, envelope.event_id)) is not None


def _apply_alert(session: Session, envelope: EventEnvelope, alert: AlertReceivedV1) -> None:
    session.add(ProcessedEvent(consumer=ALERT_CONSUMER, event_id=envelope.event_id))
    if session.scalar(select(IncidentAlert.id).where(IncidentAlert.alert_id == alert.alert_id)) is not None:
        return

    now = utcnow()
    severity = severity_from_hint(alert.severity_hint, alert.environment)
    incident = session.scalar(
        select(Incident).where(Incident.fingerprint == alert.fingerprint, Incident.status != "RESOLVED").with_for_update()
    )

    if incident is not None:
        incident.alert_count += 1
        incident.last_seen_at = max(incident.last_seen_at, alert.occurred_at)
        incident.last_message = alert.message
        incident.updated_at = now
        data: dict = {"alert_id": str(alert.alert_id), "alert_count": incident.alert_count}
        if severity_rank(severity) < severity_rank(incident.severity):
            data["severity_escalated"] = {"from": incident.severity, "to": severity}
            incident.severity = severity
            incident.sla_due_at = min(incident.sla_due_at, sla_due(incident.created_at, severity))
        session.add(_alert_row(incident.id, alert))
        session.add(_audit(incident.id, "ALERT_CORRELATED", actor="ingestion-service", actor_type="SYSTEM", data=data))
        logger.info("alert correlated to open incident", extra={"fields": {"incident_id": incident.id, "alert_id": str(alert.alert_id)}})
        return

    incident = Incident(
        source=alert.source,
        environment=alert.environment,
        fingerprint=alert.fingerprint,
        title=alert.title,
        last_message=alert.message,
        tags=alert.tags,
        severity=severity,
        category="UNCLASSIFIED",
        status="OPEN",
        sla_due_at=sla_due(now, severity),
        alert_count=1,
        first_seen_at=alert.occurred_at,
        last_seen_at=alert.occurred_at,
        created_at=now,
        updated_at=now,
    )
    session.add(incident)
    session.flush()
    session.add(_alert_row(incident.id, alert))
    session.add(
        _audit(
            incident.id,
            "CREATED",
            actor="ingestion-service",
            actor_type="SYSTEM",
            to_status="OPEN",
            data={"alert_id": str(alert.alert_id), "severity": severity},
        )
    )
    add_to_outbox(
        session,
        OutboxEvent,
        make_event(
            EventType.INCIDENT_CREATED,
            IncidentCreatedV1(
                incident_id=incident.id,
                fingerprint=incident.fingerprint,
                source=incident.source,
                environment=incident.environment,
                title=incident.title,
                severity=incident.severity,
                status=incident.status,
                first_alert_id=alert.alert_id,
                created_at=now,
                tags=incident.tags,
            ),
            producer=SERVICE_NAME,
            correlation_id=envelope.correlation_id,
            causation_id=envelope.event_id,
        ),
    )
    logger.info("incident created", extra={"fields": {"incident_id": incident.id, "severity": severity}})


def _alert_row(incident_id: int, alert: AlertReceivedV1) -> IncidentAlert:
    return IncidentAlert(
        incident_id=incident_id,
        alert_id=alert.alert_id,
        severity_hint=alert.severity_hint,
        title=alert.title,
        message=alert.message,
        tags=alert.tags,
        occurred_at=alert.occurred_at,
        received_at=alert.received_at,
    )


def _audit(
    incident_id: int,
    event_type: str,
    *,
    actor: str,
    actor_type: str,
    from_status: str | None = None,
    to_status: str | None = None,
    comment: str | None = None,
    data: dict | None = None,
) -> IncidentEvent:
    return IncidentEvent(
        incident_id=incident_id,
        event_type=event_type,
        from_status=from_status,
        to_status=to_status,
        actor=actor,
        actor_type=actor_type,
        comment=comment,
        data=data or {},
        created_at=utcnow(),
    )


def handle_triage_completed(session: Session, envelope: EventEnvelope) -> None:
    try:
        result = TriageCompletedV1.model_validate(envelope.payload)
    except ValidationError as exc:
        raise NonRetryableError(f"invalid TriageCompleted payload: {exc}") from exc

    if session.scalar(select(AgentSuggestion.id).where(AgentSuggestion.source_event_id == envelope.event_id)) is not None:
        return
    if session.get(Incident, result.incident_id) is None:
        raise NonRetryableError(f"incident {result.incident_id} does not exist")

    s = result.suggestion
    severity = s.severity if s.severity in SEVERITIES else "P3"
    suggestion = AgentSuggestion(
        incident_id=result.incident_id,
        source_event_id=envelope.event_id,
        outcome=result.outcome,
        fallback_reason=result.fallback_reason,
        category=s.category,
        severity=severity,
        confidence=max(0.0, min(1.0, s.confidence)),
        summary=s.summary,
        proposed_actions=s.proposed_actions,
        evidence=s.evidence,
        guardrail_flags=result.guardrail_flags,
        model=result.model,
        prompt_version=result.prompt_version,
        decision="PENDING_REVIEW",
        steps=result.metrics.steps,
        latency_ms=result.metrics.latency_ms,
        input_tokens=result.metrics.input_tokens,
        output_tokens=result.metrics.output_tokens,
        cost_usd=result.metrics.estimated_cost_usd,
        created_at=utcnow(),
    )
    session.add(suggestion)
    session.flush()
    session.add(
        _audit(
            result.incident_id,
            "SUGGESTION_ADDED",
            actor="triage-agent",
            actor_type="AGENT",
            data={
                "suggestion_id": suggestion.id,
                "outcome": result.outcome,
                "category": s.category,
                "confidence": s.confidence,
                "fallback_reason": result.fallback_reason,
            },
        )
    )
    try:
        session.commit()
    except IntegrityError:
        session.rollback()  # duplicate delivery raced us; the first insert wins
        return
    logger.info(
        "agent suggestion stored",
        extra={"fields": {"incident_id": result.incident_id, "suggestion_id": suggestion.id, "outcome": result.outcome}},
    )


# --------------------------------------------------------------------------- operator commands


def _get_incident_for_update(session: Session, incident_id: int) -> Incident:
    incident = session.get(Incident, incident_id, with_for_update=True)
    if incident is None:
        raise ProblemError(404, "Incident not found", f"Incident {incident_id} does not exist.", "not-found")
    return incident


def change_status(session: Session, incident_id: int, target: str, actor: str, comment: str | None, assignee: str | None) -> Incident:
    incident = _get_incident_for_update(session, incident_id)
    current = incident.status
    if not can_transition(current, target):
        raise ProblemError(
            409,
            "Invalid status transition",
            f"Cannot move incident {incident_id} from {current} to {target}.",
            "invalid-transition",
            current_status=current,
            allowed_transitions=sorted(ALLOWED_TRANSITIONS[current]),
        )
    now = utcnow()
    incident.status = target
    incident.updated_at = now
    if assignee is not None:
        incident.assignee = assignee
    elif target == "ACKNOWLEDGED" and incident.assignee is None:
        incident.assignee = actor
    if target == "RESOLVED":
        incident.resolved_at = now
        incident.resolution_note = comment
    session.add(
        _audit(
            incident.id,
            "STATUS_CHANGED",
            actor=actor,
            actor_type="USER",
            from_status=current,
            to_status=target,
            comment=comment,
            data={"assignee": incident.assignee},
        )
    )
    _commit_or_conflict(session)
    return incident


@dataclass
class DecisionResult:
    suggestion: AgentSuggestion
    replayed: bool


def decide_suggestion(
    session: Session, incident_id: int, suggestion_id: int, decision: str, actor: str, comment: str | None
) -> DecisionResult:
    suggestion = session.scalar(
        select(AgentSuggestion).where(AgentSuggestion.id == suggestion_id, AgentSuggestion.incident_id == incident_id).with_for_update()
    )
    if suggestion is None:
        raise ProblemError(
            404, "Suggestion not found", f"Suggestion {suggestion_id} does not belong to incident {incident_id}.", "not-found"
        )

    target = "ACCEPTED" if decision == "ACCEPT" else "REJECTED"
    if suggestion.decision == target:
        return DecisionResult(suggestion, replayed=True)
    if suggestion.decision != "PENDING_REVIEW":
        raise ProblemError(
            409,
            "Suggestion already decided",
            f"Suggestion {suggestion_id} was already {suggestion.decision} by {suggestion.decided_by}.",
            "already-decided",
            current_decision=suggestion.decision,
        )

    now = utcnow()
    suggestion.decision = target
    suggestion.decided_by = actor
    suggestion.decided_at = now
    suggestion.decision_comment = comment

    incident = _get_incident_for_update(session, incident_id)
    applied: dict = {}
    if target == "ACCEPTED" and suggestion.category != "UNCLASSIFIED":
        if incident.category != suggestion.category:
            applied["category"] = {"from": incident.category, "to": suggestion.category}
            incident.category = suggestion.category
        if incident.status != "RESOLVED" and suggestion.severity != incident.severity:
            applied["severity"] = {"from": incident.severity, "to": suggestion.severity}
            incident.severity = suggestion.severity
            incident.sla_due_at = sla_due(incident.created_at, suggestion.severity)
        if applied:
            incident.updated_at = now

    session.add(
        _audit(
            incident_id,
            "SUGGESTION_DECIDED",
            actor=actor,
            actor_type="USER",
            comment=comment,
            data={"suggestion_id": suggestion_id, "decision": target, "applied": applied},
        )
    )
    _commit_or_conflict(session)
    return DecisionResult(suggestion, replayed=False)


def _commit_or_conflict(session: Session) -> None:
    try:
        session.commit()
    except StaleDataError as exc:
        session.rollback()
        raise ProblemError(
            409, "Concurrent modification", "The incident was modified concurrently; retry the request.", "concurrent-modification"
        ) from exc


# --------------------------------------------------------------------------- queries


def to_summary(incident: Incident) -> IncidentSummary:
    return IncidentSummary.model_validate(
        {
            **{c: getattr(incident, c) for c in IncidentSummary.model_fields if c != "sla_breached"},
            "sla_breached": incident.status != "RESOLVED" and incident.sla_due_at < utcnow(),
        }
    )


_SORT_COLUMNS = {
    "created_at": Incident.created_at,
    "updated_at": Incident.updated_at,
    "sla_due_at": Incident.sla_due_at,
    "severity": Incident.severity,
}


def _encode_cursor(sort: str, value, incident_id: int) -> str:
    raw = {"s": sort, "v": value.isoformat() if isinstance(value, datetime) else value, "id": incident_id}
    return base64.urlsafe_b64encode(json.dumps(raw).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str, sort: str):
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded))
        if raw["s"] != sort:
            raise ValueError("sort mismatch")
        field = sort.lstrip("-")
        value = datetime.fromisoformat(raw["v"]) if field != "severity" else str(raw["v"])
        return value, int(raw["id"])
    except Exception as exc:  # noqa: BLE001
        raise ProblemError(400, "Invalid cursor", "The cursor is malformed or does not match the sort order.", "invalid-cursor") from exc


@dataclass
class IncidentFilters:
    status: list[str] | None = None
    severity: list[str] | None = None
    category: list[str] | None = None
    source: list[str] | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


def list_incidents(
    session: Session, filters: IncidentFilters, sort: str, limit: int, cursor: str | None
) -> tuple[list[Incident], str | None]:
    field = sort.lstrip("-")
    descending = sort.startswith("-")
    column = _SORT_COLUMNS[field]

    query = select(Incident)
    if filters.status:
        query = query.where(Incident.status.in_(filters.status))
    if filters.severity:
        query = query.where(Incident.severity.in_(filters.severity))
    if filters.category:
        query = query.where(Incident.category.in_(filters.category))
    if filters.source:
        query = query.where(Incident.source.in_(filters.source))
    if filters.created_from:
        query = query.where(Incident.created_at >= filters.created_from)
    if filters.created_to:
        query = query.where(Incident.created_at < filters.created_to)

    if cursor:
        value, last_id = _decode_cursor(cursor, sort)
        if descending:
            query = query.where(or_(column < value, and_(column == value, Incident.id < last_id)))
        else:
            query = query.where(or_(column > value, and_(column == value, Incident.id > last_id)))

    order = (column.desc(), Incident.id.desc()) if descending else (column.asc(), Incident.id.asc())
    rows = session.scalars(query.order_by(*order).limit(limit + 1)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    next_cursor = _encode_cursor(sort, getattr(rows[-1], field), rows[-1].id) if has_more and rows else None
    return list(rows), next_cursor


def get_detail_parts(session: Session, incident_id: int):
    incident = session.get(Incident, incident_id)
    if incident is None:
        raise ProblemError(404, "Incident not found", f"Incident {incident_id} does not exist.", "not-found")
    timeline = session.scalars(
        select(IncidentEvent).where(IncidentEvent.incident_id == incident_id).order_by(IncidentEvent.created_at, IncidentEvent.id)
    ).all()
    alerts = session.scalars(
        select(IncidentAlert).where(IncidentAlert.incident_id == incident_id).order_by(IncidentAlert.received_at.desc()).limit(5)
    ).all()
    latest = session.scalar(
        select(AgentSuggestion)
        .where(AgentSuggestion.incident_id == incident_id)
        .order_by(AgentSuggestion.created_at.desc(), AgentSuggestion.id.desc())
        .limit(1)
    )
    return incident, list(timeline), list(alerts), latest


_STOPWORDS = {"the", "and", "for", "with", "from", "this", "that", "was", "has", "error", "failed", "failure", "after", "into", "not"}
_TOKEN = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS}


def find_similar(session: Session, query: str, source: str | None, exclude_id: int | None, limit: int) -> list[SimilarIncident]:
    """Keyword-overlap ranking over recently resolved incidents.

    Production would use PostgreSQL full-text search (tsvector + GIN) or
    pgvector embeddings; the contract of this endpoint stays the same.
    """
    wanted = _tokens(query)
    if not wanted:
        return []
    candidates = session.scalars(
        select(Incident).where(Incident.status == "RESOLVED").order_by(Incident.resolved_at.desc()).limit(500)
    ).all()
    scored: list[SimilarIncident] = []
    for inc in candidates:
        if inc.id == exclude_id:
            continue
        have = _tokens(f"{inc.title} {inc.last_message} {inc.resolution_note or ''}")
        score = len(wanted & have) / len(wanted)
        if source and inc.source == source:
            score += 0.2
        if score >= 0.25:
            scored.append(
                SimilarIncident(
                    id=inc.id,
                    title=inc.title,
                    source=inc.source,
                    category=inc.category,
                    severity=inc.severity,
                    resolution_note=inc.resolution_note,
                    resolved_at=inc.resolved_at,
                    score=round(min(score, 1.0), 3),
                )
            )
    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:limit]


def agent_quality(session: Session) -> dict:
    rows = session.execute(
        select(
            AgentSuggestion.decision,
            AgentSuggestion.outcome,
            func.count(),
            func.avg(AgentSuggestion.latency_ms),
            func.avg(AgentSuggestion.cost_usd),
            func.sum(AgentSuggestion.input_tokens + AgentSuggestion.output_tokens),
        ).group_by(AgentSuggestion.decision, AgentSuggestion.outcome)
    ).all()
    total = accepted = rejected = pending = fallback = tokens = 0
    latency_sum = cost_sum = 0.0
    for decision, outcome, count, avg_latency, avg_cost, token_sum in rows:
        total += count
        latency_sum += float(avg_latency or 0) * count
        cost_sum += float(avg_cost or 0) * count
        tokens += int(token_sum or 0)
        if outcome == "FALLBACK":
            fallback += count
        if decision == "ACCEPTED":
            accepted += count
        elif decision == "REJECTED":
            rejected += count
        else:
            pending += count
    decided = accepted + rejected
    return {
        "total_suggestions": total,
        "decided": decided,
        "accepted": accepted,
        "rejected": rejected,
        "pending": pending,
        "fallback": fallback,
        "accept_rate": round(accepted / decided, 3) if decided else None,
        "override_rate": round(rejected / decided, 3) if decided else None,
        "fallback_rate": round(fallback / total, 3) if total else None,
        "avg_latency_ms": round(latency_sum / total, 1) if total else None,
        "avg_cost_usd": round(cost_sum / total, 6) if total else None,
        "total_tokens": tokens,
    }
