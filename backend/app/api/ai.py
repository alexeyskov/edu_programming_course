from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import CurrentAuth
from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.ai import AIAnswer, AIProvider
from app.integrations.errors import (
    IntegrationConfigurationError,
    IntegrationError,
    IntegrationProtocolError,
)
from app.models.attempts import Attempt, Snapshot, Submission, Workspace, WorkspaceFile
from app.models.courses import Course, CourseMembership
from app.models.enums import AttemptState, ChatMode, CourseRole
from app.models.identity import ExternalPrincipal
from app.models.integration import SystemSetting
from app.models.review import ChatMessage, ChatThread
from app.models.tasks import Assessment
from app.schemas.ai import (
    ChatMessageCreateRequest,
    ChatMessageRead,
    ChatThreadRead,
    DocumentationCitationRead,
    StudentAIThreadCreateRequest,
    TeacherAIThreadCreateRequest,
)
from app.schemas.common import EmptyMutation
from app.services.common import DomainError
from app.services.policy import (
    require_decision_support,
    require_membership,
    require_submission_review_access,
    visible_submission_ids_for_review,
)
from app.services.teacher_tokens import teacher_membership_is_authorized
from app.services.workspace import ensure_attempt_not_finalized_in_moodle

router = APIRouter(prefix="/ai", tags=["ai"])
Database = Annotated[AsyncSession, Depends(get_db)]
SYSTEM_SETTINGS_KEY = "effective"


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _domain_error(exc: DomainError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        detail["details"] = exc.details
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


async def _require_role(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    course_id: uuid.UUID,
    role: CourseRole,
) -> None:
    try:
        await require_membership(
            db,
            principal_id=principal_id,
            course_id=course_id,
            role=role,
        )
    except DomainError as exc:
        _domain_error(exc)


async def _role_course_ids(
    db: AsyncSession,
    principal_id: uuid.UUID,
) -> tuple[set[uuid.UUID], set[uuid.UUID]]:
    now = utcnow()
    rows = (
        await db.execute(
            select(CourseMembership.course_id, CourseMembership.role)
            .join(Course, Course.id == CourseMembership.course_id)
            .where(
                CourseMembership.principal_id == principal_id,
                CourseMembership.active.is_(True),
                Course.catalog_enabled.is_(True),
                Course.archived_at.is_(None),
                (CourseMembership.valid_until.is_(None) | (CourseMembership.valid_until > now)),
            )
        )
    ).all()
    roles: dict[uuid.UUID, set[str]] = {}
    for course_id, role in rows:
        roles.setdefault(course_id, set()).add(role)
    student_courses = {
        course_id
        for course_id, projected in roles.items()
        if projected == {CourseRole.STUDENT.value}
    }
    teacher_courses = {
        course_id
        for course_id, projected in roles.items()
        if projected == {CourseRole.TEACHER.value}
    }
    if not await teacher_membership_is_authorized(db, principal_id):
        teacher_courses = set()
    return student_courses, teacher_courses


async def _effective_ai_flags(db: AsyncSession, settings: Settings) -> tuple[bool, bool]:
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == SYSTEM_SETTINGS_KEY))
    configured = settings.ai_mock_enabled or (
        settings.ai_enabled and bool(settings.ai_api_key.get_secret_value())
    )
    values = row.value if row is not None else {}
    globally_enabled = configured and bool(values.get("ai_enabled", True))
    student_enabled = globally_enabled and bool(values.get("student_ai_enabled", True))
    return globally_enabled, student_enabled


def _thread_read(thread: ChatThread) -> ChatThreadRead:
    return ChatThreadRead(
        id=thread.id,
        mode=thread.mode,
        course_id=thread.course_id,
        attempt_id=thread.attempt_id,
        submission_id=thread.submission_id,
        title=thread.title,
        status=thread.status,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _message_read(message: ChatMessage) -> ChatMessageRead:
    citations: list[DocumentationCitationRead] = []
    for item in message.citations:
        if not isinstance(item, dict) or not item.get("title") or not item.get("url"):
            continue
        try:
            citations.append(DocumentationCitationRead.model_validate(item))
        except ValueError:
            continue
    return ChatMessageRead(
        id=message.id,
        thread_id=message.thread_id,
        role=message.role.upper(),
        content=message.content,
        citations=citations,
        safety_outcome=message.safety_outcome,
        created_at=message.created_at,
    )


async def _owned_thread(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    principal_id: uuid.UUID,
    lock: bool = False,
) -> ChatThread:
    statement = select(ChatThread).where(
        ChatThread.id == thread_id,
        ChatThread.owner_id == principal_id,
    )
    if lock:
        statement = statement.with_for_update()
    thread = await db.scalar(statement)
    if thread is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "AI_THREAD_NOT_FOUND", "message": "AI thread was not found"},
        )
    return thread


