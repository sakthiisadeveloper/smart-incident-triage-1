from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Iterator
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, Request, Response, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from triage_common.errors import ProblemError
from triage_common.events import AlertReceivedV1, EventType, make_event, utcnow
from triage_common.logging import correlation_id_var, new_correlation_id
from triage_common.outbox import add_to_outbox

from .config import ApiClient, Settings
from .fingerprint import compute_fingerprint
from .models import Alert, OutboxEvent
from .schemas import AlertAccepted, AlertIn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1", tags=["alerts"])

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, description="Source-system API key")

PROBLEM_RESPONSES = {
    401: {"description": "Missing or invalid API key"},
    403: {"description": "API key not allowed to send for this source"},
    409: {"description": "Idempotency-Key reused with a different payload"},
    422: {"description": "Payload failed validation (structured problem+json)"},
}


def get_session(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as session:
        yield session


def require_api_key(request: Request, api_key: str | None = Security(api_key_header)) -> ApiClient:
    settings: Settings = request.app.state.settings
    if not api_key:
        raise ProblemError(401, "Missing API key", "Send the source-system key in the X-API-Key header.", "unauthorized")
    client = settings.api_clients_by_key_hash.get(hashlib.sha256(api_key.encode()).hexdigest())
    if client is None:
        raise ProblemError(401, "Invalid API key", None, "unauthorized")
    return client


def _response(alert: Alert) -> AlertAccepted:
    return AlertAccepted(
        alert_id=alert.id,
        status=alert.status,  # type: ignore[arg-type]
        fingerprint=alert.fingerprint,
        duplicate_of=alert.duplicate_of,
        received_at=alert.received_at,
    )


def _find_by_idempotency_key(session: Session, client_id: str, key: str) -> Alert | None:
    return session.scalar(select(Alert).where(Alert.client_id == client_id, Alert.idempotency_key == key))


def _replay(existing: Alert, request_hash: str, response: Response) -> AlertAccepted:
    if existing.request_hash != request_hash:
        raise ProblemError(
            409,
            "Idempotency-Key conflict",
            "This Idempotency-Key was already used with a different payload.",
            "idempotency-conflict",
        )
    response.headers["Idempotent-Replayed"] = "true"
    return _response(existing)


@router.post(
    "/alerts",
    status_code=202,
    response_model=AlertAccepted,
    responses=PROBLEM_RESPONSES,
    summary="Ingest an alert from a source system",
)
def ingest_alert(
    body: AlertIn,
    request: Request,
    response: Response,
    client: ApiClient = Depends(require_api_key),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=200),
    session: Session = Depends(get_session),
) -> AlertAccepted:
    settings: Settings = request.app.state.settings
    if not client.may_send(body.source):
        raise ProblemError(403, "Source not allowed", f"Client '{client.client_id}' may not send alerts for '{body.source}'.", "forbidden")

    payload = body.model_dump(mode="json")
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    if idempotency_key:
        existing = _find_by_idempotency_key(session, client.client_id, idempotency_key)
        if existing is not None:
            return _replay(existing, request_hash, response)

    now = utcnow()
    fingerprint = body.fingerprint or compute_fingerprint(body)
    recent = session.scalar(
        select(Alert)
        .where(
            Alert.fingerprint == fingerprint,
            Alert.status == "ACCEPTED",
            Alert.received_at >= now - timedelta(seconds=settings.dedup_window_seconds),
        )
        .order_by(Alert.received_at.desc())
        .limit(1)
    )

    alert = Alert(
        id=uuid.uuid4(),
        client_id=client.client_id,
        source=body.source,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        fingerprint=fingerprint,
        status="DUPLICATE_SUPPRESSED" if recent else "ACCEPTED",
        duplicate_of=recent.id if recent else None,
        payload=payload,
        received_at=now,
    )
    session.add(alert)

    if recent is None:
        event = make_event(
            EventType.ALERT_RECEIVED,
            AlertReceivedV1(
                alert_id=alert.id,
                source=body.source,
                environment=body.environment,
                severity_hint=body.severity_hint,
                title=body.title,
                message=body.message,
                occurred_at=body.occurred_at,
                received_at=now,
                tags=body.tags,
                fingerprint=fingerprint,
            ),
            producer=settings.service_name,
            correlation_id=correlation_id_var.get() or new_correlation_id(),
        )
        add_to_outbox(session, OutboxEvent, event)

    try:
        session.commit()
    except IntegrityError:
        # Two concurrent requests with the same Idempotency-Key: the loser replays the winner.
        session.rollback()
        existing = _find_by_idempotency_key(session, client.client_id, idempotency_key or "")
        if existing is None:
            raise
        return _replay(existing, request_hash, response)

    logger.info(
        "alert accepted",
        extra={"fields": {"alert_id": str(alert.id), "source": alert.source, "status": alert.status, "fingerprint": fingerprint[:12]}},
    )
    return _response(alert)
