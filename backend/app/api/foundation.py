from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.auth.csrf import CSRFProtection
from app.core.config import Settings

router = APIRouter(tags=["system"])


async def _database_status(request: Request) -> tuple[bool, str]:
    try:
        async with request.app.state.session_factory() as db:
            await db.execute(text("SELECT 1"))
    except Exception as exc:  # health must translate driver failures, not leak their details
        return False, exc.__class__.__name__
    return True, "ok"


def _health_body(request: Request, database_ok: bool, database_detail: str) -> dict[str, object]:
    settings: Settings = request.app.state.settings
    services: list[dict[str, str]] = [
        {
            "name": "database",
            "status": "ok" if database_ok else "unavailable",
            "detail": database_detail,
        }
    ]
    terminal_worker = getattr(request.app.state, "terminal_checkpoint_worker_task", None)
    terminal_worker_ok = terminal_worker is None or not terminal_worker.done()
    if terminal_worker is not None:
        services.append(
            {
                "name": "terminal-checkpoint-worker",
                "status": "ok" if terminal_worker_ok else "unavailable",
                "detail": "running" if terminal_worker_ok else "stopped",
            }
        )
    return {
        "status": "ok" if database_ok and terminal_worker_ok else "degraded",
        "database": "ok" if database_ok else "unavailable",
        "build": settings.app_build,
        "time": datetime.now(UTC).isoformat(),
        "services": services,
    }


@router.get("/health", name="health")
@router.get("/system/health", name="system-health")
async def health(request: Request) -> JSONResponse:
    database_ok, detail = await _database_status(request)
    terminal_worker = getattr(request.app.state, "terminal_checkpoint_worker_task", None)
    terminal_worker_ok = terminal_worker is None or not terminal_worker.done()
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK
            if database_ok and terminal_worker_ok
            else status.HTTP_503_SERVICE_UNAVAILABLE
        ),
        content=_health_body(request, database_ok, detail),
    )


@router.get("/system/readiness", name="system-readiness")
async def readiness(request: Request) -> JSONResponse:
    return await health(request)


@router.get("/auth/csrf", tags=["auth"], name="auth-csrf")
async def csrf(request: Request, response: Response) -> dict[str, str]:
    settings: Settings = request.app.state.settings
    protection: CSRFProtection = request.app.state.csrf_protection
    token = protection.issue()
    response.set_cookie(
        settings.csrf_cookie_name,
        token,
        max_age=settings.csrf_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )
    response.headers["X-CSRFToken"] = token
    return {"csrf_token": token}
