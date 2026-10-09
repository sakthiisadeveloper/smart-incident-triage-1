"""Structured output contract of the agent. Anything that does not validate is discarded."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from triage_common.taxonomy import CATEGORIES

Category = Literal[CATEGORIES]  # type: ignore[valid-type]
Severity = Literal["P1", "P2", "P3", "P4"]
Action = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    similar_incident_ids: list[int] = Field(default_factory=list, max_length=10)
    runbook: str | None = Field(default=None, pattern=r"^RB-[A-Za-z0-9-]{1,60}$")


class TriageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: Category
    severity: Severity
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str = Field(min_length=1, max_length=600)
    proposed_actions: list[Action] = Field(min_length=1, max_length=6)
    evidence: Evidence = Field(default_factory=Evidence)
