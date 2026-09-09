from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, TypedDict

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import CurrentAuth
from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.errors import (
    IntegrationAssessmentUnavailable,
    IntegrationAttemptFinalized,
    IntegrationBusy,
    IntegrationConfigurationError,
    IntegrationError,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from app.integrations.moodle_browser import MoodleBrowserClient
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.integrations.runner import RunnerAdapter, RunnerResult
from app.models.attempts import (
    Attempt,
    EditEvent,
    RunRequest,
    RunResult,
    Snapshot,
    Submission,
    Workspace,
    WorkspaceFile,
)
from app.models.enums import AttemptState, RunOrigin, RunStatus, SyncOutboxState
from app.models.evidence import EvidenceReport
from app.models.identity import ExternalPrincipal
from app.models.integration import SyncOutbox, SystemSetting
from app.models.tasks import Assessment, TaskVersion
from app.schemas.attempts import (
    AttemptHistoryEventRead,
    AttemptStartRequest,
    AttemptStudentRead,
    AttemptSubmitRead,
    AttemptSubmitRequest,
    ClipboardReceiptCreateRequest,
    ClipboardReceiptRead,
    WorkspaceFileCreateRead,
    WorkspaceFileCreateRequest,
    WorkspaceFileDeleteRead,
    WorkspaceFileDeleteRequest,
    WorkspaceFilePatchRead,
    WorkspaceFilePatchRequest,
    WorkspaceFileRead,
    WorkspaceStudentRead,
)
from app.schemas.runs import (
    InteractiveRunCreateRequest,
    InteractiveRunInputRequest,
    InteractiveRunRead,
    RunCreateRequest,
    RunResultPayloadRead,
    RunStudentRead,
    RunTeacherRead,
)
from app.services.build_profile import (
    effective_attempt_build_profile,
    effective_workspace_build_profile,
)
from app.services.client_context import client_context_from_request, normalize_client_context
from app.services.common import DomainError, sha256_text
from app.services.moodle_quiz_runtime import (
    PreparedMoodleAssignment,
    PreparedMoodleQuizAttempt,
    pinned_moodle_quiz_binding,
    prepared_moodle_assignment,
    prepared_moodle_quiz_attempt,
    resolve_moodle_assignment_context,
    resolve_moodle_quiz_context,
)
from app.services.policy import (
    ensure_assessment_available,
    require_membership,
    require_submission_review_access,
    resolve_assigned_task_version,
)
from app.services.workspace import (
    create_clipboard_receipt,
    create_workspace_file,
    delete_workspace_file,
    ensure_attempt_not_finalized_in_moodle,
    replace_file_content,
    retry_submission_checkpoint,
    start_attempt,
    submit_attempt,
)

router = APIRouter()
DB = Annotated[AsyncSession, Depends(get_db)]


def _moodle_preparation_error(exc: IntegrationError) -> DomainError:
    if isinstance(exc, IntegrationAssessmentUnavailable):
        return DomainError(
            409,
            "MOODLE_ASSESSMENT_UNAVAILABLE",
            "The assessment is not currently available to this student in Moodle",
        )
    if isinstance(exc, IntegrationAttemptFinalized):
        return DomainError(
            409,
            "LMS_ATTEMPT_FINALIZED",
            "The Moodle attempt was already finalized outside the application",
        )
    if isinstance(exc, IntegrationConfigurationError | MoodleAuthenticationError):
        return DomainError(
            409,
            "MOODLE_REAUTHENTICATION_REQUIRED",
            "Sign in to Moodle again before starting this assessment",
        )
    if isinstance(exc, IntegrationBusy):
        return DomainError(
            409,
            "MOODLE_SESSION_BUSY",
            "The Moodle session is busy; retry in a moment",
        )
    if isinstance(exc, IntegrationProtocolError):
        return DomainError(
            502,
            "MOODLE_RUNTIME_PREPARATION_FAILED",
            "Moodle did not confirm the assessment attempt",
        )
    if isinstance(exc, IntegrationUnavailable):
        return DomainError(
            503,
            "MOODLE_RUNTIME_PREPARATION_UNAVAILABLE",
            "Moodle is temporarily unavailable",
        )
    return DomainError(
        502,
        "MOODLE_RUNTIME_PREPARATION_FAILED",
        "The Moodle assessment attempt could not be prepared",
    )


async def _prepare_moodle_quiz_attempt(
    db: AsyncSession,
    settings: Settings,
    *,
    assessment_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> PreparedMoodleQuizAttempt | None:
    context = await resolve_moodle_quiz_context(db, assessment_id)
    if context is None:
        return None

    # Fail locally before asking Moodle to create an attempt.  start_attempt
    # repeats every check under its row lock after the external round trip.
    membership = await require_membership(
        db,
        principal_id=principal_id,
        course_id=context.assessment.course_id,
        role="STUDENT",
    )
    await ensure_assessment_available(db, context.assessment, membership)
    existing = list(
        (
            await db.scalars(
                select(Attempt)
                .where(
                    Attempt.assessment_id == assessment_id,
                    Attempt.principal_id == principal_id,
                )
                .order_by(Attempt.sequence)
            )
        ).all()
    )
    active = next((row for row in existing if row.state == AttemptState.ACTIVE.value), None)
    pinned = (
        pinned_moodle_quiz_binding(
            active.integrity_policy,
            course_external_id=context.course.external_id,
            cmid=context.cmid,
        )
        if active is not None
        else None
    )
    await resolve_assigned_task_version(db, context.assessment, membership)

    # Credential lease helpers live with the only other foreground browser
    # workflow (course discovery).  Reusing them here preserves the same
    # encrypted-state, optimistic-revision and busy-session semantics.
    from app.api.courses import (
        _claim_browser_credential,
        _expire_browser_credential,
        _release_browser_credential,
        _store_refreshed_browser_credential,
    )

    try:
        lease = await _claim_browser_credential(
            db,
            settings,
            connection_id=context.connection.id,
            principal_id=principal_id,
        )
    except IntegrationError as exc:
        raise _moodle_preparation_error(exc) from exc
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            browser = MoodleBrowserClient(
                settings,
                client,
                base_url=context.connection.base_url,
                storage_state=lease.state,
            )
            expected = (
                {
                    "expected_attempt_id": pinned[0],
                    "expected_question_slot": pinned[1],
                }
                if pinned is not None
                else {}
            )
            result = await browser.prepare_quiz_essay(
                context.course.external_id,
                context.cmid,
                **expected,
            )
        preparation = result.preparation
        prepared = prepared_moodle_quiz_attempt(
            course_external_id=preparation.course_id,
            cmid=preparation.cmid,
            external_attempt_id=preparation.attempt_id,
            question_slot=preparation.question_slot,
            question_text=preparation.question_text,
            answer_transport=preparation.answer_transport,
            available_answer_transports=preparation.available_answer_transports,
            remaining_seconds=preparation.remaining_seconds,
        )
        if (
            prepared.course_external_id != context.course.external_id
            or prepared.cmid != context.cmid
        ):
            raise DomainError(
                502,
                "MOODLE_RUNTIME_PREPARATION_MISMATCH",
                "Moodle prepared a different course activity",
            )
    except MoodleAuthenticationError as exc:
        await asyncio.shield(_expire_browser_credential(db, lease))
        raise _moodle_preparation_error(exc) from exc
    except IntegrationError as exc:
        await asyncio.shield(_release_browser_credential(db, lease))
        raise _moodle_preparation_error(exc) from exc
    except BaseException:
        await asyncio.shield(_release_browser_credential(db, lease))
        raise

    try:
        await _store_refreshed_browser_credential(
            db,
            settings,
            lease,
            result.storage_state,
            connection_id=context.connection.id,
            principal_id=principal_id,
        )
    except IntegrationError as exc:
        raise _moodle_preparation_error(exc) from exc
    return prepared


# Compatibility name retained for focused service tests and older imports.
_prepare_deferred_moodle_quiz_attempt = _prepare_moodle_quiz_attempt


async def _prepare_moodle_assignment(
    db: AsyncSession,
    settings: Settings,
    *,
    assessment_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> PreparedMoodleAssignment | None:
    context = await resolve_moodle_assignment_context(db, assessment_id)
    if context is None:
        return None
    membership = await require_membership(
        db,
        principal_id=principal_id,
        course_id=context.assessment.course_id,
        role="STUDENT",
    )
    await ensure_assessment_available(db, context.assessment, membership)
    await resolve_assigned_task_version(db, context.assessment, membership)

    from app.api.courses import (
        _claim_browser_credential,
        _expire_browser_credential,
        _release_browser_credential,
        _store_refreshed_browser_credential,
    )

    try:
        lease = await _claim_browser_credential(
            db,
            settings,
            connection_id=context.connection.id,
            principal_id=principal_id,
        )
    except IntegrationError as exc:
        raise _moodle_preparation_error(exc) from exc
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            browser = MoodleBrowserClient(
                settings,
                client,
                base_url=context.connection.base_url,
                storage_state=lease.state,
            )
            result = await browser.prepare_assignment_submission(
                context.course.external_id,
                context.cmid,
            )
        preparation = result.preparation
        prepared = prepared_moodle_assignment(
            course_external_id=preparation.course_id,
            cmid=preparation.cmid,
            answer_transport=preparation.answer_transport,
            available_answer_transports=preparation.available_answer_transports,
        )
        if (
            prepared.course_external_id != context.course.external_id
            or prepared.cmid != context.cmid
        ):
            raise DomainError(
                502,
                "MOODLE_RUNTIME_PREPARATION_MISMATCH",
                "Moodle prepared a different course activity",
            )
    except MoodleAuthenticationError as exc:
        await asyncio.shield(_expire_browser_credential(db, lease))
        raise _moodle_preparation_error(exc) from exc
    except IntegrationError as exc:
        await asyncio.shield(_release_browser_credential(db, lease))
        raise _moodle_preparation_error(exc) from exc
    except BaseException:
        await asyncio.shield(_release_browser_credential(db, lease))
        raise

    try:
        await _store_refreshed_browser_credential(
            db,
            settings,
            lease,
            result.storage_state,
            connection_id=context.connection.id,
            principal_id=principal_id,
        )
    except IntegrationError as exc:
        raise _moodle_preparation_error(exc) from exc
    return prepared


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


def _interactive_start_admission_lock(request: Request) -> asyncio.Lock:
    """Serialize the short interactive-run reservation inside one worker.

    PostgreSQL row locks on the attempt/workspace or submission/experiment are
    authoritative across workers.  This process-local guard also gives the
    SQLite test database equivalent admission semantics and avoids two local
    requests reaching the database lock at the same instant.
    """

    lock = getattr(request.app.state, "interactive_start_admission_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.interactive_start_admission_lock = lock
    return lock


def _expected_revision(value: str | None) -> int:
    if value is None:
        raise DomainError(428, "REVISION_REQUIRED", "If-Match revision is required")
    normalized = value.strip()
    if normalized.startswith("W/"):
        normalized = normalized[2:].strip()
    normalized = normalized.strip('"')
    try:
        revision = int(normalized)
    except ValueError as exc:
        raise DomainError(
            422, "INVALID_REVISION", "If-Match must contain an integer revision"
        ) from exc
    if revision < 0:
        raise DomainError(422, "INVALID_REVISION", "Revision cannot be negative")
    return revision


async def _owned_attempt(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    lock: bool = False,
) -> tuple[Attempt, Assessment, Workspace]:
    statement = select(Attempt).where(
        Attempt.id == attempt_id,
        Attempt.principal_id == principal_id,
    )
    if lock:
        statement = statement.with_for_update()
    attempt = await db.scalar(statement)
    if attempt is None:
        raise DomainError(404, "ATTEMPT_NOT_FOUND", "Attempt was not found")
    assessment = await db.get(Assessment, attempt.assessment_id)
    if assessment is None:
        raise DomainError(500, "ASSESSMENT_MISSING", "Attempt assessment is missing")
    await require_membership(
        db,
        principal_id=principal_id,
        course_id=assessment.course_id,
        role="STUDENT",
    )
    workspace_statement = select(Workspace).where(Workspace.attempt_id == attempt.id)
    if lock:
        workspace_statement = workspace_statement.with_for_update()
    workspace = await db.scalar(workspace_statement)
    if workspace is None:
        raise DomainError(500, "WORKSPACE_MISSING", "Attempt workspace is missing")
    return attempt, assessment, workspace


async def _active_files(db: AsyncSession, workspace_id: uuid.UUID) -> list[WorkspaceFile]:
    return list(
        (
            await db.scalars(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == workspace_id,
                    WorkspaceFile.deleted_revision.is_(None),
                )
                .order_by(WorkspaceFile.path)
            )
        ).all()
    )


def _file_read(file: WorkspaceFile, *, read_only: bool = False) -> WorkspaceFileRead:
    return WorkspaceFileRead(
        id=file.id,
        path=file.path,
        language=file.language,
        content=file.content,
        read_only=read_only,
        created_revision=file.created_revision,
        deleted_revision=file.deleted_revision,
    )


class _EffectiveRuntimeSettings(TypedDict):
    ai_enabled: bool
    student_ai_enabled: bool
    runner_enabled: bool
    runner_cpu_seconds: int
    runner_memory_mb: int


def _bounded_setting_int(
    values: dict[str, Any],
    key: str,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value if minimum <= value <= maximum else default


async def _effective_flags(db: AsyncSession, settings: Settings) -> _EffectiveRuntimeSettings:
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == "effective"))
    values = row.value if row is not None else {}
    ai_configured = settings.ai_mock_enabled or (
        settings.ai_enabled and bool(settings.ai_api_key.get_secret_value())
    )
    runner_configured = settings.runner_mock_enabled or bool(settings.runner_url)
    return {
        "ai_enabled": ai_configured and bool(values.get("ai_enabled", True)),
        "student_ai_enabled": ai_configured
        and bool(values.get("ai_enabled", True))
        and bool(values.get("student_ai_enabled", True)),
        "runner_enabled": runner_configured and bool(values.get("runner_enabled", True)),
        "runner_cpu_seconds": _bounded_setting_int(
            values,
            "runner_cpu_seconds",
            default=4,
            minimum=1,
            maximum=300,
        ),
        "runner_memory_mb": _bounded_setting_int(
            values,
            "runner_memory_mb",
            default=256,
            minimum=64,
            maximum=65_536,
        ),
    }


async def _checkpoint_state(
    db: AsyncSession,
    attempt_id: uuid.UUID,
    *,
    terminal_only: bool = False,
    now: datetime | None = None,
) -> tuple[datetime | None, str]:
    events = list(
        (
            await db.scalars(
                select(SyncOutbox)
                .where(
                    SyncOutbox.attempt_id == attempt_id,
                    SyncOutbox.event_type == "attempt.checkpoint",
                )
                .order_by(SyncOutbox.created_at.desc(), SyncOutbox.updated_at.desc())
                .limit(100)
            )
        ).all()
    )
    event = next(
        (
            row
            for row in events
            if not terminal_only
            or (
                isinstance(row.payload, dict)
                and row.payload.get("reason") in {"SUBMISSION", "DEADLINE"}
            )
        ),
        None,
    )
    if event is None:
        return None, "PENDING"
    if event.state == SyncOutboxState.DELIVERED.value:
        return event.delivered_at or event.updated_at, "SYNCED"
    if event.state in {
        SyncOutboxState.FAILED.value,
        SyncOutboxState.BLOCKED.value,
    }:
        return event.last_attempt_at or event.updated_at, "ERROR"
    # RETRY is an active queue state: the worker has already scheduled the next
    # bounded attempt. Exposing it as ERROR made the IDE invite students to
    # click a manual retry while the automatic retry was still running, which
    # could race the same Moodle attempt and upload another file version.
    if event.state == SyncOutboxState.RETRY.value:
        return event.last_attempt_at or event.updated_at, "PENDING"
    # A fresh PENDING row normally gets claimed within one 2-second worker
    # polling interval.  If it is still untouched after this grace period, no
    # consumer is making progress; returning PENDING forever would trap the
    # student behind an endless modal with no actionable recovery.
    current = _aware(now or utcnow())
    created = _aware(event.created_at)
    if (
        event.state == SyncOutboxState.PENDING.value
        and created is not None
        and current is not None
        and created <= current - timedelta(seconds=30)
    ):
        return event.created_at, "ERROR"
    return event.last_attempt_at or event.created_at, "PENDING"


async def _attempt_read(
    db: AsyncSession,
    attempt: Attempt,
    assessment: Assessment,
    workspace: Workspace,
    settings: Settings,
) -> AttemptStudentRead:
    version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt.assigned_task_version_id
        else None
    )
    flags = await _effective_flags(db, settings)
    checkpoint_at, checkpoint_status = await _checkpoint_state(
        db,
        attempt.id,
        terminal_only=attempt.state
        in {AttemptState.SUBMITTED.value, AttemptState.AUTO_SUBMITTED.value},
    )
    assessment_policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    integrity_policy = (
        attempt.integrity_policy if isinstance(attempt.integrity_policy, dict) else {}
    )
    statement = "\n\n".join(
        part.strip()
        for part in (assessment.instructions, version.statement if version else "")
        if part and part.strip()
    )
    return AttemptStudentRead(
        id=attempt.id,
        assessment_id=assessment.id,
        title=assessment.title,
        statement=statement,
        sequence=attempt.sequence,
        state=attempt.state,
        started_at=attempt.started_at,
        expected_end_at=attempt.expected_end_at,
        deadline_at=attempt.deadline_at,
        current_revision=workspace.current_revision,
        submitted_at=attempt.submitted_at,
        paste_policy=assessment.paste_policy,
        ai_enabled=bool(
            flags["ai_enabled"] and flags["student_ai_enabled"] and assessment.student_ai_enabled
        ),
        multi_file=workspace.multi_file,
        last_checkpoint_at=checkpoint_at,
        checkpoint_status=checkpoint_status,
        closure_reason=(
            "LMS_ATTEMPT_FINALIZED" if attempt.submission_source == "MOODLE_FINALIZED" else None
        ),
        requires_live_lms_preparation=bool(
            attempt.state == AttemptState.ACTIVE.value
            and assessment_policy.get("moodle_metadata_read_only") is True
            and integrity_policy.get("moodle_runtime_prepared") is not True
        ),
    )


@router.post(
    "/assessments/{assessment_id}/attempts",
    response_model=AttemptStudentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["attempts"],
)
async def create_attempt(
    assessment_id: uuid.UUID,
    _payload: AttemptStartRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> AttemptStudentRead:
    # A local terminal submission is intentionally asynchronous.  For ordinary
    # assessments (and Moodle Assign, which has no stable remote attempt id in
    # this integration) reopening during delivery must resume that delivery.
    #
    # Moodle Quiz is different: Moodle can already have finalized the remote
    # attempt even when our terminal outbox receipt is still pending or failed,
    # and the teacher may have granted another attempt.  In that case Moodle's
    # live launch page is authoritative.  Let preparation run below; the exact
    # remote attempt-id guard in ``start_attempt`` will reject the launch if
    # Moodle still returns the previous attempt, and will create a new local
    # sequence only when Moodle returns a genuinely new attempt.
    pending_terminal = await db.scalar(
        select(Attempt)
        .where(
            Attempt.assessment_id == assessment_id,
            Attempt.principal_id == auth.principal_id,
            Attempt.state.in_([AttemptState.SUBMITTED.value, AttemptState.AUTO_SUBMITTED.value]),
        )
        .order_by(Attempt.sequence.desc())
        .limit(1)
    )
    if pending_terminal is not None:
        _checkpoint_at, checkpoint_status = await _checkpoint_state(
            db,
            pending_terminal.id,
            terminal_only=True,
        )
        quiz_context = (
            await resolve_moodle_quiz_context(db, assessment_id)
            if checkpoint_status != "SYNCED"
            else None
        )
        if checkpoint_status != "SYNCED" and quiz_context is None:
            attempt, assessment, workspace = await _owned_attempt(
                db,
                attempt_id=pending_terminal.id,
                principal_id=auth.principal_id,
            )
            return await _attempt_read(
                db,
                attempt,
                assessment,
                workspace,
                request.app.state.settings,
            )
    prepared_moodle_quiz = await _prepare_moodle_quiz_attempt(
        db,
        request.app.state.settings,
        assessment_id=assessment_id,
        principal_id=auth.principal_id,
    )
    prepared_moodle_assignment = await _prepare_moodle_assignment(
        db,
        request.app.state.settings,
        assessment_id=assessment_id,
        principal_id=auth.principal_id,
    )
    attempt = await start_attempt(
        db,
        assessment_id=assessment_id,
        principal_id=auth.principal_id,
        prepared_moodle_quiz=prepared_moodle_quiz,
        prepared_moodle_assignment=prepared_moodle_assignment,
        client_context=client_context_from_request(request),
    )
    await db.commit()
    attempt, assessment, workspace = await _owned_attempt(
        db,
        attempt_id=attempt.id,
        principal_id=auth.principal_id,
    )
    return await _attempt_read(db, attempt, assessment, workspace, request.app.state.settings)


@router.get("/attempts/{attempt_id}", response_model=AttemptStudentRead, tags=["attempts"])
async def get_attempt(
    attempt_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> AttemptStudentRead:
    attempt, assessment, workspace = await _owned_attempt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
    )
    return await _attempt_read(db, attempt, assessment, workspace, request.app.state.settings)


@router.get(
    "/attempts/{attempt_id}/workspace",
    response_model=WorkspaceStudentRead,
    tags=["workspace"],
)
async def get_workspace(attempt_id: uuid.UUID, auth: CurrentAuth, db: DB) -> WorkspaceStudentRead:
    attempt, _assessment, workspace = await _owned_attempt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
    )
    deadline = _aware(attempt.deadline_at)
    read_only = attempt.state != AttemptState.ACTIVE.value or bool(
        deadline and utcnow() >= deadline
    )
    files = await _active_files(db, workspace.id)
    return WorkspaceStudentRead(
        id=workspace.id,
        attempt_id=attempt.id,
        current_revision=workspace.current_revision,
        multi_file=workspace.multi_file,
        aggregate_size=workspace.aggregate_size,
        files=[_file_read(file, read_only=read_only) for file in files],
    )


@router.patch(
    "/attempts/{attempt_id}/workspace/files/{file_id}",
    response_model=WorkspaceFilePatchRead,
    tags=["workspace"],
)
async def patch_workspace_file(
    attempt_id: uuid.UUID,
    file_id: uuid.UUID,
    payload: WorkspaceFilePatchRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceFilePatchRead:
    expected_revision = _expected_revision(if_match)
    request_key = (
        payload.client_request_id
        or idempotency_key
        or sha256_text(
            f"edit:{attempt_id}:{file_id}:{expected_revision}:{payload.source}:{payload.content}"
        )
    )
    mutation = await replace_file_content(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        file_id=file_id,
        content=payload.content,
        expected_revision=expected_revision,
        client_request_id=request_key,
        source=payload.source,
        receipt_id=payload.receipt_id,
        client_id=payload.client_id,
        client_context=client_context_from_request(request),
    )
    await db.commit()
    return WorkspaceFilePatchRead(
        file=_file_read(mutation.file),
        revision=mutation.workspace.current_revision,
        workspace_revision=mutation.workspace.current_revision,
    )


@router.post(
    "/attempts/{attempt_id}/workspace/files",
    response_model=WorkspaceFileCreateRead,
    status_code=status.HTTP_201_CREATED,
    tags=["workspace"],
)
@router.post(
    "/attempts/{attempt_id}/workspace/files/new",
    response_model=WorkspaceFileCreateRead,
    status_code=status.HTTP_201_CREATED,
    tags=["workspace"],
)
async def add_workspace_file(
    attempt_id: uuid.UUID,
    payload: WorkspaceFileCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceFileCreateRead:
    expected_revision = _expected_revision(if_match)
    mutation = await create_workspace_file(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        path=payload.path,
        content="",
        expected_revision=expected_revision,
        client_request_id=idempotency_key
        or sha256_text(f"create:{attempt_id}:{expected_revision}:{payload.path}"),
        client_context=client_context_from_request(request),
    )
    await db.commit()
    return WorkspaceFileCreateRead(
        file=_file_read(mutation.file),
        revision=mutation.workspace.current_revision,
    )


@router.delete(
    "/attempts/{attempt_id}/workspace/files/{file_id}",
    response_model=WorkspaceFileDeleteRead,
    tags=["workspace"],
)
async def remove_workspace_file(
    attempt_id: uuid.UUID,
    file_id: uuid.UUID,
    _payload: WorkspaceFileDeleteRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> WorkspaceFileDeleteRead:
    expected_revision = _expected_revision(if_match)
    mutation = await delete_workspace_file(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        file_id=file_id,
        expected_revision=expected_revision,
        client_request_id=idempotency_key
        or sha256_text(f"delete:{attempt_id}:{file_id}:{expected_revision}"),
        client_context=client_context_from_request(request),
    )
    await db.commit()
    return WorkspaceFileDeleteRead(
        file_id=mutation.file.id,
        revision=mutation.workspace.current_revision,
    )


@router.post(
    "/attempts/{attempt_id}/clipboard-receipts",
    response_model=ClipboardReceiptRead,
    status_code=status.HTTP_201_CREATED,
    tags=["workspace"],
)
async def create_internal_clipboard_receipt(
    attempt_id: uuid.UUID,
    payload: ClipboardReceiptCreateRequest,
    auth: CurrentAuth,
    db: DB,
) -> ClipboardReceiptRead:
    receipt = await create_clipboard_receipt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        source_file_id=payload.file_id,
        text=payload.text,
        revision=payload.revision,
    )
    await db.commit()
    return ClipboardReceiptRead(id=receipt.id, expires_at=receipt.expires_at)


def _edit_event_payload(event: EditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "epoch": event.epoch,
        "sequence": event.sequence,
        "client_id": event.client_id,
        "client_request_id": event.client_request_id,
        "source": event.source,
        "event_type": event.event_type,
        "file_id": event.file_id,
        "changes": event.changes,
        "previous_hash": event.previous_hash,
        "event_hash": event.event_hash,
        "received_at": event.received_at,
        "client": normalize_client_context(event.client_context) or None,
    }


@router.get(
    "/attempts/{attempt_id}/history",
    response_model=list[AttemptHistoryEventRead],
    tags=["attempts"],
)
async def get_attempt_history(
    attempt_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=1_000)] = 500,
    before: Annotated[datetime | None, Query()] = None,
) -> list[AttemptHistoryEventRead]:
    attempt, _assessment, workspace = await _owned_attempt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
    )
    before = _aware(before)
    edit_statement = select(EditEvent).where(EditEvent.workspace_id == workspace.id)
    snapshot_statement = select(Snapshot).where(Snapshot.workspace_id == workspace.id)
    run_statement = select(RunRequest).where(
        RunRequest.attempt_id == attempt.id,
        RunRequest.origin == RunOrigin.STUDENT_ATTEMPT.value,
        RunRequest.requested_by_id == auth.principal_id,
    )
    submission_statement = (
        select(Submission, Snapshot.revision.label("workspace_revision"))
        .join(Snapshot, Snapshot.id == Submission.snapshot_id)
        .where(Submission.attempt_id == attempt.id)
    )
    if before is not None:
        edit_statement = edit_statement.where(EditEvent.received_at < before)
        snapshot_statement = snapshot_statement.where(Snapshot.created_at < before)
        run_statement = run_statement.where(RunRequest.created_at < before)
        submission_statement = submission_statement.where(Submission.submitted_at < before)
    edit_events = list(
        (await db.scalars(edit_statement.order_by(EditEvent.received_at.desc()).limit(limit))).all()
    )
    snapshots = list(
        (
            await db.scalars(snapshot_statement.order_by(Snapshot.created_at.desc()).limit(limit))
        ).all()
    )
    submission_rows = list(
        (
            await db.execute(
                submission_statement.order_by(
                    Submission.submitted_at.desc(),
                    Submission.revision.desc(),
                ).limit(limit)
            )
        ).all()
    )
    runs = list(
        (await db.scalars(run_statement.order_by(RunRequest.created_at.desc()).limit(limit))).all()
    )
    rows: list[AttemptHistoryEventRead] = []
    for event in edit_events:
        internal_paste = event.source == "INTERNAL_PASTE"
        rows.append(
            AttemptHistoryEventRead(
                id=event.id,
                type="internal_paste" if internal_paste else "edit",
                label="Внутренняя вставка" if internal_paste else "Изменение файла",
                detail=event.event_type,
                at=event.received_at,
                revision=event.sequence,
                event=_edit_event_payload(event),
                client=normalize_client_context(event.client_context) or None,
            )
        )
    submission_snapshot_ids = {submission.snapshot_id for submission, _ in submission_rows}
    for snapshot in snapshots:
        if snapshot.id in submission_snapshot_ids:
            continue
        rows.append(
            AttemptHistoryEventRead(
                id=snapshot.id,
                type="snapshot",
                label=(
                    "Начало попытки"
                    if snapshot.reason == "ATTEMPT_STARTED"
                    else "Контрольная точка"
                ),
                detail=snapshot.reason,
                at=snapshot.created_at,
                revision=snapshot.revision,
                client=(
                    normalize_client_context(attempt.client_context) or None
                    if snapshot.reason == "ATTEMPT_STARTED"
                    else None
                ),
            )
        )
    for submission, workspace_revision in submission_rows:
        automatic = submission.source == "DEADLINE"
        rows.append(
            AttemptHistoryEventRead(
                id=submission.id,
                type="submit",
                label="Автоматическая сдача" if automatic else "Сдача работы",
                detail=submission.source,
                at=submission.submitted_at,
                revision=workspace_revision,
                client=normalize_client_context(submission.client_context) or None,
            )
        )
    for run in runs:
        rows.append(
            AttemptHistoryEventRead(
                id=run.id,
                type="run",
                label="Компиляция и запуск",
                detail=run.status,
                at=run.created_at,
                revision=run.revision,
                client=normalize_client_context(run.client_context) or None,
            )
        )
    rows.sort(key=lambda row: (_aware(row.at) or datetime.min.replace(tzinfo=UTC), row.revision))
    return rows[-limit:]


