"""Heuristic prompt-injection detector for untrusted alert text.

Detection does not block triage (the alert is still a real alert); it flags the
suggestion and caps confidence so a human always reviews it with a warning.
The primary defences are structural: data delimiting, no write tools, strict
output schema, evidence grounding, and human approval.
"""

from __future__ import annotations

import re

_PATTERNS = [
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|prompts?|rules)",
    r"disregard (all |any )?(the )?(previous|prior|above|system) ",
    r"\byou are now\b",
    r"\bnew (system )?instructions?\b",
    r"\bsystem prompt\b",
    r"</?\s*(system|assistant|untrusted_data)\s*>",
    r"\b(set|change|mark) (the )?(severity|category|priority|status) (to|as)\b",
    r"\b(call|invoke|use) the [a-z_]+ tool\b",
    r"\bjailbreak\b",
    r"\bdo anything now\b",
]
_REGEX = re.compile("|".join(f"(?:{p})" for p in _PATTERNS), re.IGNORECASE)


def looks_like_prompt_injection(text: str) -> bool:
    return bool(_REGEX.search(text or ""))
