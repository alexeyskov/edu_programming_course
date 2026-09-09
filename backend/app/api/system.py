from __future__ import annotations

import asyncio
import hashlib
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AuthContext, CurrentAuth, require_capability
from app.core.config import Settings
from app.core.security import hash_teacher_token, verify_teacher_token_hash
from app.core.teacher_token_crypto import (
    TeacherTokenDecryptionError,
    decrypt_teacher_token,
    encrypt_teacher_token,
)
from app.db.base import utcnow
from app.db.session import get_db
from app.models.attempts import Submission
from app.models.courses import Course, CourseMembership, CourseMembershipGroup
from app.models.enums import CourseRole, SyncOutboxState
from app.models.identity import (
    ExternalPrincipal,
    LoginTransaction,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import AuditEntry, SyncOutbox, SystemSetting
from app.models.review import ReviewDecision
from app.schemas.system import (
    ServiceHealthRead,
    SyncOutboxRead,
    SyncOutboxRetryRequest,
    SystemSettingsRead,
    SystemSettingsUpdateRequest,
    TeacherAccessTokenCreateRequest,
    TeacherAccessTokenIssuedRead,
    TeacherAccessTokenRead,
    TeacherAccessTokenSecretRead,
    TeacherAccessTokenUpdateRequest,
)
from app.services.common import DomainError
from app.services.policy import (
    require_membership,
    require_submission_review_access,
    visible_submission_ids_for_review,
)
from app.services.teacher_tokens import (
    generate_teacher_token,
    teacher_membership_is_authorized,
    teacher_membership_prefix,
    teacher_token_public_id,
)

router = APIRouter(tags=["system", "integrations"])
Database = Annotated[AsyncSession, Depends(get_db)]
SystemAdmin = Annotated[AuthContext, Depends(require_capability("SYSTEM_SETTINGS"))]
SYSTEM_SETTINGS_KEY = "effective"
_MAX_TEACHER_TOKENS = 256
_STUDENT_BOUND_OUTBOX_EVENTS = ("attempt.checkpoint", "review.decision")


def _domain_error(exc: DomainError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        detail["details"] = exc.details
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


def _default_values(settings: Settings) -> dict[str, Any]:
    origins = [settings.moodle_base_url] if settings.moodle_base_url else []
    return {
        "ai_enabled": True,
        "student_ai_enabled": True,
        "runner_enabled": True,
        "runner_cpu_seconds": 4,
        "runner_memory_mb": 256,
        "retention_days": 365,
        "allowed_lms_origins": origins,
        "incident_banner": "",
    }


async def _settings_read(db: AsyncSession, settings: Settings) -> SystemSettingsRead:
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == SYSTEM_SETTINGS_KEY))
    values = _default_values(settings)
    if row is not None:
        values.update(row.value)
    ai_configured = settings.ai_mock_enabled or (
        settings.ai_enabled and bool(settings.ai_api_key.get_secret_value())
    )
    runner_configured = settings.runner_mock_enabled or bool(settings.runner_url)
    ai_enabled = ai_configured and bool(values["ai_enabled"])
    student_ai_enabled = ai_enabled and bool(values["student_ai_enabled"])
    runner_enabled = runner_configured and bool(values["runner_enabled"])
    services = [
        ServiceHealthRead(
            name="ai",
            status="ok" if ai_enabled else "down",
            detail="enabled" if ai_enabled else "disabled or not configured",
        ),
        ServiceHealthRead(
            name="runner",
            status="ok" if runner_enabled else "down",
            detail="enabled" if runner_enabled else "disabled or not configured",
        ),
    ]
    return SystemSettingsRead(
        revision=row.revision if row is not None else 1,
        ai_enabled=ai_enabled,
        student_ai_enabled=student_ai_enabled,
        runner_enabled=runner_enabled,
        runner_cpu_seconds=int(values["runner_cpu_seconds"]),
        runner_memory_mb=int(values["runner_memory_mb"]),
        retention_days=int(values["retention_days"]),
        allowed_lms_origins=values["allowed_lms_origins"],
        incident_banner=str(values["incident_banner"]),
        services=services,
    )