async def _readable_thread(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    auth: CurrentAuth,
) -> ChatThread:
    statement = select(ChatThread).where(ChatThread.id == thread_id)
    if auth.has_capability("SYSTEM_SETTINGS"):
        statement = statement.where(
            or_(
                ChatThread.owner_id == auth.principal_id,
                ChatThread.mode == ChatMode.TEACHER.value,
            )
        )
    else:
        statement = statement.where(ChatThread.owner_id == auth.principal_id)
    thread = await db.scalar(statement)
    if thread is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "AI_THREAD_NOT_FOUND", "message": "AI thread was not found"},
        )
    if thread.mode == ChatMode.TEACHER.value:
        if thread.submission_id is None:
            raise DomainError(500, "AI_THREAD_CONTEXT_MISSING", "AI thread context is incomplete")
        await require_submission_review_access(
            db,
            principal_id=auth.principal_id,
            submission_id=thread.submission_id,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
        )
    else:
        await _require_role(
            db,
            principal_id=auth.principal_id,
            course_id=thread.course_id,
            role=CourseRole.STUDENT,
        )
    return thread


async def _require_thread_mutation_access(
    db: AsyncSession,
    *,
    thread: ChatThread,
    principal_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> None:
    if thread.mode == ChatMode.TEACHER.value:
        if thread.submission_id is None:
            raise DomainError(500, "AI_THREAD_CONTEXT_MISSING", "AI thread context is incomplete")
        await require_submission_review_access(
            db,
            principal_id=principal_id,
            submission_id=thread.submission_id,
            allow_system_settings_read=allow_system_settings_read,
        )
        return
    await _require_role(
        db,
        principal_id=principal_id,
        course_id=thread.course_id,
        role=CourseRole.STUDENT,
    )


async def _student_context(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    attempt_id: uuid.UUID,
    expected_course_id: uuid.UUID | None,
    settings: Settings,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    attempt = await db.get(Attempt, attempt_id)
    if attempt is None or attempt.principal_id != principal_id:
        raise DomainError(404, "ATTEMPT_NOT_FOUND", "Attempt was not found")
    ensure_attempt_not_finalized_in_moodle(attempt)
    if attempt.state != AttemptState.ACTIVE.value:
        raise DomainError(409, "ATTEMPT_READ_ONLY", "Only an active attempt can use student AI")
    if attempt.deadline_at is not None and utcnow() >= _aware(attempt.deadline_at):
        raise DomainError(409, "DEADLINE_PASSED", "The attempt deadline has passed")
    assessment = await db.get(Assessment, attempt.assessment_id)
    if assessment is None:
        raise DomainError(500, "ASSESSMENT_MISSING", "Attempt assessment is missing")
    if expected_course_id is not None and assessment.course_id != expected_course_id:
        raise DomainError(404, "ATTEMPT_NOT_FOUND", "Attempt was not found in this course")
    await require_membership(
        db,
        principal_id=principal_id,
        course_id=assessment.course_id,
        role=CourseRole.STUDENT,
    )
    globally_enabled, student_enabled = await _effective_ai_flags(db, settings)
    if not globally_enabled or not student_enabled or not assessment.student_ai_enabled:
        raise DomainError(403, "STUDENT_AI_DISABLED", "Student AI help is disabled")
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    if workspace is None:
        raise DomainError(500, "WORKSPACE_MISSING", "Attempt workspace is missing")
    if expected_revision is not None and workspace.current_revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "AI context revision is stale",
            {"current_revision": workspace.current_revision},
        )
    files = list(
        (
            await db.scalars(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == workspace.id,
                    WorkspaceFile.deleted_revision.is_(None),
                )
                .order_by(WorkspaceFile.path)
            )
        ).all()
    )
    return {
        "mode": ChatMode.STUDENT.value,
        "course_id": str(assessment.course_id),
        "assessment": {
            "id": str(assessment.id),
            "title": assessment.title,
            "instructions": assessment.instructions,
        },
        "attempt": {
            "id": str(attempt.id),
            "revision": workspace.current_revision,
            "deadline_at": attempt.deadline_at.isoformat() if attempt.deadline_at else None,
        },
        "files": [
            {
                "path": row.path,
                "language": row.language,
                "content": row.content,
                "content_hash": row.content_hash,
            }
            for row in files
        ],
    }


