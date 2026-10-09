"""JWT helpers.

The prototype signs tokens with HS256 shared secrets. In Azure the operator
tokens come from Entra ID (RS256, validated against the tenant JWKS at APIM
and again in the service) and the agent uses a managed identity token.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import jwt

from .events import utcnow

ALGORITHM = "HS256"


def issue_token(
    *,
    secret: str,
    subject: str,
    audience: str,
    issuer: str,
    ttl: timedelta,
    roles: list[str] | None = None,
    scope: str | None = None,
) -> str:
    now = utcnow()
    claims: dict[str, Any] = {
        "sub": subject,
        "aud": audience,
        "iss": issuer,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + ttl).timestamp()),
    }
    if roles is not None:
        claims["roles"] = roles
    if scope is not None:
        claims["scope"] = scope
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def decode_token(token: str, *, secret: str, audience: str, issuer: str) -> dict[str, Any]:
    return jwt.decode(
        token,
        secret,
        algorithms=[ALGORITHM],
        audience=audience,
        issuer=issuer,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        leeway=30,
    )
