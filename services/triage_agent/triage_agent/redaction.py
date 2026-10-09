"""Redact secrets and personal data before any text is sent to a model."""

from __future__ import annotations

import re
from typing import Any

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("PRIVATE_KEY", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("JWT", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("CONNECTION_STRING_CREDENTIALS", re.compile(r"(?<=://)[^/\s:@]+:[^@\s/]+(?=@)")),
    ("BEARER_TOKEN", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}")),
    ("AWS_ACCESS_KEY", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "SECRET",
        re.compile(
            r"(?i)\b(password|passwd|pwd|secret|api[_-]?key|access[_-]?key|token|client[_-]?secret)\b(\s*[=:]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
        ),
    ),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("CARD_NUMBER", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
    ("PHONE", re.compile(r"(?<!\w)\+\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}(?!\w)")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")),
]


def _luhn_ok(candidate: str) -> bool:
    digits = [int(c) for c in candidate if c.isdigit()]
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit = digit * 2 - 9 if digit > 4 else digit * 2
        checksum += digit
    return checksum % 10 == 0


def redact_text(text: str) -> tuple[str, int]:
    count = 0
    for label, pattern in _RULES:
        if label == "SECRET":
            text, n = pattern.subn(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED_SECRET]", text)
        elif label == "CARD_NUMBER":
            hits = 0

            def _card(match: re.Match[str]) -> str:
                nonlocal hits
                if _luhn_ok(match.group(0)):
                    hits += 1
                    return "[REDACTED_CARD_NUMBER]"
                return match.group(0)

            text = pattern.sub(_card, text)
            n = hits
        else:
            text, n = pattern.subn(f"[REDACTED_{label}]", text)
        count += n
    return text, count


def redact(value: Any) -> tuple[Any, int]:
    """Recursively redact strings inside dicts/lists. Returns (value, number_of_redactions)."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        total = 0
        out = {}
        for key, item in value.items():
            out[key], n = redact(item)
            total += n
        return out, total
    if isinstance(value, list):
        total = 0
        items = []
        for item in value:
            red, n = redact(item)
            items.append(red)
            total += n
        return items, total
    return value, 0