@router.post(
    "/attempts/{attempt_id}/submit",
    response_model=AttemptSubmitRead,
    status_code=status.HTTP_201_CREATED,
    tags=["attempts"],
)
async def finish_attempt(
    attempt_id: uuid.UUID,
    payload: AttemptSubmitRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> AttemptSubmitRead:
    submission = await submit_attempt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        expected_revision=payload.revision,
        client_context=client_context_from_request(request),
    )
    await db.commit()
    return AttemptSubmitRead(
        submission_id=submission.id,
        receipt_id=f"submission:{submission.id}",
        submitted_at=submission.submitted_at,
        revision=payload.revision,
    )


@router.post(
    "/attempts/{attempt_id}/submit/retry",
    response_model=AttemptSubmitRead,
    tags=["attempts"],
)
async def retry_finish_attempt(
    attempt_id: uuid.UUID,
    _payload: AttemptStartRequest,
    auth: CurrentAuth,
    db: DB,
) -> AttemptSubmitRead:
    submission = await retry_submission_checkpoint(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
    )
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        raise DomainError(409, "SUBMISSION_SNAPSHOT_MISSING", "Submission snapshot was not found")
    await db.commit()
    return AttemptSubmitRead(
        submission_id=submission.id,
        receipt_id=f"submission:{submission.id}",
        submitted_at=submission.submitted_at,
        revision=snapshot.revision,
    )


