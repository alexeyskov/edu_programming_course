from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.httpsredirect import HTTPSRedirectMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import __version__
from app.api.errors import install_exception_handlers
from app.api.middleware import RequestIDMiddleware, SecurityHeadersMiddleware
from app.api.router import api_router
from app.auth.csrf import CSRFProtection
from app.auth.middleware import CSRFMiddleware, SessionAuthenticationMiddleware
from app.core.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory


def create_app(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    owned_engine = engine is None
    engine = engine or create_engine(settings)
    session_factory = session_factory or create_session_factory(engine)

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        terminal_worker_stop: asyncio.Event | None = None
        terminal_worker_task: asyncio.Task[int] | None = None
        if settings.sync_embedded_terminal_worker_enabled:
            # A dedicated sync-worker remains responsible for the complete
            # outbox.  This single narrow lane is a safety net for final student
            # submissions, so a missing/restarting worker cannot leave the IDE
            # waiting forever after the local attempt has already been locked.
            from app.workers.sync import run_sync_worker

            terminal_worker_stop = asyncio.Event()
            terminal_worker_task = asyncio.create_task(
                run_sync_worker(
                    session_factory,
                    settings,
                    stop_event=terminal_worker_stop,
                    terminal_checkpoints_only=True,
                    concurrency=1,
                ),
                name="embedded-terminal-checkpoint-worker",
            )
        application.state.terminal_checkpoint_worker_task = terminal_worker_task
        try:
            yield
        finally:
            if terminal_worker_stop is not None:
                terminal_worker_stop.set()
            if terminal_worker_task is not None:
                terminal_worker_task.cancel()
                with suppress(asyncio.CancelledError):
                    await terminal_worker_task
            if owned_engine:
                await engine.dispose()

    app = FastAPI(
        title=f"{settings.app_name} API",
        version=__version__,
        docs_url=f"{settings.api_prefix}/docs" if settings.debug else None,
        openapi_url=f"{settings.api_prefix}/openapi.json" if settings.debug else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.csrf_protection = CSRFProtection(settings)

    app.include_router(api_router, prefix=settings.api_prefix)
    install_exception_handlers(app)

    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(SessionAuthenticationMiddleware, settings=settings)
    app.add_middleware(CSRFMiddleware, settings=settings, protection=app.state.csrf_protection)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-CSRFToken",
            "X-Request-ID",
            "Idempotency-Key",
            "If-Match",
        ],
        expose_headers=["X-CSRFToken", "X-Request-ID"],
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)
    if settings.secure_ssl_redirect:
        app.add_middleware(HTTPSRedirectMiddleware)
    app.add_middleware(RequestIDMiddleware)
    return app


app = create_app()
