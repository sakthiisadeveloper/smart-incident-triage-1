"""Operator JWT (roles) and agent service-token validation."""

from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, Request, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from triage_common.auth import decode_token
from triage_common.errors import ProblemError

from .config import Settings

ROLE_RANK = {"VIEWER": 1, "ENGINEER": 2, "ADMIN": 3}

operator_bearer = HTTPBearer(auto_error=False, scheme_name="OperatorJWT", description="Operator JWT with roles claim")
service_bearer = HTTPBearer(auto_error=False, scheme_name="ServiceToken", description="Agent service-to-service token")

_CHALLENGE = {"WWW-Authenticate": "Bearer"}


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]

    @property
    def rank(self) -> int:
        return max((ROLE_RANK[r] for r in self.roles), default=0)


def _decode(token: str, *, secret: str, audience: str, issuer: str) -> dict:
    try:
        return decode_token(token, secret=secret, audience=audience, issuer=issuer)
    except jwt.ExpiredSignatureError as exc:
        raise ProblemError(401, "Token expired", None, "unauthorized", headers=_CHALLENGE) from exc
    except jwt.InvalidTokenError as exc:
        raise ProblemError(401, "Invalid token", str(exc), "unauthorized", headers=_CHALLENGE) from exc


def get_principal(request: Request, creds: HTTPAuthorizationCredentials | None = Security(operator_bearer)) -> Principal:
    if creds is None:
        raise ProblemError(401, "Missing bearer token", None, "unauthorized", headers=_CHALLENGE)
    settings: Settings = request.app.state.settings
    claims = _decode(creds.credentials, secret=settings.jwt_secret, audience=settings.jwt_audience, issuer=settings.jwt_issuer)
    roles = frozenset(r for r in claims.get("roles", []) if r in ROLE_RANK)
    return Principal(subject=claims["sub"], roles=roles)


def require_role(minimum: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.rank < ROLE_RANK[minimum]:
            raise ProblemError(403, "Insufficient role", f"This operation requires role {minimum} or higher.", "forbidden")
        return principal

    return dependency


def require_service_read(request: Request, creds: HTTPAuthorizationCredentials | None = Security(service_bearer)) -> str:
    if creds is None:
        raise ProblemError(401, "Missing service token", None, "unauthorized", headers=_CHALLENGE)
    settings: Settings = request.app.state.settings
    claims = _decode(
        creds.credentials,
        secret=settings.service_jwt_secret,
        audience=settings.service_jwt_audience,
        issuer=settings.service_jwt_issuer,
    )
    if "incidents:read" not in str(claims.get("scope", "")).split():
        raise ProblemError(403, "Missing scope", "Service token requires scope incidents:read.", "forbidden")
    return claims["sub"]