def _mock_result(run_id: uuid.UUID) -> RunnerResult:
    return RunnerResult(
        external_job_id=f"mock-{run_id}",
        status=RunStatus.COMPLETED.value,
        exit_code=0,
        exit_reason="MOCK",
        stdout="Runner mock: исходный код не компилировался и не выполнялся.",
        stderr="",
        diagnostics=[],
        metrics={},
        executor_version="mock",
        filesystem_policy_version="mock-no-execution-v1",
        filesystem_isolated=True,
        network_enabled=False,
    )


def _validated_runner_result(result: RunnerResult) -> RunnerResult:
    if result.status not in {
        RunStatus.COMPLETED.value,
        RunStatus.FAILED.value,
        RunStatus.CANCELLED.value,
    }:
        raise IntegrationProtocolError("Runner returned a non-terminal status")
    if (
        len(result.external_job_id) > 255
        or len(result.executor_version) > 100
        or not result.filesystem_policy_version
        or len(result.filesystem_policy_version) > 100
    ):
        raise IntegrationProtocolError("Runner policy metadata is invalid")
    try:
        payload = RunResultPayloadRead.model_validate(
            {
                "exit_code": result.exit_code,
                "exit_reason": result.exit_reason,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "diagnostics": result.diagnostics,
                "metrics": result.metrics,
            }
        )
    except ValidationError as exc:
        raise IntegrationProtocolError("Runner result does not match the storage schema") from exc
    return RunnerResult(
        external_job_id=result.external_job_id,
        status=result.status,
        exit_code=payload.exit_code,
        exit_reason=payload.exit_reason,
        stdout=payload.stdout,
        stderr=payload.stderr,
        diagnostics=[item.model_dump(mode="json") for item in payload.diagnostics],
        metrics=payload.metrics.model_dump(mode="json"),
        executor_version=result.executor_version,
        filesystem_policy_version=result.filesystem_policy_version,
        filesystem_isolated=result.filesystem_isolated,
        network_enabled=result.network_enabled,
    )