@router.get("/system/settings", response_model=SystemSettingsRead)
async def get_system_settings(
    _admin: SystemAdmin,
    request: Request,
    db: Database,
) -> SystemSettingsRead:
    settings: Settings = request.app.state.settings
    return await _settings_read(db, settings)


@router.patch("/system/settings", response_model=SystemSettingsRead)
async def update_system_settings(
    body: SystemSettingsUpdateRequest,
    admin: SystemAdmin,
    request: Request,
    db: Database,
) -> SystemSettingsRead:
    settings: Settings = request.app.state.settings
    try:
        async with db.begin():
            row = await db.scalar(
                select(SystemSetting)
                .where(SystemSetting.key == SYSTEM_SETTINGS_KEY)
                .with_for_update()
            )
            current_revision = row.revision if row is not None else 1
            if body.revision != current_revision:
                raise DomainError(
                    409,
                    "SETTINGS_REVISION_CONFLICT",
                    "System settings revision is stale",
                    {"current_revision": current_revision},
                )
            values = _default_values(settings)
            if row is not None:
                values.update(row.value)
            supplied = body.model_dump(
                mode="json",
                exclude={"revision"},
                exclude_unset=True,
            )
            defaults = _default_values(settings)
            changes = {
                key: defaults[key] if value is None else value for key, value in supplied.items()
            }
            values.update(changes)
            if row is None:
                row = SystemSetting(
                    key=SYSTEM_SETTINGS_KEY,
                    value=values,
                    updated_by_id=admin.principal_id,
                    revision=2,
                )
                db.add(row)
            else:
                row.value = values
                row.updated_by_id = admin.principal_id
                row.revision += 1
            await db.flush()
            db.add(
                AuditEntry(
                    actor_id=admin.principal_id,
                    action="system_settings.updated",
                    object_type="SystemSetting",
                    object_id=row.id,
                    request_id=getattr(request.state, "request_id", "")[:100],
                    metadata_json={"keys": sorted(changes), "revision": row.revision},
                )
            )
            await db.flush()
    except DomainError as exc:
        _domain_error(exc)
    return await _settings_read(db, settings)


def _teacher_token_read(
    token: TeacherAccessToken,
    grant: TeacherTokenGrant | None,
    principal: ExternalPrincipal | None,
) -> TeacherAccessTokenRead:
    return TeacherAccessTokenRead(
        id=token.id,
        label=token.label,
        public_id=token.public_id,
        hash_fingerprint=hashlib.sha256(token.secret_hash.encode("utf-8")).hexdigest()[:16],
        can_reveal=token.encrypted_secret is not None,
        bound_principal_id=grant.principal_id if grant is not None else None,
        bound_display_name=principal.display_name if principal is not None else None,
        use_count=token.use_count,
        last_used_at=token.last_used_at,
        created_at=token.created_at,
    )


async def _teacher_token_rows(
    db: AsyncSession,
) -> list[tuple[TeacherAccessToken, TeacherTokenGrant | None, ExternalPrincipal | None]]:
    return list(
        (
            await db.execute(
                select(TeacherAccessToken, TeacherTokenGrant, ExternalPrincipal)
                .outerjoin(
                    TeacherTokenGrant,
                    TeacherTokenGrant.token_id == TeacherAccessToken.id,
                )
                .outerjoin(
                    ExternalPrincipal,
                    ExternalPrincipal.id == TeacherTokenGrant.principal_id,
                )
                .order_by(TeacherAccessToken.created_at.desc())
            )
        ).all()
    )


@router.get("/system/teacher-tokens", response_model=list[TeacherAccessTokenRead])
async def list_teacher_tokens(
    _admin: SystemAdmin,
    db: Database,
) -> list[TeacherAccessTokenRead]:
    return [_teacher_token_read(*row) for row in await _teacher_token_rows(db)]


