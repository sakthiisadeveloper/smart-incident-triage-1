"""Run all three services in ONE process without Docker (in-memory broker + SQLite files).

    python scripts/run_local.py

Ingestion :8001, Incident :8002, Triage Agent :8003 (same ports as docker compose).
Use this for a quick demo on a laptop without Docker; ``docker compose up`` is
the reference setup (RabbitMQ + PostgreSQL).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("libs/common", "services/ingestion", "services/incident", "services/triage_agent"):
    sys.path.insert(0, str(ROOT / sub))

import uvicorn  # noqa: E402

from incident_service.config import Settings as IncidentSettings  # noqa: E402
from incident_service.main import create_app as incident_app  # noqa: E402
from ingestion_service.config import Settings as IngestionSettings  # noqa: E402
from ingestion_service.main import create_app as ingestion_app  # noqa: E402
from triage_agent.config import Settings as AgentSettings  # noqa: E402
from triage_agent.main import create_app as agent_app  # noqa: E402
from triage_common.broker import InMemoryBroker  # noqa: E402

DATA = ROOT / ".local-data"


async def main() -> None:
    DATA.mkdir(exist_ok=True)
    broker = InMemoryBroker()
    apps = [
        (
            ingestion_app(
                IngestionSettings(
                    database_url=f"sqlite:///{DATA / 'ingestion.db'}",
                    api_keys=json.dumps({"dev-ingest-key": {"client_id": "local-demo", "sources": ["*"]}}),
                ),
                broker=broker,
            ),
            8001,
        ),
        (
            incident_app(
                IncidentSettings(database_url=f"sqlite:///{DATA / 'incidents.db'}", enable_dev_token_endpoint=True, seed_demo_data=True),
                broker=broker,
            ),
            8002,
        ),
        (agent_app(AgentSettings(incident_service_url="http://127.0.0.1:8002"), broker=broker), 8003),
    ]
    servers = [uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")) for app, port in apps]
    tasks = [asyncio.create_task(s.serve()) for s in servers]

    while not all(s.started for s in servers):
        await asyncio.sleep(0.1)
    print("Ingestion http://127.0.0.1:8001/docs | Incident http://127.0.0.1:8002/docs | Agent http://127.0.0.1:8003/status", flush=True)

    async def pump() -> None:
        while True:
            await broker.drain()
            await asyncio.sleep(0.2)

    tasks.append(asyncio.create_task(pump()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