def _unavailable_run_result(run_id: uuid.UUID, code: str) -> RunResult:
    return RunResult(
        run_id=run_id,
        exit_reason=code[:64],
        stderr="Сервис компиляции временно недоступен.",
        diagnostics=[
            {
                "file": None,
                "range": {},
                "severity": "error",
                "code": code[:100],
                "message": "Не удалось запустить сервис компиляции.",
                "notes": [],
                "related": [],
                "fix_its": [],
            }
        ],
        metrics={},
        filesystem_policy_version="unavailable",
        filesystem_isolated=None,
        network_enabled=None,
    )


async def _enforce_run_budget(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    settings: Settings,
) -> int:
    principal = await db.scalar(
        select(ExternalPrincipal).where(ExternalPrincipal.id == principal_id).with_for_update()
    )
    if principal is None:
        raise DomainError(404, "PRINCIPAL_NOT_FOUND", "User was not found")

    now = utcnow()
    stale_seconds = max(
        settings.runner_running_stale_seconds,
        int(settings.runner_http_timeout_seconds) + 15,
    )
    stale_before = now - timedelta(seconds=stale_seconds)
    stale_runs = list(
        (
            await db.scalars(
                select(RunRequest)
                .where(
                    RunRequest.requested_by_id == principal_id,
                    RunRequest.status == RunStatus.RUNNING.value,
                    RunRequest.updated_at < stale_before,
                )
                .with_for_update()
            )
        ).all()
    )
    if stale_runs:
        stale_ids = [run.id for run in stale_runs]
        existing_results = {
            result.run_id: result
            for result in (
                await db.scalars(select(RunResult).where(RunResult.run_id.in_(stale_ids)))
            ).all()
        }
        for stale_run in stale_runs:
            stale_run.status = RunStatus.FAILED.value
            existing = existing_results.get(stale_run.id)
            if existing is None:
                db.add(_unavailable_run_result(stale_run.id, "STALE_RUN_RECOVERED"))
            else:
                existing.exit_reason = "STALE_RUN_RECOVERED"
                existing.stderr = "Запуск прерван до получения подтвержденного результата."
                existing.filesystem_isolated = None
                existing.network_enabled = None
        await db.flush()

    active_count = int(
        await db.scalar(
            select(func.count(RunRequest.id)).where(
                RunRequest.requested_by_id == principal_id,
                RunRequest.status.in_([RunStatus.QUEUED.value, RunStatus.RUNNING.value]),
            )
        )
        or 0
    )
    if active_count >= settings.runner_max_concurrent_runs_per_user:
        raise DomainError(
            429,
            "RUN_CONCURRENCY_LIMIT",
            "Too many compilation requests are already running",
            {"limit": settings.runner_max_concurrent_runs_per_user},
        )

    recent_after = now - timedelta(seconds=settings.runner_rate_limit_window_seconds)
    recent_count = int(
        await db.scalar(
            select(func.count(RunRequest.id)).where(
                RunRequest.requested_by_id == principal_id,
                RunRequest.created_at >= recent_after,
            )
        )
        or 0
    )
    if recent_count >= settings.runner_rate_limit_runs:
        raise DomainError(
            429,
            "RUN_RATE_LIMIT",
            "Compilation request rate limit exceeded",
            {
                "limit": settings.runner_rate_limit_runs,
                "retry_after_seconds": settings.runner_rate_limit_window_seconds,
            },
        )
    return len(stale_runs)