def _prevent_secret_caching(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


async def _teacher_token_row(
    db: AsyncSession,
    token_id: uuid.UUID,
) -> tuple[TeacherAccessToken, TeacherTokenGrant | None, ExternalPrincipal | None] | None:
    return (
        await db.execute(
            select(TeacherAccessToken, TeacherTokenGrant, ExternalPrincipal)
            .outerjoin(TeacherTokenGrant, TeacherTokenGrant.token_id == TeacherAccessToken.id)
            .outerjoin(ExternalPrincipal, ExternalPrincipal.id == TeacherTokenGrant.principal_id)
            .where(TeacherAccessToken.id == token_id)
        )
    ).one_or_none()


@router.get(
    "/system/teacher-tokens/{token_id}/secret",
    response_model=TeacherAccessTokenSecretRead,
)
async def reveal_teacher_token(
    token_id: uuid.UUID,
    admin: SystemAdmin,
    request: Request,
    response: Response,
    db: Database,
) -> TeacherAccessTokenSecretRead:
    _prevent_secret_caching(response)
    async with db.begin():
        token = await db.scalar(
            select(TeacherAccessToken).where(TeacherAccessToken.id == token_id).with_for_update()
        )
        if token is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "TEACHER_TOKEN_NOT_FOUND", "message": "Токен не найден"},
            )
        if token.encrypted_secret is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TEACHER_TOKEN_SECRET_UNAVAILABLE",
                    "message": (
                        "Этот токен был создан до появления защищённого хранения. "
                        "Задайте ему новое значение, чтобы его можно было просматривать."
                    ),
                },
            )
        try:
            raw_token = decrypt_teacher_token(
                token.encrypted_secret,
                request.app.state.settings,
                token_id=token.id,
            )
        except TeacherTokenDecryptionError as exc:
            raise HTTPException(
                status_code=500,
                detail={
                    "code": "TEACHER_TOKEN_SECRET_DECRYPTION_FAILED",
                    "message": "Не удалось расшифровать сохранённый токен",
                },
            ) from exc
        db.add(
            AuditEntry(
                actor_id=admin.principal_id,
                action="teacher_token.revealed",
                object_type="TeacherAccessToken",
                object_id=token.id,
                request_id=getattr(request.state, "request_id", "")[:100],
                metadata_json={"public_id": token.public_id},
            )
        )
        await db.flush()
    return TeacherAccessTokenSecretRead(token=raw_token)


@router.post(
    "/system/teacher-tokens",
    response_model=TeacherAccessTokenIssuedRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_teacher_token(
    body: TeacherAccessTokenCreateRequest,
    admin: SystemAdmin,
    request: Request,
    response: Response,
    db: Database,
) -> TeacherAccessTokenIssuedRead:
    _prevent_secret_caching(response)
    public_id, complete_token = generate_teacher_token()
    encoded_hash = await asyncio.to_thread(hash_teacher_token, complete_token)
    token_id = uuid.uuid4()
    encrypted_secret = encrypt_teacher_token(
        complete_token,
        request.app.state.settings,
        token_id=token_id,
    )
    async with db.begin():
        # Every issued token has an ExternalPrincipal creator, so the oldest
        # principal is a stable database-wide mutex row.  A no-op UPDATE takes
        # a PostgreSQL row lock (and SQLite writer lock in tests), serializing
        # count + insert across administrators and application workers.
        mutex_principal_id = await db.scalar(
            select(ExternalPrincipal.id).order_by(ExternalPrincipal.id).limit(1)
        )
        if mutex_principal_id is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TEACHER_TOKEN_CREATOR_NOT_FOUND",
                    "message": "Не найдена учётная запись создателя токена",
                },
            )
        await db.execute(
            update(ExternalPrincipal)
            .where(ExternalPrincipal.id == mutex_principal_id)
            .values(updated_at=ExternalPrincipal.updated_at)
        )
        token_count = int(await db.scalar(select(func.count(TeacherAccessToken.id))) or 0)
        if token_count >= _MAX_TEACHER_TOKENS:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TEACHER_TOKEN_LIMIT_REACHED",
                    "message": "Достигнут предел преподавательских токенов",
                },
            )
        if await db.scalar(
            select(TeacherAccessToken.id).where(TeacherAccessToken.public_id == public_id)
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "TEACHER_TOKEN_SELECTOR_COLLISION",
                    "message": "Не удалось выпустить токен; повторите запрос",
                },
            )
        row = TeacherAccessToken(
            id=token_id,
            public_id=public_id,
            label=body.label,
            secret_hash=encoded_hash,
            encrypted_secret=encrypted_secret,
            created_by_id=admin.principal_id,
        )
        db.add(row)
        await db.flush()
        db.add(
            AuditEntry(
                actor_id=admin.principal_id,
                action="teacher_token.created",
                object_type="TeacherAccessToken",
                object_id=row.id,
                request_id=getattr(request.state, "request_id", "")[:100],
                metadata_json={"label": row.label, "public_id": row.public_id},
            )
        )
        await db.flush()
    base = _teacher_token_read(row, None, None)
    return TeacherAccessTokenIssuedRead(**base.model_dump(), token=complete_token)


