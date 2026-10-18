from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.exceptions import RequestValidationError

from .config import Settings
from .models import Query
from .security import RequestAuthenticator
from .service import LanguageService, QueryTimeout, Unavailable


def create_app(
    settings: Settings | None = None, *, service: LanguageService | None = None
) -> FastAPI:
    settings = settings or Settings.from_env()
    service = service or LanguageService(settings)
    authenticator = RequestAuthenticator(settings.shared_secret)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await service.start()
        try:
            yield
        finally:
            await service.close()

    app = FastAPI(
        title="Private clangd service",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError):
        # Pydantic's default response includes the invalid source/input verbatim.
        return JSONResponse(
            {"detail": "invalid language service request"}, status_code=422
        )

    @app.middleware("http")
    async def bound_body(request: Request, call_next):
        request.state.deadline = time.monotonic() + settings.request_seconds
        declared = request.headers.get("content-length")
        if declared:
            try:
                if not 0 <= int(declared) <= settings.body_limit:
                    return JSONResponse(
                        {"detail": "request too large"}, status_code=413
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid content length"}, status_code=400
                )
        body = bytearray()
        try:
            async with asyncio.timeout(min(settings.request_seconds, 3)):
                async for chunk in request.stream():
                    if len(body) + len(chunk) > settings.body_limit:
                        return JSONResponse(
                            {"detail": "request too large"}, status_code=413
                        )
                    body.extend(chunk)
        except TimeoutError:
            return JSONResponse({"detail": "request upload timeout"}, status_code=408)
        request._body = bytes(body)
        return await call_next(request)

    async def authenticate(request: Request):
        try:
            authenticator.verify(request.headers, await request.body())
        except ValueError as exc:
            raise HTTPException(401, "invalid language service authentication") from exc

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        return JSONResponse(
            {
                "ready": service.ready,
                "isolation": "test"
                if settings.test_without_sandbox
                else settings.isolation,
                "filesystem_isolated": (
                    service.ready
                    and settings.isolation == "landlock"
                    and not settings.test_without_sandbox
                ),
            },
            status_code=200 if service.ready else 503,
        )

    @app.post("/v1/query", dependencies=[Depends(authenticate)])
    async def query(payload: Query, request: Request):
        operation = None
        try:
            async with asyncio.timeout(
                max(0, request.state.deadline - time.monotonic())
            ):
                operation = asyncio.create_task(service.query(payload))
                while not operation.done():
                    await asyncio.wait({operation}, timeout=0.2)
                    if not operation.done() and await request.is_disconnected():
                        raise HTTPException(499, "language request cancelled")
                return operation.result()
        except (QueryTimeout, TimeoutError) as exc:
            raise HTTPException(504, "language service timeout") from exc
        except Unavailable as exc:
            raise HTTPException(503, "language service unavailable") from exc
        finally:
            if operation is not None and not operation.done():
                operation.cancel()
                with contextlib.suppress(asyncio.CancelledError, Unavailable):
                    await operation

    return app