async def _teacher_context(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    submission_id: uuid.UUID,
    expected_course_id: uuid.UUID | None,
    settings: Settings,
    allow_system_settings_read: bool = False,
) -> dict[str, Any]:
    submission = await db.get(Submission, submission_id)
    attempt = await db.get(Attempt, submission.attempt_id) if submission is not None else None
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt is not None else None
    if submission is None or attempt is None or assessment is None:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
    if expected_course_id is not None and assessment.course_id != expected_course_id:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found in this course")
    await require_submission_review_access(
        db,
        principal_id=principal_id,
        submission_id=submission.id,
        allow_system_settings_read=allow_system_settings_read,
    )
    globally_enabled, _student_enabled = await _effective_ai_flags(db, settings)
    if not globally_enabled or not assessment.teacher_ai_enabled:
        raise DomainError(403, "TEACHER_AI_DISABLED", "Teacher AI help is disabled")
    require_decision_support(assessment)
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")
    return {
        "mode": ChatMode.TEACHER.value,
        "course_id": str(assessment.course_id),
        "assessment": {
            "id": str(assessment.id),
            "title": assessment.title,
            "instructions": assessment.instructions,
        },
        "submission": {
            "id": str(submission.id),
            "revision": submission.revision,
            "submitted_at": submission.submitted_at.isoformat(),
            "manifest_hash": snapshot.manifest_hash,
        },
        "files": [
            {
                "path": str(item.get("path", "")),
                "language": str(item.get("language", "")),
                "content": str(item.get("content", "")),
                "content_hash": str(item.get("content_hash", "")),
            }
            for item in snapshot.files
        ],
    }


async def _thread_context(
    db: AsyncSession,
    *,
    thread: ChatThread,
    principal_id: uuid.UUID,
    settings: Settings,
    allow_system_settings_read: bool = False,
) -> dict[str, Any]:
    if thread.mode == ChatMode.STUDENT.value and thread.attempt_id is not None:
        return await _student_context(
            db,
            principal_id=principal_id,
            attempt_id=thread.attempt_id,
            expected_course_id=thread.course_id,
            settings=settings,
        )
    if thread.mode == ChatMode.TEACHER.value and thread.submission_id is not None:
        return await _teacher_context(
            db,
            principal_id=principal_id,
            submission_id=thread.submission_id,
            expected_course_id=thread.course_id,
            settings=settings,
            allow_system_settings_read=allow_system_settings_read,
        )
    raise DomainError(500, "AI_THREAD_CONTEXT_MISSING", "AI thread context is incomplete")


