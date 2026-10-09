from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def install_health_endpoints(app: FastAPI, checks: dict[str, Callable[[], bool]]) -> None:
    """``/health/live`` = process is up; ``/health/ready`` = dependencies reachable."""

    @app.get("/health/live", tags=["health"], include_in_schema=False)
    def live() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"], include_in_schema=False)
    def ready() -> JSONResponse:
        results: dict[str, str] = {}
        for name, check in checks.items():
            try:
                results[name] = "ok" if check() else "down"
            except Exception:  # noqa: BLE001
                results[name] = "down"
        healthy = all(value == "ok" for value in results.values())
        return JSONResponse({"status": "ok" if healthy else "degraded", "checks": results}, status_code=200 if healthy else 503)