@router.patch(
    "/system/teacher-tokens/{token_id}",
    response_model=TeacherAccessTokenRead,
)
async def update_teacher_token(
    token_id: uuid.UUID,
    body: TeacherAccessTokenUpdateRequest,
    admin: SystemAdmin,
    request: Request,
    response: Response,
    db: Database,
) -> TeacherAccessTokenRead:
    _prevent_secret_caching(response)
    complete_token = body.token.get_secret_value()
    public_id = teacher_token_public_id(complete_token)
    if public_id is None:  # Kept as defence in depth behind schema validation.
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_TEACHER_TOKEN",
                "message": "Токен должен состоять ровно из восьми латинских букв или цифр",
            },
        )
    encoded_hash = await asyncio.to_thread(hash_teacher_token, complete_token)
    encrypted_secret = encrypt_teacher_token(
        complete_token,
        request.app.state.settings,
        token_id=token_id,
    )
    try:
        async with db.begin():
            token = await db.scalar(
                select(TeacherAccessToken)
                .where(TeacherAccessToken.id == token_id)
                .with_for_update()
            )
            if token is None:
                raise HTTPException(
                    status_code=404,
                    detail={"code": "TEACHER_TOKEN_NOT_FOUND", "message": "Токен не найден"},
                )
            collision = await db.scalar(
                select(TeacherAccessToken.id).where(
                    TeacherAccessToken.public_id == public_id,
                    TeacherAccessToken.id != token.id,
                )
            )
            if collision is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "TEACHER_TOKEN_SELECTOR_COLLISION",
                        "message": "Такой токен уже существует",
                    },
                )
            old_public_id = token.public_id
            same_secret = await asyncio.to_thread(
                verify_teacher_token_hash,
                complete_token,
                token.secret_hash,
            )
            if not same_secret:
                token.public_id = public_id
                token.secret_hash = encoded_hash
            token.encrypted_secret = encrypted_secret

            # A bridge transaction stores the already-verified token id. Clear
            # unused references so an old secret verified before this rotation
            # cannot grant teacher access after it.
            invalidated_transactions = 0
            if not same_secret:
                invalidated_transactions = int(
                    (
                        await db.execute(
                            update(LoginTransaction)
                            .where(
                                LoginTransaction.teacher_token_id == token.id,
                                LoginTransaction.used_at.is_(None),
                            )
                            .values(teacher_token_id=None)
                        )
                    ).rowcount
                    or 0
                )
            grant = await db.scalar(
                select(TeacherTokenGrant).where(TeacherTokenGrant.token_id == token.id)
            )
            db.add(
                AuditEntry(
                    actor_id=admin.principal_id,
                    action="teacher_token.updated",
                    object_type="TeacherAccessToken",
                    object_id=token.id,
                    request_id=getattr(request.state, "request_id", "")[:100],
                    metadata_json={
                        "old_public_id": old_public_id,
                        "new_public_id": public_id,
                        "secret_changed": not same_secret,
                        "grant_preserved": grant is not None,
                        "invalidated_login_transactions": invalidated_transactions,
                    },
                )
            )
            await db.flush()
    except IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TEACHER_TOKEN_SELECTOR_COLLISION",
                "message": "Такой токен уже существует",
            },
        ) from exc

    result = await _teacher_token_row(db, token_id)
    if result is None:  # The row is locked throughout the update; this is defensive only.
        raise HTTPException(
            status_code=404,
            detail={"code": "TEACHER_TOKEN_NOT_FOUND", "message": "Токен не найден"},
        )
    return _teacher_token_read(*result)


