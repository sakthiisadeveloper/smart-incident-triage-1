"""RFC 7807 ``application/problem+json`` error format shared by all services."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging import correlation_id_var

logger = logging.getLogger(__name__)

PROBLEM_BASE = "https://errors.incident-triage.example.com/"


class ProblemError(Exception):
    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        problem_type: str = "about:blank",
        headers: dict[str, str] | None = None,
        **extra: Any,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.problem_type = problem_type
        self.headers = headers
        self.extra = extra


def problem_response(
    request: Request,
    status: int,
    title: str,
    detail: str | None = None,
    problem_type: str = "about:blank",
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": PROBLEM_BASE + problem_type if problem_type != "about:blank" else problem_type,
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "correlation_id": correlation_id_var.get(),
    }
    body.update(extra)
    return JSONResponse(body, status_code=status, media_type="application/problem+json", headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ProblemError)
    async def _problem(request: Request, exc: ProblemError) -> JSONResponse:
        return problem_response(request, exc.status, exc.title, exc.detail, exc.problem_type, exc.headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {
                "field": ".".join(str(part) for part in err.get("loc", ())[1:]) or str(err.get("loc", [""])[0]),
                "location": str(err.get("loc", ["body"])[0]),
                "message": err.get("msg"),
                "code": err.get("type"),
            }
            for err in exc.errors()
        ]
        return problem_response(
            request, 422, "Request validation failed", "One or more fields are invalid.", "validation-error", errors=errors
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return problem_response(request, exc.status_code, str(exc.detail), headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("unhandled error")
        return problem_response(request, 500, "Internal server error", "An unexpected error occurred.", "internal-error")
