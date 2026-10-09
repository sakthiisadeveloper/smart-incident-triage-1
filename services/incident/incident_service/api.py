from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

from fastapi import APIRouter, Depends, Path, Query, Request, Response
from pydantic import AwareDatetime
from sqlalchemy.orm import Session

from triage_common.auth import issue_token
from triage_common.errors import ProblemError
from triage_common.taxonomy import CATEGORIES

from . import service
from .auth import Principal, require_role, require_service_read
from .config import Settings
from .schemas import (
    AgentQuality,
    AlertOut,
    DecisionIn,
    DevTokenIn,
    DevTokenOut,
    IncidentDetail,
    IncidentPage,
    PageInfo,
    Severity,
    SimilarIncident,
    SortField,
    Status,
    StatusChangeIn,
    SuggestionOut,
    TimelineEntry,
)

public = APIRouter(prefix="/api/v1", tags=["incidents"])
internal = APIRouter(prefix="/internal/v1", tags=["internal (agent tools, read-only)"])
dev = APIRouter(prefix="/dev", tags=["dev only"])

ERRORS = {
    401: {"description": "Missing/invalid token"},
    403: {"description": "Insufficient role"},
    404: {"description": "Not found"},
    409: {"description": "Conflict (invalid transition / already decided / concurrent update)"},
    422: {"description": "Validation error"},
}


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def _detail(session: Session, incident_id: int) -> IncidentDetail:
    incident, timeline, alerts, latest = service.get_detail_parts(session, incident_id)
    summary = service.to_summary(incident)
    return IncidentDetail(
        **summary.model_dump(),
        fingerprint=incident.fingerprint,
        tags=incident.tags,
        last_message=incident.last_message,
        resolved_at=incident.resolved_at,
        resolution_note=incident.resolution_note,
        version=incident.version,
        recent_alerts=[AlertOut.model_validate(a) for a in alerts],
        timeline=[TimelineEntry.model_validate(e) for e in timeline],
        latest_suggestion=SuggestionOut.from_model(latest) if latest else None,
    )


@public.get("/incidents", response_model=IncidentPage, responses=ERRORS, summary="List incidents")
def list_incidents(
    status: list[Status] | None = Query(default=None, description="Repeatable, e.g. ?status=OPEN&status=ACKNOWLEDGED"),
    severity: list[Severity] | None = Query(default=None),
    category: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    created_from: AwareDatetime | None = Query(default=None, description="Inclusive, ISO-8601 with offset"),
    created_to: AwareDatetime | None = Query(default=None, description="Exclusive, ISO-8601 with offset"),
    sort: SortField = Query(default="-created_at"),
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, description="Opaque cursor from page.next_cursor"),
    _: Principal = Depends(require_role("VIEWER")),
    session: Session = Depends(get_session),
) -> IncidentPage:
    if category and any(c not in CATEGORIES for c in category):
        raise ProblemError(422, "Request validation failed", f"category must be one of {', '.join(CATEGORIES)}", "validation-error")
    if created_from and created_to and created_from >= created_to:
        raise ProblemError(422, "Request validation failed", "created_from must be before created_to", "validation-error")
    filters = service.IncidentFilters(status, severity, category, source, created_from, created_to)
    rows, next_cursor = service.list_incidents(session, filters, sort, limit, cursor)
    return IncidentPage(
        items=[service.to_summary(r) for r in rows],
        page=PageInfo(limit=limit, next_cursor=next_cursor, has_more=next_cursor is not None),
    )


@public.get(
    "/incidents/{incident_id}",
    response_model=IncidentDetail,
    responses=ERRORS,
    summary="Incident detail with timeline and latest AI suggestion",
)
def get_incident(
    incident_id: int = Path(ge=1),
    _: Principal = Depends(require_role("VIEWER")),
    session: Session = Depends(get_session),
) -> IncidentDetail:
    return _detail(session, incident_id)


@public.patch(
    "/incidents/{incident_id}/status", response_model=IncidentDetail, responses=ERRORS, summary="Move an incident through its lifecycle"
)
def change_status(
    body: StatusChangeIn,
    incident_id: int = Path(ge=1),
    principal: Principal = Depends(require_role("ENGINEER")),
    session: Session = Depends(get_session),
) -> IncidentDetail:
    service.change_status(session, incident_id, body.status, principal.subject, body.comment, body.assignee)
    return _detail(session, incident_id)


@public.post(
    "/incidents/{incident_id}/suggestions/{suggestion_id}/decision",
    response_model=SuggestionOut,
    responses=ERRORS,
    summary="Accept or reject an AI suggestion (idempotent)",
)
def decide(
    body: DecisionIn,
    response: Response,
    incident_id: int = Path(ge=1),
    suggestion_id: int = Path(ge=1),
    principal: Principal = Depends(require_role("ENGINEER")),
    session: Session = Depends(get_session),
) -> SuggestionOut:
    result = service.decide_suggestion(session, incident_id, suggestion_id, body.decision, principal.subject, body.comment)
    if result.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return SuggestionOut.from_model(result.suggestion)


@public.get(
    "/agent/quality", response_model=AgentQuality, tags=["agent quality"], summary="Accept/override/fallback rates, latency and cost"
)
def quality(_: Principal = Depends(require_role("VIEWER")), session: Session = Depends(get_session)) -> AgentQuality:
    return AgentQuality(**service.agent_quality(session))


@internal.get("/incidents/{incident_id}", summary="Incident and recent related alerts (agent tool)")
def internal_incident(
    incident_id: int = Path(ge=1),
    _: str = Depends(require_service_read),
    session: Session = Depends(get_session),
) -> dict:
    incident, _timeline, alerts, _latest = service.get_detail_parts(session, incident_id)
    return {
        "id": incident.id,
        "source": incident.source,
        "environment": incident.environment,
        "title": incident.title,
        "severity": incident.severity,
        "status": incident.status,
        "category": incident.category,
        "alert_count": incident.alert_count,
        "tags": incident.tags,
        "first_seen_at": incident.first_seen_at.isoformat(),
        "last_seen_at": incident.last_seen_at.isoformat(),
        "recent_alerts": [
            {
                "title": a.title,
                "message": a.message,
                "severity_hint": a.severity_hint,
                "occurred_at": a.occurred_at.isoformat(),
                "tags": a.tags,
            }
            for a in alerts
        ],
    }


@internal.get("/similar-incidents", response_model=list[SimilarIncident], summary="Resolved incidents similar to a text (agent tool)")
def internal_similar(
    q: str = Query(min_length=3, max_length=2000),
    source: str | None = Query(default=None, max_length=100),
    exclude_id: int | None = Query(default=None),
    limit: int = Query(default=5, ge=1, le=10),
    _: str = Depends(require_service_read),
    session: Session = Depends(get_session),
) -> list[SimilarIncident]:
    return service.find_similar(session, q, source, exclude_id, limit)


@dev.post("/token", response_model=DevTokenOut, summary="Mint an operator JWT (local demo only; disabled in production)")
def dev_token(body: DevTokenIn, request: Request) -> DevTokenOut:
    settings: Settings = request.app.state.settings
    ttl = timedelta(hours=8)
    token = issue_token(
        secret=settings.jwt_secret,
        subject=body.subject,
        audience=settings.jwt_audience,
        issuer=settings.jwt_issuer,
        ttl=ttl,
        roles=list(body.roles),
    )
    return DevTokenOut(access_token=token, expires_in=int(ttl.total_seconds()))
