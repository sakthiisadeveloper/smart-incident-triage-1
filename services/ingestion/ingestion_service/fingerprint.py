"""Alert fingerprinting.

Two alerts describe "the same problem" when source, environment, app tag and
the normalised title match. Volatile parts (numbers, ids, hex, timestamps) are
masked so "job 4711 failed" and "job 4712 failed" collapse together. The host
tag is deliberately excluded: the same failure on several hosts is one incident.
"""

from __future__ import annotations

import hashlib
import re

from .schemas import AlertIn

_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_HEX = re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b")
_NUM = re.compile(r"\d+")
_WS = re.compile(r"\s+")


def normalise(text: str) -> str:
    text = text.lower()
    text = _UUID.sub("<id>", text)
    text = _HEX.sub("<hex>", text)
    text = _NUM.sub("#", text)
    return _WS.sub(" ", text).strip()


def compute_fingerprint(alert: AlertIn) -> str:
    parts = [alert.source, alert.environment, alert.tags.get("app", ""), normalise(alert.title)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()
