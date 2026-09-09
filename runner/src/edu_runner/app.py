from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from starlette.responses import JSONResponse

from . import __version__
from .config import Settings
from .executor import ExecutorUnavailable
from .models import (
    ExecutionRequest,
    ExecutionResponse,
    InteractiveSessionCommand,
    InteractiveSessionCreateRequest,
    InteractiveSessionInput,
    InteractiveSessionResponse,
    ProfileResponse,
)
from .security import AuthenticationError, ReplayError, RequestAuthenticator
from .service import (
    InvalidManifestError,
    InteractiveSessionNotFoundError,
    InteractiveSessionStateError,
    ProfileUnavailableError,
    RunnerBusyError,
    RunnerService,
)


def create_app(
    settings: Settings | None = None,
    *,
    service: RunnerService | None = None,
) -> FastAPI:
    actual_settings = settings or Settings.from_env()
    actual_service = service or RunnerService(actual_settings)
    authenticator = RequestAuthenticator(
        actual_settings.shared_secret,
        clock_skew_seconds=actual_settings.signature_clock_skew_seconds,
        nonce_ttl_seconds=actual_settings.nonce_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        actual_service.close_interactive_sessions()

    application = FastAPI(
        title="Education Programming Runner",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.runner_service = actual_service

    @application.middleware("http")
    async def bound_request_body(request: Request, call_next):  # type: ignore[no-untyped-def]
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                if int(declared) > actual_settings.request_body_limit_bytes:
                    return JSONResponse(
                        {"detail": "request body too large"},
                        status_code=413,
                    )
            except ValueError:
                return JSONResponse(
                    {"detail": "invalid Content-Length"},
                    status_code=status.HTTP_400_BAD_REQUEST,
                )
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > actual_settings.request_body_limit_bytes:
                return JSONResponse(
                    {"detail": "request body too large"},
                    status_code=413,
                )
            body.extend(chunk)
        # Starlette's cached request forwards the cached bytes to the endpoint,
        # avoiding a second unbounded read by FastAPI's JSON parser.
        request._body = bytes(body)  # type: ignore[attr-defined]
        return await call_next(request)

    async def require_internal_signature(request: Request) -> None:
        body = await request.body()
        try:
            authenticator.verify(request.headers, body)
        except ReplayError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="replayed runner request",
            ) from exc
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid runner authentication",
            ) from exc

    @application.get("/health/live")
    def live() -> dict[str, object]:
        return {"status": "ok", "version": __version__}

    @application.get("/health/ready")
    def ready(response: Response) -> dict[str, object]:
        readiness = actual_service.readiness()
        if not readiness["ready"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return readiness

    @application.get(
        "/v1/profiles",
        response_model=list[ProfileResponse],
        dependencies=[Depends(require_internal_signature)],
    )
    def profiles() -> list[ProfileResponse]:
        return actual_service.list_profiles()

    @application.post(
        "/v1/jobs",
        response_model=ExecutionResponse,
        dependencies=[Depends(require_internal_signature)],
    )
    async def execute(request: ExecutionRequest) -> ExecutionResponse:
        try:
            # Compilation is blocking and must never run in the ASGI event loop.
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(actual_service.execute, request)
        except InvalidManifestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RunnerBusyError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (ProfileUnavailableError, ExecutorUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @application.post(
        "/v1/interactive-sessions",
        response_model=InteractiveSessionResponse,
        dependencies=[Depends(require_internal_signature)],
    )
    async def start_interactive(
        request: InteractiveSessionCreateRequest,
    ) -> InteractiveSessionResponse:
        try:
            from starlette.concurrency import run_in_threadpool

            return await run_in_threadpool(actual_service.start_interactive, request)
        except InvalidManifestError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RunnerBusyError as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from exc
        except (ProfileUnavailableError, ExecutorUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    def interactive_error(exc: Exception) -> HTTPException:
        if isinstance(exc, InteractiveSessionNotFoundError):
            return HTTPException(
                status_code=404, detail="interactive session was not found"
            )
        return HTTPException(
            status_code=409, detail="interactive program is not running"
        )

    @application.post(
        "/v1/interactive-sessions/{session_id}/state",
        response_model=InteractiveSessionResponse,
        dependencies=[Depends(require_internal_signature)],
    )
    def interactive_state(
        session_id: str, command: InteractiveSessionCommand
    ) -> InteractiveSessionResponse:
        try:
            return actual_service.interactive_snapshot(
                session_id, owner_key=command.owner_key
            )
        except (InteractiveSessionNotFoundError, InteractiveSessionStateError) as exc:
            raise interactive_error(exc) from exc

    @application.post(
        "/v1/interactive-sessions/{session_id}/input",
        response_model=InteractiveSessionResponse,
        dependencies=[Depends(require_internal_signature)],
    )
    def interactive_input(
        session_id: str, command: InteractiveSessionInput
    ) -> InteractiveSessionResponse:
        try:
            return actual_service.interactive_send_line(
                session_id, owner_key=command.owner_key, text=command.text
            )
        except (InteractiveSessionNotFoundError, InteractiveSessionStateError) as exc:
            raise interactive_error(exc) from exc

    @application.post(
        "/v1/interactive-sessions/{session_id}/eof",
        response_model=InteractiveSessionResponse,
        dependencies=[Depends(require_internal_signature)],
    )
    def interactive_eof(
        session_id: str, command: InteractiveSessionCommand
    ) -> InteractiveSessionResponse:
        try:
            return actual_service.interactive_close_input(
                session_id, owner_key=command.owner_key
            )
        except (InteractiveSessionNotFoundError, InteractiveSessionStateError) as exc:
            raise interactive_error(exc) from exc

    @application.post(
        "/v1/interactive-sessions/{session_id}/stop",
        response_model=InteractiveSessionResponse,
        dependencies=[Depends(require_internal_signature)],
    )
    def interactive_stop(
        session_id: str, command: InteractiveSessionCommand
    ) -> InteractiveSessionResponse:
        try:
            return actual_service.interactive_stop(
                session_id, owner_key=command.owner_key
            )
        except (InteractiveSessionNotFoundError, InteractiveSessionStateError) as exc:
            raise interactive_error(exc) from exc

    return application