async def _stored_history(
    db: AsyncSession,
    *,
    thread_id: uuid.UUID,
    settings: Settings,
) -> list[dict[str, str]]:
    limit = settings.ai_history_messages
    if limit <= 0:
        return []
    rows = list(
        (
            await db.scalars(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread_id)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(limit)
            )
        ).all()
    )
    rows.reverse()
    char_limit = max(4_000, settings.ai_max_context_bytes // 2)
    bounded: list[dict[str, str]] = []
    used = 0
    for row in reversed(rows):
        role = row.role.lower()
        if role not in {"user", "assistant"}:
            continue
        remaining = char_limit - used
        if remaining <= 0:
            break
        content = row.content[-remaining:]
        bounded.append({"role": role, "content": content})
        used += len(content)
    bounded.reverse()
    return bounded


async def _enforce_message_budget(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    mode: str,
    settings: Settings,
) -> None:
    principal = await db.scalar(
        select(ExternalPrincipal.id).where(ExternalPrincipal.id == principal_id).with_for_update()
    )
    if principal is None:
        raise DomainError(404, "PRINCIPAL_NOT_FOUND", "User was not found")
    limit = (
        settings.ai_student_rate_limit_messages
        if mode == ChatMode.STUDENT.value
        else settings.ai_teacher_rate_limit_messages
    )
    window = settings.ai_rate_limit_window_seconds
    used = int(
        await db.scalar(
            select(func.count(ChatMessage.id))
            .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
            .where(
                ChatThread.owner_id == principal_id,
                ChatThread.mode == mode,
                ChatMessage.role == "USER",
                ChatMessage.created_at >= utcnow() - timedelta(seconds=window),
            )
        )
        or 0
    )
    if used >= limit:
        raise DomainError(
            429,
            "AI_RATE_LIMIT",
            "AI message rate limit exceeded",
            {"limit": limit, "window_seconds": window},
        )


async def _ask_provider(
    request: Request,
    *,
    mode: str,
    question: str,
    context: dict[str, Any],
    history: list[dict[str, str]],
) -> AIAnswer:
    injected = getattr(request.app.state, "ai_provider", None)
    if injected is not None:
        return await injected.answer(
            mode=mode,
            question=question,
            context=context,
            history=history,
        )
    settings: Settings = request.app.state.settings
    async with httpx.AsyncClient() as client:
        provider = AIProvider(settings, client)
        return await provider.answer(
            mode=mode,
            question=question,
            context=context,
            history=history,
        )


@router.post(
    "/student-threads",
    response_model=ChatThreadRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_student_thread(
    body: StudentAIThreadCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> ChatThreadRead:
    settings: Settings = request.app.state.settings
    try:
        async with db.begin():
            await _student_context(
                db,
                principal_id=auth.principal_id,
                attempt_id=body.attempt,
                expected_course_id=body.course,
                expected_revision=body.revision,
                settings=settings,
            )
            thread = ChatThread(
                owner_id=auth.principal_id,
                mode=ChatMode.STUDENT.value,
                course_id=body.course,
                attempt_id=body.attempt,
                title=body.title,
                policy_version="student-v1",
            )
            db.add(thread)
            await db.flush()
    except DomainError as exc:
        _domain_error(exc)
    return _thread_read(thread)


@router.post(
    "/teacher-threads",
    response_model=ChatThreadRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_teacher_thread(
    body: TeacherAIThreadCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> ChatThreadRead:
    settings: Settings = request.app.state.settings
    try:
        async with db.begin():
            await _teacher_context(
                db,
                principal_id=auth.principal_id,
                submission_id=body.submission,
                expected_course_id=body.course,
                settings=settings,
                allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
            )
            thread = ChatThread(
                owner_id=auth.principal_id,
                mode=ChatMode.TEACHER.value,
                course_id=body.course,
                submission_id=body.submission,
                title=body.title,
                policy_version="teacher-v1",
            )
            db.add(thread)
            await db.flush()
    except DomainError as exc:
        _domain_error(exc)
    return _thread_read(thread)


@router.get("/threads", response_model=list[ChatThreadRead])
async def list_threads(
    auth: CurrentAuth,
    db: Database,
    course_id: Annotated[uuid.UUID | None, Query()] = None,
    mode: Annotated[ChatMode | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[ChatThreadRead]:
    student_courses, _teacher_courses = await _role_course_ids(db, auth.principal_id)
    access_clauses = []
    if student_courses:
        access_clauses.append(
            and_(
                ChatThread.owner_id == auth.principal_id,
                ChatThread.mode == ChatMode.STUDENT.value,
                ChatThread.course_id.in_(student_courses),
            )
        )
    visible_submission_ids = await visible_submission_ids_for_review(
        db,
        principal_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    if visible_submission_ids:
        teacher_clause = and_(
            ChatThread.mode == ChatMode.TEACHER.value,
            ChatThread.submission_id.in_(visible_submission_ids),
        )
        if not auth.has_capability("SYSTEM_SETTINGS"):
            teacher_clause = and_(
                ChatThread.owner_id == auth.principal_id,
                teacher_clause,
            )
        access_clauses.append(teacher_clause)
    if not access_clauses:
        return []
    statement = select(ChatThread).where(or_(*access_clauses))
    if course_id is not None:
        statement = statement.where(ChatThread.course_id == course_id)
    if mode is not None:
        statement = statement.where(ChatThread.mode == mode.value)
    rows = list(
        (
            await db.scalars(
                statement.order_by(ChatThread.updated_at.desc()).limit(limit).offset(offset)
            )
        ).all()
    )
    return [_thread_read(row) for row in rows]


@router.get("/threads/{thread_id}", response_model=ChatThreadRead)
async def get_thread(
    thread_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> ChatThreadRead:
    thread = await _readable_thread(
        db,
        thread_id=thread_id,
        auth=auth,
    )
    return _thread_read(thread)


@router.get("/threads/{thread_id}/messages", response_model=list[ChatMessageRead])
async def list_messages(
    thread_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> list[ChatMessageRead]:
    thread = await _readable_thread(
        db,
        thread_id=thread_id,
        auth=auth,
    )
    rows = list(
        (
            await db.scalars(
                select(ChatMessage)
                .where(ChatMessage.thread_id == thread.id)
                .order_by(ChatMessage.created_at, ChatMessage.id)
            )
        ).all()
    )
    return [_message_read(row) for row in rows]


@router.post("/threads/{thread_id}/messages", response_model=ChatMessageRead)
async def create_message(
    thread_id: uuid.UUID,
    body: ChatMessageCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> ChatMessageRead:
    settings: Settings = request.app.state.settings
    try:
        async with db.begin():
            thread = await _owned_thread(
                db,
                thread_id=thread_id,
                principal_id=auth.principal_id,
                lock=True,
            )
            if thread.status != "OPEN":
                raise DomainError(409, "AI_THREAD_CLOSED", "AI thread is closed")
            await _require_thread_mutation_access(
                db,
                thread=thread,
                principal_id=auth.principal_id,
                allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
            )
            await _enforce_message_budget(
                db,
                principal_id=auth.principal_id,
                mode=thread.mode,
                settings=settings,
            )
            context = await _thread_context(
                db,
                thread=thread,
                principal_id=auth.principal_id,
                settings=settings,
                allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
            )
            history = await _stored_history(db, thread_id=thread.id, settings=settings)
            user_message = ChatMessage(
                thread_id=thread.id,
                role="USER",
                content=body.content,
                citations=[],
                safety_outcome="ALLOWED",
            )
            db.add(user_message)
            await db.flush()
            mode = thread.mode
    except DomainError as exc:
        _domain_error(exc)

    try:
        answer = await _ask_provider(
            request,
            mode=mode,
            question=body.content,
            context=context,
            history=history,
        )
    except IntegrationConfigurationError as exc:
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except IntegrationProtocolError as exc:
        raise HTTPException(
            status_code=502,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except IntegrationError as exc:
        raise HTTPException(
            status_code=503 if exc.retryable else 502,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc

    async with db.begin():
        await _owned_thread(
            db,
            thread_id=thread_id,
            principal_id=auth.principal_id,
            lock=True,
        )
        assistant_message = ChatMessage(
            thread_id=thread_id,
            role="ASSISTANT",
            content=answer.content,
            citations=answer.citations,
            model=answer.model,
            safety_outcome=answer.safety_outcome,
        )
        db.add(assistant_message)
        await db.flush()
    return _message_read(assistant_message)


@router.post("/threads/{thread_id}/close", response_model=ChatThreadRead)
async def close_thread(
    thread_id: uuid.UUID,
    _body: EmptyMutation,
    auth: CurrentAuth,
    db: Database,
) -> ChatThreadRead:
    async with db.begin():
        thread = await _owned_thread(
            db,
            thread_id=thread_id,
            principal_id=auth.principal_id,
            lock=True,
        )
        await _require_thread_mutation_access(
            db,
            thread=thread,
            principal_id=auth.principal_id,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
        )
        if thread.status == "OPEN":
            thread.status = "CLOSED"
            await db.flush()
    return _thread_read(thread)


__all__ = ["router"]
