"""Read-only agent tools.

The agent cannot write anything: tools only GET from the Incident Service
(with a read-scoped service token) or read local static files. Arguments are
validated, and ``get_incident_details`` is scoped to the incident under triage.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from triage_common.auth import issue_token

from .config import Settings

DATA_DIR = Path(__file__).parent / "data"


class IncidentReader(Protocol):
    async def get_incident(self, incident_id: int) -> dict[str, Any]: ...

    async def search_similar(self, query: str, source: str | None, exclude_id: int | None, limit: int) -> list[dict[str, Any]]: ...


class ServiceTokenProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._token: str | None = None
        self._expires = 0.0

    def token(self) -> str:
        if self._token is None or time.time() > self._expires - 60:
            ttl = timedelta(minutes=5)
            self._token = issue_token(
                secret=self.settings.service_jwt_secret,
                subject=self.settings.service_name,
                audience=self.settings.service_jwt_audience,
                issuer=self.settings.service_jwt_issuer,
                ttl=ttl,
                scope="incidents:read",
            )
            self._expires = time.time() + ttl.total_seconds()
        return self._token


class IncidentServiceClient:
    def __init__(
        self, base_url: str, tokens: ServiceTokenProvider, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 5.0
    ) -> None:
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._tokens = tokens

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._tokens.token()}"}

    async def get_incident(self, incident_id: int) -> dict[str, Any]:
        response = await self._client.get(f"/internal/v1/incidents/{incident_id}", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def search_similar(self, query: str, source: str | None, exclude_id: int | None, limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"q": query[:2000], "limit": limit}
        if source:
            params["source"] = source
        if exclude_id:
            params["exclude_id"] = exclude_id
        response = await self._client.get("/internal/v1/similar-incidents", params=params, headers=self._headers())
        response.raise_for_status()
        return response.json()


class RunbookStore:
    def __init__(self, path: Path = DATA_DIR / "runbooks.json") -> None:
        self.runbooks: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))

    def ids(self) -> set[str]:
        return {rb["runbook_id"] for rb in self.runbooks}

    def find(self, app: str | None, error_text: str | None) -> dict[str, Any] | None:
        text = (error_text or "").lower()
        best, best_score = None, 0
        for rb in self.runbooks:
            score = 0
            if app and app.lower() in [a.lower() for a in rb.get("apps", [])]:
                score += 2
            score += sum(1 for p in rb.get("patterns", []) if p.lower() in text)
            if score > best_score:
                best, best_score = rb, score
        return best


class ServiceOwnerDirectory:
    def __init__(self, path: Path = DATA_DIR / "service_owners.json") -> None:
        self.owners: dict[str, dict[str, str]] = json.loads(path.read_text(encoding="utf-8"))

    def lookup(self, app: str) -> dict[str, str] | None:
        return self.owners.get(app.lower())


# --------------------------------------------------------------------------- tool registry


class _GetIncidentArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    incident_id: int = Field(ge=1)


class _SearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=3, max_length=2000)
    source: str | None = Field(default=None, max_length=100)
    limit: int = Field(default=5, ge=1, le=5)


class _RunbookArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: str | None = Field(default=None, max_length=100)
    error_text: str | None = Field(default=None, max_length=2000)


class _OwnerArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    app: str = Field(min_length=1, max_length=100)


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_incident_details",
            "description": "Fetch the incident under triage and its recent alerts.",
            "parameters": _GetIncidentArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_similar_incidents",
            "description": "Find past RESOLVED incidents with similar text.",
            "parameters": _SearchArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_runbook",
            "description": "Return the runbook for an application or error pattern.",
            "parameters": _RunbookArgs.model_json_schema(),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_service_owner",
            "description": "Return owning team and on-call contact for an app.",
            "parameters": _OwnerArgs.model_json_schema(),
        },
    },
]


@dataclass
class ToolTrace:
    """What the tools actually returned during one run; used to ground the final answer."""

    incident: dict[str, Any] | None = None
    similar_ids: set[int] = field(default_factory=set)
    runbook_ids: set[str] = field(default_factory=set)
    calls: list[dict[str, Any]] = field(default_factory=list)


class ToolRegistry:
    def __init__(self, incidents: IncidentReader, runbooks: RunbookStore, owners: ServiceOwnerDirectory) -> None:
        self.incidents = incidents
        self.runbooks = runbooks
        self.owners = owners

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return TOOL_SCHEMAS

    async def execute(self, name: str, arguments: dict[str, Any], *, incident_id: int, trace: ToolTrace) -> dict[str, Any]:
        started = time.monotonic()
        try:
            result = await self._dispatch(name, arguments, incident_id, trace)
            ok = "error" not in result
        except ValidationError as exc:
            result, ok = {"error": f"invalid arguments: {exc.errors(include_url=False)}"}, False
        except httpx.HTTPError as exc:
            result, ok = {"error": f"tool backend unavailable: {type(exc).__name__}"}, False
        trace.calls.append({"tool": name, "ok": ok, "ms": int((time.monotonic() - started) * 1000)})
        return result

    async def _dispatch(self, name: str, arguments: dict[str, Any], incident_id: int, trace: ToolTrace) -> dict[str, Any]:
        if name == "get_incident_details":
            args = _GetIncidentArgs.model_validate(arguments)
            if args.incident_id != incident_id:
                return {"error": "access denied: only the incident under triage can be read"}
            trace.incident = await self.incidents.get_incident(args.incident_id)
            return trace.incident
        if name == "search_similar_incidents":
            args = _SearchArgs.model_validate(arguments)
            found = await self.incidents.search_similar(args.query, args.source, incident_id, args.limit)
            trace.similar_ids.update(int(i["id"]) for i in found)
            return {"incidents": found}
        if name == "get_runbook":
            args = _RunbookArgs.model_validate(arguments)
            runbook = self.runbooks.find(args.app, args.error_text)
            if runbook:
                trace.runbook_ids.add(runbook["runbook_id"])
            return {"runbook": runbook}
        if name == "lookup_service_owner":
            args = _OwnerArgs.model_validate(arguments)
            return {"owner": self.owners.lookup(args.app)}
        return {"error": f"unknown tool '{name}'"}
