from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from starlette.responses import JSONResponse

from . import __version__
from .config import Settings
from .models import (
    AssignmentSubmissionPrepareRequest,
    AssignmentSubmissionPrepareResponse,
    AssignmentSubmissionSyncRequest,
    AssignmentSubmissionSyncResponse,
    CourseDiscoverRequest,
    CourseDiscoverResponse,
    GradeRequest,
    GradeResponse,
    HealthResponse,
    HistoricalSubmissionsRequest,
    HistoricalSubmissionsResponse,
    LoginRequest,
    LoginResponse,
    QuizAnswersSyncRequest,
    QuizAnswersSyncResponse,
    QuizEssayPrepareRequest,
    QuizEssayPrepareResponse,
    QuizEssaySyncRequest,
    QuizEssaySyncResponse,
)
from .security import AuthenticationError, ReplayError, RequestAuthenticator
from .service import (
    BrowserBusy,
    BrowserNavigationUnavailable,
    BrowserUnavailable,
    IdempotencyConflict,
    MoodleActivityUnavailable,
    MoodleAttemptFinalized,
    MoodleBrowserError,
    MoodleBrowserService,
    MoodleContractError,
    MoodleCredentialsRejected,
    MoodleProtocolError,
    MoodleSessionExpired,
    QuizPreviewRejected,
    TeacherMembershipRequired,
)

logger = logging.getLogger(__name__)

# Only fixed diagnostic codes cross the service boundary, never page bodies,
# response text or credentials embedded in an unexpected exception.
_PROTOCOL_ERROR_CODES = {
    "Moodle assignment grading page has no submissions table": "ASSIGN_TABLE_NOT_FOUND",
    "Moodle quiz report has no attempts table": "QUIZ_TABLE_NOT_FOUND",
    "Moodle upload rejected: upload_error_invalid_file": "UPLOAD_INVALID_FILE",
    "Moodle upload rejected: invalidfiletype": "UPLOAD_INVALID_TYPE",
    "Moodle upload rejected: maxbytesfile": "UPLOAD_TOO_LARGE",
    "Moodle upload rejected: maxareabytes": "UPLOAD_TOO_LARGE",
    "Moodle upload rejected: repository_error": "UPLOAD_REJECTED",
}


