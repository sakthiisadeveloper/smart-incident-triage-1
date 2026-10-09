"""End-to-end demo over HTTP (works against docker compose or scripts/run_local.py).

    python scripts/demo.py [--title "[mock:low_confidence] Nightly settlement job failed"]

1. POST an alert to the Ingestion Service (API key + Idempotency-Key)
2. Wait for the Incident Service to create the incident
3. Wait for the AI suggestion to be attached
4. ACCEPT the suggestion and print the audit timeline
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import UTC, datetime

import httpx


def wait_for(fn, what: str, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.5)
    sys.exit(f"timed out waiting for {what}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ingestion", default="http://127.0.0.1:8001")
    parser.add_argument("--incident", default="http://127.0.0.1:8002")
    parser.add_argument("--api-key", default="dev-ingest-key")
    parser.add_argument("--title", default="Nightly settlement job failed")
    parser.add_argument("--message", default="TimeoutError: connection to ledger-db exceeded 30s")
    parser.add_argument("--decision", default="ACCEPT", choices=["ACCEPT", "REJECT"])
    args = parser.parse_args()

    alert = {
        "source": "payments-batch",
        "environment": "prod",
        "severity_hint": "high",
        "title": args.title,
        "message": args.message,
        "occurred_at": datetime.now(UTC).isoformat(),
        "tags": {"app": "settlement", "host": "vm-prd-07", "run": uuid.uuid4().hex[:8]},
        "fingerprint": f"demo-{uuid.uuid4().hex}",
    }
    r = httpx.post(
        f"{args.ingestion}/api/v1/alerts",
        json=alert,
        headers={"X-API-Key": args.api_key, "Idempotency-Key": str(uuid.uuid4())},
    )
    print(f"1. POST /api/v1/alerts -> {r.status_code} {r.json()}")
    r.raise_for_status()
    fingerprint = r.json()["fingerprint"]

    token = httpx.post(f"{args.incident}/dev/token", json={"subject": "demo.engineer@example.com", "roles": ["ENGINEER"]}).json()
    auth = {"Authorization": f"Bearer {token['access_token']}"}

    def find_incident():
        items = httpx.get(f"{args.incident}/api/v1/incidents", params={"status": "OPEN", "limit": 50}, headers=auth).json()["items"]
        for item in items:
            detail = httpx.get(f"{args.incident}/api/v1/incidents/{item['id']}", headers=auth).json()
            if detail["fingerprint"] == fingerprint:
                return detail
        return None

    incident = wait_for(find_incident, "incident")
    print(f"2. Incident #{incident['id']} created: severity={incident['severity']} status={incident['status']}")

    def suggestion():
        detail = httpx.get(f"{args.incident}/api/v1/incidents/{incident['id']}", headers=auth).json()
        return detail["latest_suggestion"]

    s = wait_for(suggestion, "AI suggestion")
    print("3. AI suggestion attached:")
    print(
        json.dumps(
            {
                k: s[k]
                for k in (
                    "id",
                    "outcome",
                    "fallback_reason",
                    "category",
                    "severity",
                    "confidence",
                    "summary",
                    "proposed_actions",
                    "evidence",
                    "guardrail_flags",
                    "metrics",
                )
            },
            indent=2,
        )
    )

    r = httpx.post(
        f"{args.incident}/api/v1/incidents/{incident['id']}/suggestions/{s['id']}/decision",
        json={"decision": args.decision, "comment": "Reviewed in demo"},
        headers=auth,
    )
    print(f"4. POST decision {args.decision} -> {r.status_code}, decision={r.json()['decision']}")

    final = httpx.get(f"{args.incident}/api/v1/incidents/{incident['id']}", headers=auth).json()
    print(f"   Incident now: category={final['category']} severity={final['severity']}")
    print("   Timeline:")
    for e in final["timeline"]:
        print(f"   - {e['created_at']}  {e['event_type']:<18} by {e['actor']} ({e['actor_type']})")


if __name__ == "__main__":
    main()
