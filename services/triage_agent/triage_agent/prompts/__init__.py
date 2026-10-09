"""Versioned prompts. A new prompt is a new file; the version is stored with every suggestion."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from triage_common.taxonomy import CATEGORIES

from ..schemas import TriageOutput

_DIR = Path(__file__).parent


@lru_cache
def system_prompt(version: str, max_steps: int) -> str:
    template = (_DIR / f"{version}.md").read_text(encoding="utf-8")
    return template.format(
        max_steps=max_steps,
        categories=", ".join(CATEGORIES),
        output_schema=json.dumps(TriageOutput.model_json_schema(), separators=(",", ":")),
    )