def create_app(
    settings: Settings | None = None,
    *,
    service: MoodleBrowserService | None = None,
) -> FastAPI:
    actual_settings = settings or Settings.from_env()
    actual_service = service or MoodleBrowserService(actual_settings)
    manage_lifecycle = service is None
    authenticator = RequestAuthenticator(
        actual_settings.shared_secret,
        clock_skew_seconds=actual_settings.signature_clock_skew_seconds,
        nonce_ttl_seconds=actual_settings.nonce_ttl_seconds,
    )

    @asynccontextmanager
    async def lifespan(_application: FastAPI) -> AsyncIterator[None]:
        if manage_lifecycle:
            await actual_service.start()
        try:
            yield
        finally:
            if manage_lifecycle:
                await actual_service.close()

    application = FastAPI(
        title="Education Moodle Browser",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.moodle_browser_service = actual_service

    @application.middleware("http")
    async def bound_request_body(request: Request, call_next):  # type: ignore[no-untyped-def]
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
                if declared_size < 0:
                    return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
                if declared_size > actual_settings.request_body_max_bytes:
                    return JSONResponse({"detail": "request body too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "invalid Content-Length"}, status_code=400)
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > actual_settings.request_body_max_bytes:
                return JSONResponse({"detail": "request body too large"}, status_code=413)
            body.extend(chunk)
        request._body = bytes(body)  # type: ignore[attr-defined]
        return await call_next(request)

    async def require_internal_signature(request: Request) -> None:
        body = await request.body()
        try:
            authenticator.verify(request.headers, body)
        except ReplayError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="replayed Moodle browser request",
            ) from exc
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid Moodle browser authentication",
            ) from exc

    @application.exception_handler(MoodleCredentialsRejected)
    async def credentials_rejected(
        request: Request, exc: MoodleCredentialsRejected
    ) -> JSONResponse:
        logger.info(
            "Moodle browser rejected authentication operation=%s type=%s",
            request.url.path,
            type(exc).__name__,
        )
        detail = (
            "Moodle browser session expired"
            if isinstance(exc, MoodleSessionExpired)
            else "Moodle credentials were not accepted"
        )
        return JSONResponse({"detail": detail}, status_code=status.HTTP_401_UNAUTHORIZED)

    @application.exception_handler(TeacherMembershipRequired)
    async def teacher_required(_request: Request, _exc: TeacherMembershipRequired) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle did not confirm teacher membership"},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @application.exception_handler(QuizPreviewRejected)
    async def preview_rejected(_request: Request, _exc: QuizPreviewRejected) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle teacher preview cannot receive student artifacts"},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @application.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(_request: Request, _exc: IdempotencyConflict) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle idempotency key conflicts with an earlier request"},
            status_code=status.HTTP_409_CONFLICT,
        )

    @application.exception_handler(MoodleAttemptFinalized)
    async def attempt_finalized(_request: Request, _exc: MoodleAttemptFinalized) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle attempt is already finalized"},
            status_code=status.HTTP_423_LOCKED,
        )

    @application.exception_handler(MoodleActivityUnavailable)
    async def activity_unavailable(
        _request: Request, _exc: MoodleActivityUnavailable
    ) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle assessment is not available to this student"},
            status_code=status.HTTP_403_FORBIDDEN,
        )

    @application.exception_handler(BrowserBusy)
    async def browser_busy(_request: Request, _exc: BrowserBusy) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle browser is busy"},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            headers={"Retry-After": "2"},
        )

    @application.exception_handler(BrowserUnavailable)
    async def browser_unavailable(request: Request, exc: BrowserUnavailable) -> JSONResponse:
        logger.warning(
            "Moodle browser unavailable operation=%s type=%s detail=%s",
            request.url.path,
            type(exc).__name__,
            exc,
        )
        return JSONResponse(
            {"detail": "Moodle browser is unavailable"},
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            headers=(
                {"X-Moodle-Error-Code": exc.diagnostic_code}
                if isinstance(exc, BrowserNavigationUnavailable) else None
            ),
        )

    @application.exception_handler(MoodleProtocolError)
    async def protocol_error(request: Request, exc: MoodleProtocolError) -> JSONResponse:
        # Connector exceptions contain only bounded messages authored by this
        # service.  Logging the category and message makes Moodle markup drift
        # diagnosable without recording credentials, cookies or page bodies.
        logger.warning(
            "Moodle browser protocol error operation=%s type=%s detail=%s",
            request.url.path,
            type(exc).__name__,
            exc,
        )
        content = {"detail": "Moodle returned an unsupported page"}
        diagnostic = _PROTOCOL_ERROR_CODES.get(str(exc))
        if diagnostic is not None:
            content["code"] = diagnostic
        return JSONResponse(
            content,
            status_code=status.HTTP_502_BAD_GATEWAY,
            headers={"X-Moodle-Error-Code": diagnostic} if diagnostic is not None else None,
        )

    @application.exception_handler(MoodleContractError)
    async def contract_error(_request: Request, _exc: MoodleContractError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle browser request violates the configured contract"},
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        )

    @application.exception_handler(MoodleBrowserError)
    async def browser_error(_request: Request, _exc: MoodleBrowserError) -> JSONResponse:
        return JSONResponse(
            {"detail": "Moodle browser operation failed"},
            status_code=status.HTTP_502_BAD_GATEWAY,
        )

    @application.get("/health/live", response_model=HealthResponse)
    def live() -> HealthResponse:
        readiness = actual_service.readiness()
        return HealthResponse(
            status="ok",
            version=__version__,
            browser=readiness["browser"],  # type: ignore[arg-type]
            ready=bool(readiness["ready"]),
        )

    @application.get("/health/ready", response_model=HealthResponse)
    def ready(response: Response) -> HealthResponse:
        readiness = actual_service.readiness()
        if not readiness["ready"]:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(
            status="ok" if readiness["ready"] else "unavailable",
            version=__version__,
            browser=readiness["browser"],  # type: ignore[arg-type]
            ready=bool(readiness["ready"]),
        )

    signature = [Depends(require_internal_signature)]

    @application.post(
        "/internal/v1/moodle/login",
        response_model=LoginResponse,
        dependencies=signature,
    )
    async def login(payload: LoginRequest) -> LoginResponse:
        try:
            return await asyncio.wait_for(
                actual_service.login(payload),
                timeout=actual_settings.login_operation_timeout_seconds,
            )
        except TimeoutError as exc:
            raise BrowserUnavailable("Moodle login exceeded its bounded deadline") from exc

    @application.post(
        "/internal/v1/moodle/course/discover",
        response_model=CourseDiscoverResponse,
        dependencies=signature,
    )
    async def discover(payload: CourseDiscoverRequest) -> CourseDiscoverResponse:
        return await actual_service.discover_course(payload)

    @application.post(
        "/internal/v1/moodle/activity/submissions/discover",
        response_model=HistoricalSubmissionsResponse,
        dependencies=signature,
    )
    async def discover_historical_submissions(
        payload: HistoricalSubmissionsRequest,
    ) -> HistoricalSubmissionsResponse:
        return await actual_service.discover_historical_submissions(payload)

    @application.post(
        "/internal/v1/moodle/assignment/grade",
        response_model=GradeResponse,
        dependencies=signature,
    )
    async def grade(payload: GradeRequest) -> GradeResponse:
        return await actual_service.grade_assignment(payload)

    @application.post(
        "/internal/v1/moodle/assignment/submission/prepare",
        response_model=AssignmentSubmissionPrepareResponse,
        dependencies=signature,
    )
    async def prepare_assignment_submission(
        payload: AssignmentSubmissionPrepareRequest,
    ) -> AssignmentSubmissionPrepareResponse:
        return await actual_service.prepare_assignment_submission(payload)

    @application.post(
        "/internal/v1/moodle/assignment/submission/sync",
        response_model=AssignmentSubmissionSyncResponse,
        dependencies=signature,
    )
    async def sync_assignment_submission(
        payload: AssignmentSubmissionSyncRequest,
    ) -> AssignmentSubmissionSyncResponse:
        return await actual_service.sync_assignment_submission(payload)

    @application.post(
        "/internal/v1/moodle/quiz/essay/prepare",
        response_model=QuizEssayPrepareResponse,
        dependencies=signature,
    )
    async def prepare_quiz_essay(
        payload: QuizEssayPrepareRequest,
    ) -> QuizEssayPrepareResponse:
        return await actual_service.prepare_quiz_essay(payload)

    @application.post(
        "/internal/v1/moodle/quiz/essay/sync",
        response_model=QuizEssaySyncResponse,
        dependencies=signature,
    )
    async def sync_quiz_essay(payload: QuizEssaySyncRequest) -> QuizEssaySyncResponse:
        return await actual_service.sync_quiz_essay(payload)

    @application.post(
        "/internal/v1/moodle/quiz/answers/sync",
        response_model=QuizAnswersSyncResponse,
        dependencies=signature,
    )
    async def sync_quiz_answers(payload: QuizAnswersSyncRequest) -> QuizAnswersSyncResponse:
        return await actual_service.sync_quiz_answers(payload)

    return application
