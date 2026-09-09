from __future__ import annotations

import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.services.common import DomainError

logger = logging.getLogger(__name__)


def _trace_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(request: Request, exc: DomainError) -> JSONResponse:
        body: dict[str, object] = {
            "code": exc.code,
            "message": exc.message,
            "trace_id": _trace_id(request),
        }
        if exc.details:
            body["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=body)

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> JSONResponse:
        if isinstance(exc.detail, dict):
            body = dict(exc.detail)
            body.setdefault("code", "HTTP_ERROR")
            body.setdefault("message", body.get("detail", "Request failed"))
        else:
            body = {"code": "HTTP_ERROR", "message": str(exc.detail)}
        body["trace_id"] = _trace_id(request)
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {key: value for key, value in item.items() if key not in {"input", "ctx"}}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "code": "VALIDATION_ERROR",
                "message": "Request validation failed",
                # Validation input can contain passwords or administrator tokens.
                # Never reflect request values back to the client or access log.
                "errors": errors,
                "trace_id": _trace_id(request),
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.exception("Unhandled API error", extra={"trace_id": _trace_id(request)})
        return JSONResponse(
            status_code=500,
            content={
                "code": "INTERNAL_ERROR",
                "message": "Internal server error",
                "trace_id": _trace_id(request),
            },
        )