async def _store_run_result(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    result: RunnerResult | None,
    error: IntegrationError | None = None,
) -> RunRequest:
    run = await db.scalar(select(RunRequest).where(RunRequest.id == run_id).with_for_update())
    if run is None:
        raise DomainError(500, "RUN_REQUEST_MISSING", "Run request disappeared")
    existing = await db.scalar(select(RunResult).where(RunResult.run_id == run.id))
    if existing is not None or run.status != RunStatus.RUNNING.value:
        await db.commit()
        return run
    if result is not None:
        try:
            result = _validated_runner_result(result)
        except IntegrationProtocolError as exc:
            result = None
            error = exc
    if result is not None:
        run.status = result.status
        run.external_job_id = result.external_job_id
        db.add(
            RunResult(
                run_id=run.id,
                exit_code=result.exit_code,
                exit_reason=result.exit_reason,
                stdout=result.stdout,
                stderr=result.stderr,
                diagnostics=result.diagnostics,
                metrics=result.metrics,
                executor_version=result.executor_version,
                filesystem_policy_version=result.filesystem_policy_version,
                filesystem_isolated=result.filesystem_isolated,
                network_enabled=result.network_enabled,
            )
        )
    else:
        run.status = RunStatus.FAILED.value
        code = error.code if error is not None else "INTEGRATION_ERROR"
        db.add(_unavailable_run_result(run.id, code))
    await db.commit()
    return run