@router.delete(
    "/system/teacher-tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_teacher_token(
    token_id: uuid.UUID,
    admin: SystemAdmin,
    request: Request,
    db: Database,
) -> None:
    async with db.begin():
        token = await db.scalar(
            select(TeacherAccessToken).where(TeacherAccessToken.id == token_id).with_for_update()
        )
        if token is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "TEACHER_TOKEN_NOT_FOUND", "message": "Токен не найден"},
            )
        grants = list(
            (
                await db.scalars(
                    select(TeacherTokenGrant).where(TeacherTokenGrant.token_id == token.id)
                )
            ).all()
        )
        principal_ids = [grant.principal_id for grant in grants]
        demoted = 0
        if principal_ids:
            memberships = list(
                (
                    await db.scalars(
                        select(CourseMembership).where(
                            CourseMembership.principal_id.in_(principal_ids),
                            CourseMembership.role == CourseRole.TEACHER.value,
                            CourseMembership.active.is_(True),
                            CourseMembership.external_revision.startswith(
                                teacher_membership_prefix(token.id)
                            ),
                        )
                    )
                ).all()
            )
            now = utcnow()
            for membership in memberships:
                student = await db.scalar(
                    select(CourseMembership).where(
                        CourseMembership.course_id == membership.course_id,
                        CourseMembership.principal_id == membership.principal_id,
                        CourseMembership.role == CourseRole.STUDENT.value,
                    )
                )
                if student is None:
                    student = CourseMembership(
                        course_id=membership.course_id,
                        principal_id=membership.principal_id,
                        role=CourseRole.STUDENT.value,
                    )
                    db.add(student)
                    await db.flush()
                student.active = True
                student.valid_until = membership.valid_until
                student.external_revision = f"teacher-token-revoked:{token.id}"
                student.synced_at = now
                group_ids = list(
                    (
                        await db.scalars(
                            select(CourseMembershipGroup.coursegroup_id).where(
                                CourseMembershipGroup.coursemembership_id == membership.id
                            )
                        )
                    ).all()
                )
                await db.execute(
                    delete(CourseMembershipGroup).where(
                        CourseMembershipGroup.coursemembership_id == student.id
                    )
                )
                for group_id in group_ids:
                    db.add(
                        CourseMembershipGroup(
                            coursemembership_id=student.id,
                            coursegroup_id=group_id,
                        )
                    )
                membership.active = False
                membership.synced_at = now
                demoted += 1
        await db.execute(delete(TeacherTokenGrant).where(TeacherTokenGrant.token_id == token.id))
        await db.delete(token)
        db.add(
            AuditEntry(
                actor_id=admin.principal_id,
                action="teacher_token.deleted",
                object_type="TeacherAccessToken",
                object_id=token_id,
                request_id=getattr(request.state, "request_id", "")[:100],
                metadata_json={
                    "label": token.label,
                    "public_id": token.public_id,
                    "revoked_grants": len(grants),
                    "demoted_memberships": demoted,
                },
            )
        )
        await db.flush()


