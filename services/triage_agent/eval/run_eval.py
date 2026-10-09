"""Offline evaluation of the triage agent against a labelled golden set.

Usage (from repo root):  python services/triage_agent/eval/run_eval.py [--min-accuracy 0.8]

Runs the real agent loop with the configured model client (mock by default;
set AGENT_MODEL_PROVIDER/keys to evaluate a real model) and a fake incident
reader that serves the golden alerts. Exits non-zero below the accuracy gate,
so it can run in CI whenever the prompt or model changes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parents[2] / "libs" / "common"))

from triage_agent.config import Settings  # noqa: E402
from triage_agent.main import build_agent  # noqa: E402
from triage_common.events import IncidentCreatedV1  # noqa: E402


class GoldenReader:
    def __init__(self, cases: dict[int, dict]) -> None:
        self.cases = cases

    async def get_incident(self, incident_id: int) -> dict:
        c = self.cases[incident_id]
        return {
            "id": incident_id,
            "source": c["source"],
            "environment": c["environment"],
            "title": c["title"],
            "severity": c["severity"],
            "status": "OPEN",
            "category": "UNCLASSIFIED",
            "alert_count": 1,
            "tags": c["tags"],
            "recent_alerts": [{"title": c["title"], "message": c["message"], "tags": c["tags"]}],
        }

    async def search_similar(self, query, source, exclude_id, limit):
        return []


async def evaluate(min_accuracy: float) -> int:
    cases = json.loads((HERE / "golden_set.json").read_text(encoding="utf-8"))
    by_id = {i: c for i, c in enumerate(cases, start=1)}
    agent = build_agent(Settings(), incidents=GoldenReader(by_id))

    rows, correct, sev_correct, review_ok, cost, latency = [], 0, 0, 0, 0.0, 0
    for incident_id, case in by_id.items():
        result = await agent.run(
            IncidentCreatedV1(
                incident_id=incident_id,
                fingerprint="eval",
                source=case["source"],
                environment=case["environment"],
                title=case["title"],
                severity=case["severity"],
                status="OPEN",
                first_alert_id=uuid.uuid4(),
                created_at=datetime.now(UTC),
                tags=case["tags"],
            )
        )
        out = result.output
        flagged = result.outcome == "FALLBACK" or bool(result.guardrail_flags)
        # Cases marked expect_review (ambiguous or hostile input) pass when routed to a human.
        ok = flagged if case.get("expect_review") else out.category == case["expected_category"]
        correct += ok
        sev_correct += out.severity == case["expected_severity"]
        review_ok += (not case.get("expect_review")) or flagged
        cost += result.cost_usd
        latency += result.latency_ms
        rows.append(
            (
                case["id"],
                case["expected_category"],
                out.category,
                f"{out.confidence:.2f}",
                result.outcome,
                result.fallback_reason or "",
                ",".join(result.guardrail_flags),
                "ok" if ok else "MISS",
            )
        )

    widths = [4, 28, 28, 5, 10, 15, 28, 4]
    header = ("id", "expected", "predicted", "conf", "outcome", "fallback", "flags", "")
    print("  ".join(h.ljust(w) for h, w in zip(header, widths, strict=True)))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))
    n = len(cases)
    accuracy = correct / n
    print(
        f"\ncategory accuracy {accuracy:.0%} | severity accuracy {sev_correct / n:.0%} | "
        f"risky cases routed to review {review_ok}/{n} | avg latency {latency / n:.0f} ms | total cost ${cost:.4f}"
    )
    return 0 if accuracy >= min_accuracy else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-accuracy", type=float, default=0.8)
    raise SystemExit(asyncio.run(evaluate(parser.parse_args().min_accuracy)))