async def _run_read(db: AsyncSession, run: RunRequest, *, teacher: bool) -> RunStudentRead:
    result = await db.scalar(select(RunResult).where(RunResult.run_id == run.id))
    payload = None
    if result is not None:
        payload = {
            "exit_code": result.exit_code,
            "exit_reason": result.exit_reason,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "diagnostics": result.diagnostics,
            "metrics": result.metrics,
            "completed_at": result.completed_at,
        }
    common = {
        "id": run.id,
        "status": run.status,
        "revision": run.revision,
        "result": payload,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
    }
    if not teacher:
        return RunStudentRead(**common)
    return RunTeacherRead(
        **common,
        origin=run.origin,
        attempt_id=run.attempt_id,
        submission_id=run.submission_id,
        teacher_experiment_id=run.teacher_experiment_id,
        evidence_report_id=run.evidence_report_id,
        evidence_case_index=run.evidence_case_index,
        requested_by_id=run.requested_by_id,
        mode=run.mode,
        build_profile=run.build_profile,
        filesystem_profile=run.filesystem_profile,
        network_enabled=run.network_enabled,
        external_job_id=run.external_job_id,
        actual_executor_version=result.executor_version if result is not None else None,
        actual_filesystem_policy_version=(
            result.filesystem_policy_version if result is not None else None
        ),
        actual_filesystem_isolated=result.filesystem_isolated if result is not None else None,
        actual_network_enabled=result.network_enabled if result is not None else None,
    )


@router.post(
    "/attempts/{attempt_id}/runs",
    response_model=RunStudentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["runs"],
)
async def create_student_run(
    attempt_id: uuid.UUID,
    payload: RunCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> RunStudentRead:
    attempt, _assessment, workspace = await _owned_attempt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        lock=True,
    )
    deadline = _aware(attempt.deadline_at)
    ensure_attempt_not_finalized_in_moodle(attempt)
    if attempt.state != AttemptState.ACTIVE.value or (deadline and utcnow() >= deadline):
        raise DomainError(409, "ATTEMPT_READ_ONLY", "Attempt is read-only")
    if workspace.current_revision != payload.revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Run revision is stale",
            {"current_revision": workspace.current_revision},
        )
    settings: Settings = request.app.state.settings
    flags = await _effective_flags(db, settings)
    if not flags["runner_enabled"]:
        raise DomainError(503, "RUNNER_DISABLED", "Compilation and execution are disabled")
    version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt.assigned_task_version_id
        else None
    )
    if version is None:
        raise DomainError(500, "TASK_VERSION_MISSING", "Assigned task version is missing")
    build_profile = await effective_attempt_build_profile(
        db,
        attempt=attempt,
        configured_profile=version.build_profile,
    )
    files = await _active_files(db, workspace.id)
    source_manifest = [{"path": file.path, "content": file.content} for file in files]
    await _enforce_run_budget(
        db,
        principal_id=auth.principal_id,
        settings=settings,
    )
    run = RunRequest(
        origin=RunOrigin.STUDENT_ATTEMPT.value,
        attempt_id=attempt.id,
        requested_by_id=auth.principal_id,
        revision=workspace.current_revision,
        mode=payload.mode,
        build_profile=build_profile,
        filesystem_profile="UNRESTRICTED_CONTAINER",
        network_enabled=True,
        stdin=payload.stdin,
        status=RunStatus.RUNNING.value,
        client_context=client_context_from_request(request),
    )
    db.add(run)
    await db.commit()

    result: RunnerResult | None = None
    integration_error: IntegrationError | None = None
    try:
        if settings.runner_mock_enabled:
            result = _mock_result(run.id)
        else:
            async with httpx.AsyncClient() as client:
                result = await RunnerAdapter(settings, client).dispatch(
                    request_id=str(run.id),
                    profile_id=build_profile,
                    files=source_manifest,
                    stdin=payload.stdin,
                    mode=payload.mode,
                    limits={
                        "cpu_seconds": flags["runner_cpu_seconds"],
                        "memory_mb": flags["runner_memory_mb"],
                    },
                )
    except IntegrationError as exc:
        integration_error = exc
    run = await _store_run_result(db, run_id=run.id, result=result, error=integration_error)
    return await _run_read(db, run, teacher=False)