async def _teacher_course_ids(db: AsyncSession, principal_id: uuid.UUID) -> set[uuid.UUID]:
    if not await teacher_membership_is_authorized(db, principal_id):
        return set()
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
    return {
        course_id
        for course_id, projected_roles in roles.items()
        if projected_roles == {CourseRole.TEACHER.value}
    }


async def _require_outbox_access(
    db: AsyncSession,
    *,
    auth: AuthContext,
    row: SyncOutbox,
) -> None:
    if auth.has_capability("SYSTEM_SETTINGS"):
        return
    if row.course_id is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
        )
    try:
        await require_membership(
            db,
            principal_id=auth.principal_id,
            course_id=row.course_id,
            role=CourseRole.TEACHER,
        )
    except DomainError as exc:
        raise HTTPException(
            status_code=404,
            detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
        ) from exc
    submission_id: uuid.UUID | None = None
    if row.event_type == "attempt.checkpoint" and row.attempt_id is not None:
        submission_id = await db.scalar(
            select(Submission.id)
            .where(Submission.attempt_id == row.attempt_id)
            .order_by(Submission.revision.desc(), Submission.created_at.desc())
        )
    elif row.event_type == "review.decision" and row.aggregate_type == "ReviewDecision":
        submission_id = await db.scalar(
            select(ReviewDecision.submission_id).where(ReviewDecision.id == row.aggregate_id)
        )
    if row.event_type in _STUDENT_BOUND_OUTBOX_EVENTS:
        if submission_id is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
            )
        try:
            access = await require_submission_review_access(
                db,
                principal_id=auth.principal_id,
                submission_id=submission_id,
            )
        except DomainError as exc:
            raise HTTPException(
                status_code=404,
                detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
            ) from exc
        if access.course.id != row.course_id:
            raise HTTPException(
                status_code=404,
                detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
            )
    if row.event_type == "moodle.history.import":
        principal = await db.get(ExternalPrincipal, auth.principal_id)
        actor = str((row.payload or {}).get("actor_external_subject", ""))
        if principal is None or actor != principal.external_subject:
            raise HTTPException(
                status_code=404,
                detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
            )


def _outbox_read(row: SyncOutbox) -> SyncOutboxRead:
    return SyncOutboxRead(
        id=row.id,
        connection_id=row.connection_id,
        course_id=row.course_id,
        attempt_id=row.attempt_id,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        state=row.state,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_error=row.last_error,
        last_attempt_at=row.last_attempt_at,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        payload=row.payload,
        idempotency_key=row.idempotency_key,
        receipt=row.receipt,
        locked_at=row.locked_at,
    )


@router.get("/integrations/lms/outbox", response_model=list[SyncOutboxRead])
async def list_sync_outbox(
    auth: CurrentAuth,
    db: Database,
    course_id: Annotated[uuid.UUID | None, Query()] = None,
    state: Annotated[SyncOutboxState | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SyncOutboxRead]:
    statement = select(SyncOutbox)
    if not auth.has_capability("SYSTEM_SETTINGS"):
        course_ids = await _teacher_course_ids(db, auth.principal_id)
        if course_id is not None and course_id not in course_ids:
            return []
        if course_id is not None:
            course_ids = {course_id}
        if not course_ids:
            return []
        statement = statement.where(SyncOutbox.course_id.in_(course_ids))
        visible_submission_ids = await visible_submission_ids_for_review(
            db,
            principal_id=auth.principal_id,
        )
        visible_attempt_ids = select(Submission.attempt_id).where(
            Submission.id.in_(visible_submission_ids)
        )
        visible_decision_ids = select(ReviewDecision.id).where(
            ReviewDecision.submission_id.in_(visible_submission_ids)
        )
        student_bound_access = (
            (SyncOutbox.event_type == "attempt.checkpoint")
            & SyncOutbox.attempt_id.in_(visible_attempt_ids)
        ) | (
            (SyncOutbox.event_type == "review.decision")
            & (SyncOutbox.aggregate_type == "ReviewDecision")
            & SyncOutbox.aggregate_id.in_(visible_decision_ids)
        )
        statement = statement.where(
            (~SyncOutbox.event_type.in_(_STUDENT_BOUND_OUTBOX_EVENTS)) | student_bound_access
        )
        principal = await db.get(ExternalPrincipal, auth.principal_id)
        actor_external_subject = principal.external_subject if principal is not None else ""
        statement = statement.where(
            (SyncOutbox.event_type != "moodle.history.import")
            | (SyncOutbox.payload["actor_external_subject"].as_string() == actor_external_subject)
        )
    elif course_id is not None:
        statement = statement.where(SyncOutbox.course_id == course_id)
    if state is not None:
        statement = statement.where(SyncOutbox.state == state.value)
    rows = list(
        (
            await db.scalars(
                statement.order_by(SyncOutbox.created_at.desc()).limit(limit).offset(offset)
            )
        ).all()
    )
    return [_outbox_read(row) for row in rows]


@router.get("/integrations/lms/outbox/{event_id}", response_model=SyncOutboxRead)
async def get_sync_outbox(
    event_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> SyncOutboxRead:
    row = await db.get(SyncOutbox, event_id)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "OUTBOX_NOT_FOUND", "message": "Outbox event was not found"},
        )
    await _require_outbox_access(db, auth=auth, row=row)
    return _outbox_read(row)


