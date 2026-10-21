"""Authenticated images referenced by the exact condition the viewer may read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.attempts import _owned_attempt
from app.api.authoring import _assessment_access, _student_available
from app.auth.context import CurrentAuth
from app.core.config import Settings
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    CredentialDecryptionError,
    decrypt_moodle_browser_state,
)
from app.db.session import get_db
from app.models.attempts import Attempt
from app.models.courses import Course
from app.models.enums import CourseRole
from app.models.identity import LMSConnection, MoodleCredential
from app.models.tasks import Assessment, TaskVersion
from app.services.common import DomainError
from app.services.moodle_quiz_session import quiz_question_for_attempt
from app.services.policy import require_submission_review_access
from app.services.statement_images import fetch_statement_image, safe_statement_image_url
from app.services.statement_markup import statement_image_urls

router = APIRouter(tags=["statement-images"])
DB = Annotated[AsyncSession, Depends(get_db)]


async def _own_browser_state(
    db: AsyncSession,
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> dict[str, Any] | None:
    credential = await db.scalar(
        select(MoodleCredential).where(
            MoodleCredential.connection_id == connection_id,
            MoodleCredential.principal_id == principal_id,
            MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
        )
    )
    if credential is None:
        return None  # Moodle's TeX images are also commonly public.
    expires = credential.expires_at
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if expires <= datetime.now(UTC):
            return None
    try:
        return decrypt_moodle_browser_state(
            credential.encrypted_secret,
            settings,
            connection_id=connection_id,
            principal_id=principal_id,
        )
    except CredentialDecryptionError:
        return None


@router.get("/statement-images/{kind}/{scope_id}")
async def get_statement_image(
    kind: Literal["attempt", "submission", "assessment"],
    scope_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    url: Annotated[str, Query(min_length=1, max_length=4096)],
) -> Response:
    statements: list[str] = []
    if kind == "attempt":
        attempt, assessment, _ = await _owned_attempt(
            db,
            attempt_id=scope_id,
            principal_id=auth.principal_id,
        )
        relation = await quiz_question_for_attempt(db, attempt.id)
        if relation is not None:
            root = await db.get(Attempt, relation.root_attempt_id)
            if root is None or root.principal_id != auth.principal_id:
                raise DomainError(404, "ATTEMPT_NOT_FOUND", "Attempt was not found")
            parent = await db.get(Assessment, root.assessment_id)
            if parent is not None:
                statements.append(parent.instructions)
        else:
            statements.append(assessment.instructions)
        if attempt.assigned_task_version_id:
            version = await db.get(TaskVersion, attempt.assigned_task_version_id)
            if version is not None:
                statements.append(version.statement)
    elif kind == "submission":
        access = await require_submission_review_access(
            db,
            principal_id=auth.principal_id,
            submission_id=scope_id,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
        )
        attempt, assessment = access.attempt, access.assessment
        version = (
            await db.get(TaskVersion, attempt.assigned_task_version_id)
            if attempt.assigned_task_version_id
            else None
        )
        statements.append(version.statement if version else assessment.instructions)
    else:
        assessment, membership = await _assessment_access(db, auth, scope_id)
        if membership.membership.role != CourseRole.TEACHER.value and not await _student_available(
            db,
            assessment,
            membership,
        ):
            raise DomainError(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found")
        statements.append(assessment.instructions)

    # Merely knowing a URL (or belonging to the same course) must not turn this
    # endpoint into an arbitrary authenticated Moodle file downloader.
    if not any(url in statement_image_urls(statement) for statement in statements):
        raise DomainError(404, "STATEMENT_IMAGE_NOT_FOUND", "Изображение условия недоступно")
    course = await db.get(Course, assessment.course_id)
    connection = await db.get(LMSConnection, course.connection_id) if course else None
    if (
        connection is None
        or not connection.enabled
        or not safe_statement_image_url(
            url,
            connection.base_url,
        )
    ):
        raise DomainError(404, "STATEMENT_IMAGE_NOT_FOUND", "Изображение условия недоступно")
    base_url = connection.base_url
    settings: Settings = request.app.state.settings
    storage_state = await _own_browser_state(
        db,
        settings,
        connection_id=connection.id,
        principal_id=auth.principal_id,
    )
    # This operation does not refresh or lease browser sessions, and must not
    # retain a database transaction while an upstream image is downloading.
    await db.rollback()
    image = await fetch_statement_image(url=url, base_url=base_url, storage_state=storage_state)
    return Response(
        content=image.content,
        media_type=image.media_type,
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
        },
    )