def _attempt_interactive_owner(attempt: Attempt, auth: CurrentAuth) -> str:
    """Opaque runner ownership scope; never accept it from the browser."""

    return f"{attempt.id}:{auth.principal_id}"


def _mock_student_interactive() -> InteractiveRunRead:
    return InteractiveRunRead(
        session_id=uuid.uuid4().hex,
        status="SUCCESS",
        terminal=True,
        exit_code=0,
        duration_ms=0,
        stdout="Runner mock: программа не запускалась.",
        stderr="",
        output_truncated=False,
        diagnostics=[],
    )


def _interactive_runner_result(raw: InteractiveRunRead) -> RunnerResult:
    return RunnerResult(
        external_job_id=raw.session_id,
        status=(
            RunStatus.COMPLETED.value
            if raw.status == "SUCCESS"
            else RunStatus.CANCELLED.value
            if raw.status == "STOPPED"
            else RunStatus.FAILED.value
        ),
        exit_code=raw.exit_code,
        exit_reason=raw.status,
        stdout=raw.stdout,
        stderr=raw.stderr,
        diagnostics=[item.model_dump(mode="json") for item in raw.diagnostics],
        metrics={"wall_time_ms": raw.duration_ms},
        executor_version="interactive-runner-v1",
        filesystem_policy_version="unrestricted-container-v1",
        filesystem_isolated=False,
        network_enabled=True,
    )


async def _record_interactive_response(
    db: AsyncSession,
    *,
    run_id: uuid.UUID,
    raw: InteractiveRunRead,
) -> None:
    run = await db.scalar(select(RunRequest).where(RunRequest.id == run_id).with_for_update())
    if run is None:
        raise DomainError(500, "RUN_REQUEST_MISSING", "Run request disappeared")
    if not run.external_job_id:
        run.external_job_id = raw.session_id
    elif run.external_job_id != raw.session_id:
        raise DomainError(409, "INTERACTIVE_SESSION_MISMATCH", "Interactive session mismatch")
    await db.flush()
    if not raw.terminal:
        await db.commit()
        return
    # _store_run_result takes the row lock again in this transaction and is
    # idempotent: concurrent terminal polls observe the existing RunResult.
    await _store_run_result(
        db,
        run_id=run.id,
        result=_interactive_runner_result(raw),
    )


async def _student_interactive_context(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    auth: CurrentAuth,
    revision: int | None = None,
    require_active: bool = False,
    lock_start_parent: bool = False,
) -> tuple[Attempt, Workspace, TaskVersion, list[WorkspaceFile]]:
    attempt, _assessment, workspace = await _owned_attempt(
        db,
        attempt_id=attempt_id,
        principal_id=auth.principal_id,
        # Interactive start reserves the stable attempt/workspace rows until
        # its RUNNING RunRequest is committed.  Concurrent starts therefore
        # cannot both pass the active-session check.  The commit happens
        # before contacting the runner, so no database lock is retained while
        # waiting for the external compiler.  Follow-up commands stay
        # lock-free and re-check state/deadline below.
        lock=lock_start_parent,
    )
    deadline = _aware(attempt.deadline_at)
    if require_active:
        ensure_attempt_not_finalized_in_moodle(attempt)
    if require_active and (
        attempt.state != AttemptState.ACTIVE.value
        or (deadline is not None and utcnow() >= deadline)
    ):
        raise DomainError(409, "ATTEMPT_READ_ONLY", "Attempt is read-only")
    if revision is not None and workspace.current_revision != revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Run revision is stale",
            {"current_revision": workspace.current_revision},
        )
    version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt.assigned_task_version_id
        else None
    )
    if version is None:
        raise DomainError(500, "TASK_VERSION_MISSING", "Assigned task version is missing")
    return attempt, workspace, version, await _active_files(db, workspace.id)


async def _student_interactive_command(
    *,
    action: str,
    attempt_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: AsyncSession,
    text: str | None = None,
) -> InteractiveRunRead:
    require_active = action in {"input", "eof"}
    attempt, _workspace, _version, _files = await _student_interactive_context(
        db,
        attempt_id=attempt_id,
        auth=auth,
        require_active=require_active,
    )
    settings: Settings = request.app.state.settings
    owner_key = _attempt_interactive_owner(attempt, auth)
    run = await db.scalar(
        select(RunRequest).where(
            RunRequest.attempt_id == attempt.id,
            RunRequest.requested_by_id == auth.principal_id,
            RunRequest.origin == RunOrigin.STUDENT_ATTEMPT.value,
            RunRequest.mode == "INTERACTIVE",
            RunRequest.external_job_id == session_id,
        )
    )
    if run is None:
        raise DomainError(404, "INTERACTIVE_SESSION_NOT_FOUND", "Interactive session was not found")
    if action in {"input", "eof"} and run.status != RunStatus.RUNNING.value:
        raise DomainError(409, "INTERACTIVE_SESSION_FINISHED", "Interactive program is not running")
    try:
        async with httpx.AsyncClient() as client:
            adapter = RunnerAdapter(settings, client)
            if action == "state":
                deadline = _aware(attempt.deadline_at)
                read_only = attempt.state != AttemptState.ACTIVE.value or (
                    deadline is not None and utcnow() >= deadline
                )
                raw = (
                    await adapter.interactive_stop(session_id=session_id, owner_key=owner_key)
                    if read_only
                    else await adapter.interactive_state(session_id=session_id, owner_key=owner_key)
                )
            elif action == "input":
                raw = await adapter.interactive_input(
                    session_id=session_id, owner_key=owner_key, text=text or ""
                )
            elif action == "eof":
                raw = await adapter.interactive_eof(session_id=session_id, owner_key=owner_key)
            else:
                raw = await adapter.interactive_stop(session_id=session_id, owner_key=owner_key)
    except IntegrationError as exc:
        raise DomainError(
            503, "RUNNER_UNAVAILABLE", "Сервис компиляции временно недоступен"
        ) from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response