async def _ensure_grade_event_current(db: AsyncSession, row: SyncOutbox) -> None:
    if row.aggregate_type != "ReviewDecision":
        return
    decision = await db.get(ReviewDecision, row.aggregate_id)
    if decision is None:
        raise DomainError(409, "GRADE_DECISION_MISSING", "Grade decision no longer exists")
    newer = await db.scalar(
        select(ReviewDecision.id).where(
            ReviewDecision.submission_id == decision.submission_id,
            ReviewDecision.revision > decision.revision,
        )
    )
    if decision.status == "SUPERSEDED" or decision.lms_export_state == "SUPERSEDED" or newer:
        raise DomainError(
            409,
            "GRADE_DECISION_SUPERSEDED",
            "A superseded grade decision cannot be retried",
        )


@router.post("/integrations/lms/outbox/{event_id}/retry", response_model=SyncOutboxRead)
async def retry_sync_outbox(
    event_id: uuid.UUID,
    body: SyncOutboxRetryRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> SyncOutboxRead:
    try:
        async with db.begin():
            row = await db.scalar(
                select(SyncOutbox).where(SyncOutbox.id == event_id).with_for_update()
            )
            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "OUTBOX_NOT_FOUND",
                        "message": "Outbox event was not found",
                    },
                )
            await _require_outbox_access(db, auth=auth, row=row)
            await _ensure_grade_event_current(db, row)
            if row.state in {SyncOutboxState.PENDING.value, SyncOutboxState.RETRY.value}:
                return _outbox_read(row)
            if row.state not in {SyncOutboxState.FAILED.value, SyncOutboxState.BLOCKED.value}:
                raise DomainError(
                    409,
                    "OUTBOX_NOT_RETRYABLE",
                    "This outbox event is not in a retryable state",
                )
            row.state = SyncOutboxState.RETRY.value
            # A deliberate retry starts a fresh delivery budget.  Keeping the
            # exhausted counter made a recovered historical-import page fail
            # again after the very next transient connector error.
            row.attempts = 0
            row.next_attempt_at = utcnow()
            row.locked_at = None
            row.last_error = f"Manual retry: {body.reason}"[:4000]
            db.add(
                AuditEntry(
                    actor_id=auth.principal_id,
                    action="sync_outbox.retry_requested",
                    object_type="SyncOutbox",
                    object_id=row.id,
                    course_id=row.course_id,
                    request_id=getattr(request.state, "request_id", "")[:100],
                    metadata_json={"reason": body.reason},
                )
            )
            await db.flush()
    except DomainError as exc:
        _domain_error(exc)
    return _outbox_read(row)


__all__ = ["router"]
