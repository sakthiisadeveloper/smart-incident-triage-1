from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from triage_common.events import utcnow

Environment = Literal["prod", "preprod", "staging", "uat", "test", "dev"]
SeverityHint = Literal["critical", "high", "medium", "low", "info"]


class AlertIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "source": "payments-batch",
                    "environment": "prod",
                    "severity_hint": "high",
                    "title": "Nightly settlement job failed",
                    "message": "TimeoutError: connection to ledger-db exceeded 30s",
                    "occurred_at": "2026-10-05T23:42:10Z",
                    "tags": {"app": "settlement", "host": "vm-prd-07"},
                }
            ]
        },
    )

    source: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    environment: Environment
    severity_hint: SeverityHint | None = None
    title: str = Field(min_length=1, max_length=300)
    message: str = Field(min_length=1, max_length=20_000)
    occurred_at: AwareDatetime
    tags: dict[str, str] = Field(default_factory=dict, max_length=30)
    fingerprint: str | None = Field(
        default=None, max_length=128, description="Optional caller-supplied fingerprint; overrides the computed one."
    )

    @field_validator("occurred_at")
    @classmethod
    def not_in_future(cls, value):
        if value > utcnow() + timedelta(minutes=5):
            raise ValueError("occurred_at must not be more than 5 minutes in the future")
        return value

    @field_validator("tags")
    @classmethod
    def tag_sizes(cls, value: dict[str, str]):
        for key, val in value.items():
            if not key or len(key) > 64 or len(val) > 256:
                raise ValueError("tag keys must be 1-64 chars and values at most 256 chars")
        return value


class AlertAccepted(BaseModel):
    alert_id: uuid.UUID
    status: Literal["ACCEPTED", "DUPLICATE_SUPPRESSED"]
    fingerprint: str
    duplicate_of: uuid.UUID | None = None
    received_at: AwareDatetime