@router.post(
    "/attempts/{attempt_id}/interactive-sessions",
    response_model=InteractiveRunRead,
    status_code=status.HTTP_201_CREATED,
    tags=["runs", "interactive-runs"],
)
async def start_student_interactive(
    attempt_id: uuid.UUID,
    payload: InteractiveRunCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    settings: Settings = request.app.state.settings
    async with _interactive_start_admission_lock(request):
        attempt, workspace, version, files = await _student_interactive_context(
            db,
            attempt_id=attempt_id,
            auth=auth,
            revision=payload.revision,
            require_active=True,
            lock_start_parent=True,
        )
        flags = await _effective_flags(db, settings)
        if not flags["runner_enabled"]:
            raise DomainError(503, "RUNNER_DISABLED", "Compilation and execution are disabled")
        await _enforce_run_budget(db, principal_id=auth.principal_id, settings=settings)
        build_profile = effective_workspace_build_profile(
            version.build_profile,
            multi_file=workspace.multi_file,
        )
        existing_interactive = await db.scalar(
            select(RunRequest).where(
                RunRequest.attempt_id == attempt.id,
                RunRequest.requested_by_id == auth.principal_id,
                RunRequest.origin == RunOrigin.STUDENT_ATTEMPT.value,
                RunRequest.mode == "INTERACTIVE",
                RunRequest.status == RunStatus.RUNNING.value,
            )
        )
        if existing_interactive is not None:
            raise DomainError(
                409,
                "INTERACTIVE_SESSION_ACTIVE",
                "An interactive program is already running for this attempt",
                {"session_id": existing_interactive.external_job_id or None},
            )
        run = RunRequest(
            origin=RunOrigin.STUDENT_ATTEMPT.value,
            attempt_id=attempt.id,
            requested_by_id=auth.principal_id,
            revision=payload.revision,
            mode="INTERACTIVE",
            build_profile=build_profile,
            filesystem_profile="UNRESTRICTED_CONTAINER",
            network_enabled=True,
            stdin="",
            status=RunStatus.RUNNING.value,
            client_context=client_context_from_request(request),
        )
        db.add(run)
        # The RUNNING row becomes visible before the local and database locks
        # are released.  Calling the external runner deliberately happens
        # outside this admission section.
        await db.commit()
    if settings.runner_mock_enabled:
        response = _mock_student_interactive()
        await _record_interactive_response(db, run_id=run.id, raw=response)
        return response
    try:
        async with httpx.AsyncClient() as client:
            raw = await RunnerAdapter(settings, client).start_interactive(
                request_id=str(run.id),
                owner_key=_attempt_interactive_owner(attempt, auth),
                profile_id=build_profile,
                files=[{"path": file.path, "content": file.content} for file in files],
                limits={
                    "cpu_seconds": flags["runner_cpu_seconds"],
                    "memory_mb": flags["runner_memory_mb"],
                },
            )
    except IntegrationError as exc:
        await _store_run_result(db, run_id=run.id, result=None, error=exc)
        raise DomainError(
            503, "RUNNER_UNAVAILABLE", "Сервис компиляции временно недоступен"
        ) from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response


@router.post(
    "/attempts/{attempt_id}/interactive-sessions/{session_id}/state",
    response_model=InteractiveRunRead,
    tags=["runs", "interactive-runs"],
)
async def student_interactive_state(
    attempt_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    return await _student_interactive_command(
        action="state",
        attempt_id=attempt_id,
        session_id=session_id,
        request=request,
        auth=auth,
        db=db,
    )


@router.post(
    "/attempts/{attempt_id}/interactive-sessions/{session_id}/input",
    response_model=InteractiveRunRead,
    tags=["runs", "interactive-runs"],
)
async def student_interactive_input(
    attempt_id: uuid.UUID,
    session_id: str,
    payload: InteractiveRunInputRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    return await _student_interactive_command(
        action="input",
        attempt_id=attempt_id,
        session_id=session_id,
        request=request,
        auth=auth,
        db=db,
        text=payload.text,
    )


@router.post(
    "/attempts/{attempt_id}/interactive-sessions/{session_id}/eof",
    response_model=InteractiveRunRead,
    tags=["runs", "interactive-runs"],
)
async def student_interactive_eof(
    attempt_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    return await _student_interactive_command(
        action="eof",
        attempt_id=attempt_id,
        session_id=session_id,
        request=request,
        auth=auth,
        db=db,
    )


@router.post(
    "/attempts/{attempt_id}/interactive-sessions/{session_id}/stop",
    response_model=InteractiveRunRead,
    tags=["runs", "interactive-runs"],
)
async def stop_student_interactive(
    attempt_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    return await _student_interactive_command(
        action="stop",
        attempt_id=attempt_id,
        session_id=session_id,
        request=request,
        auth=auth,
        db=db,
    )


@router.get(
    "/runs/{run_id}",
    response_model=RunTeacherRead | RunStudentRead,
    tags=["runs"],
)
async def get_run(
    run_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> RunTeacherRead | RunStudentRead:
    run = await db.get(RunRequest, run_id)
    if run is None:
        raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
    if run.origin == RunOrigin.STUDENT_ATTEMPT.value and run.attempt_id is not None:
        attempt = await db.get(Attempt, run.attempt_id)
        if attempt is None or attempt.principal_id != auth.principal_id:
            raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
        return await _run_read(db, run, teacher=False)
    if run.attempt_id is None:
        raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
    if run.origin == RunOrigin.IMMUTABLE_SUBMISSION.value:
        report = (
            await db.get(EvidenceReport, run.evidence_report_id)
            if run.evidence_report_id is not None
            else None
        )
        report_submission = (
            await db.get(Submission, report.submission_id) if report is not None else None
        )
        if (
            report is None
            or run.submission_id is None
            or report.submission_id != run.submission_id
            or report_submission is None
            or report_submission.attempt_id != run.attempt_id
        ):
            raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
        try:
            access = await require_submission_review_access(
                db,
                principal_id=auth.principal_id,
                submission_id=run.submission_id,
                allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
            )
        except DomainError as exc:
            raise DomainError(404, "RUN_NOT_FOUND", "Run was not found") from exc
        if access.attempt.id != run.attempt_id:
            raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
        return await _run_read(db, run, teacher=True)
    elif run.requested_by_id != auth.principal_id:
        # Teacher experiments remain visible only to their owner.
        raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
    if run.submission_id is not None:
        try:
            access = await require_submission_review_access(
                db,
                principal_id=auth.principal_id,
                submission_id=run.submission_id,
            )
        except DomainError as exc:
            raise DomainError(404, "RUN_NOT_FOUND", "Run was not found") from exc
        if access.attempt.id != run.attempt_id:
            raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
        return await _run_read(db, run, teacher=True)
    # Compatibility only for old private rows which predate submission_id.
    attempt = await db.get(Attempt, run.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt is not None else None
    if assessment is None:
        raise DomainError(404, "RUN_NOT_FOUND", "Run was not found")
    try:
        await require_membership(
            db,
            principal_id=auth.principal_id,
            course_id=assessment.course_id,
            role="TEACHER",
        )
    except DomainError as exc:
        raise DomainError(404, "RUN_NOT_FOUND", "Run was not found") from exc
    return await _run_read(db, run, teacher=True)
