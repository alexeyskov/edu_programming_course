from __future__ import annotations

import asyncio
import hashlib
import json
import math
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import AuthContext, CurrentAuth, require_capability
from app.core.config import Settings
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    CredentialDecryptionError,
    decrypt_moodle_browser_state,
    decrypt_moodle_credential,
    encrypt_moodle_browser_state,
)
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.errors import (
    IntegrationBusy,
    IntegrationConfigurationError,
    IntegrationError,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from app.integrations.moodle import CourseDiscovery, MoodleBridge
from app.integrations.moodle_modes import moodle_auth_mode, moodle_pluginless_transport
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.integrations.moodle_transport import (
    confirmed_moodle_activity_answer_transport,
    normalize_moodle_essay_answer_transport,
)
from app.models.attempts import Attempt
from app.models.courses import (
    Course,
    CourseGroup,
    CourseImportJob,
    CourseMembership,
    CourseMembershipGroup,
    CourseSection,
)
from app.models.enums import (
    AttemptState,
    CourseImportState,
    CourseRole,
    LMSProvider,
    SyncOutboxState,
)
from app.models.identity import ExternalPrincipal, LMSConnection, MoodleCredential
from app.models.integration import AuditEntry, ExternalMapping, SyncOutbox, SystemSetting
from app.models.tasks import Assessment
from app.schemas.courses import (
    CourseCatalogRead,
    CourseGroupRead,
    CourseImportCreateRequest,
    CourseImportRead,
    CourseSectionRead,
    CourseSyncRequest,
)
from app.services.common import DomainError, positive_decimal
from app.services.course_sync_state import course_sync_stale_before
from app.services.moodle_history import enqueue_historical_submission_imports
from app.services.moodle_materialization import materialize_moodle_activity_drafts
from app.services.moodle_source import (
    moodle_source_confirmation_from_activity,
    moodle_source_is_confirmed,
)
from app.services.policy import (
    MembershipContext,
    publication_group_ids_for_teacher,
    require_membership,
)
from app.services.teacher_tokens import (
    teacher_membership_is_authorized,
    teacher_membership_revision,
    teacher_token_for_principal,
)

router = APIRouter(tags=["courses"])
DBSession = Annotated[AsyncSession, Depends(get_db)]
SystemAdmin = Annotated[AuthContext, Depends(require_capability("SYSTEM_SETTINGS"))]

_MAX_MEMBERS = 20_000
_MAX_SECTIONS = 2_000
_MAX_GROUPS = 5_000
_MAX_POLICIES_BYTES = 128 * 1024
_MAX_ACTIVITIES = 5_000
_LMS_POLICY_KEYS = frozenset({"lms_activities", "lms_activity_revision"})
_SYSTEM_SETTINGS_KEY = "effective"


@dataclass(frozen=True, slots=True)
class _BrowserCredentialLease:
    credential_id: uuid.UUID
    owner: str
    revision: int
    state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _BrowserCredentialSnapshot:
    """Read-only browser state used by an explicit foreground refresh.

    Background imports keep the exclusive lease while they may update Moodle.
    Course discovery itself is read-only, so a user-triggered refresh may use a
    detached snapshot concurrently.  Persisting its refreshed cookies remains
    optimistic: a newer exclusively leased state always wins.
    """

    credential_id: uuid.UUID
    revision: int
    state: dict[str, Any]


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _domain_error(exc: DomainError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": exc.message, **exc.details},
    )


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _bounded(value: object, maximum: int, *, fallback: str = "") -> str:
    text = str(value if value is not None else fallback).strip()
    return text[:maximum]


def _record_sync_error(
    course: Course,
    *,
    code: str,
    message: str,
    retryable: bool,
) -> None:
    """Persist only bounded connector-authored diagnostics, never page bodies."""

    course.sync_status = "FAILED"
    course.sync_error_code = _bounded(code, 64, fallback="SYNC_FAILED") or "SYNC_FAILED"
    course.sync_error_message = _bounded(message, 4000, fallback="LMS synchronization failed")
    course.sync_error_at = utcnow()
    course.sync_error_retryable = retryable


def _clear_sync_error(course: Course) -> None:
    course.sync_error_code = ""
    course.sync_error_message = ""
    course.sync_error_at = None
    course.sync_error_retryable = False


def _epoch(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if number <= 0:
        return None
    try:
        return datetime.fromtimestamp(number, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _url_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        port = parsed.port
    except ValueError:
        return ""
    default_port = 443 if parsed.scheme == "https" else 80
    suffix = f":{port}" if port is not None and port != default_port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{suffix}"


async def _allowed_lms_origins(db: AsyncSession, settings: Settings) -> set[str]:
    row = await db.scalar(select(SystemSetting).where(SystemSetting.key == _SYSTEM_SETTINGS_KEY))
    raw: object = None
    if row is not None and isinstance(row.value, dict):
        raw = row.value.get("allowed_lms_origins")
    if raw is None and settings.moodle_base_url:
        raw = [settings.moodle_base_url]
    if not isinstance(raw, list):
        return set()
    return {origin for item in raw if (origin := _url_origin(str(item)))}


async def _membership(
    db: AsyncSession,
    context: CurrentAuth,
    course_id: uuid.UUID,
    role: CourseRole | None = None,
) -> MembershipContext:
    try:
        return await require_membership(
            db,
            principal_id=context.principal_id,
            course_id=course_id,
            role=role,
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


async def _group_names(db: AsyncSession, membership_id: uuid.UUID) -> list[str]:
    return list(
        (
            await db.scalars(
                select(CourseGroup.name)
                .join(
                    CourseMembershipGroup,
                    CourseMembershipGroup.coursegroup_id == CourseGroup.id,
                )
                .where(
                    CourseMembershipGroup.coursemembership_id == membership_id,
                    CourseGroup.active.is_(True),
                )
                .order_by(CourseGroup.name)
            )
        ).all()
    )


async def _course_payload(
    db: AsyncSession,
    membership: CourseMembership,
    course: Course,
    *,
    teacher: bool,
) -> dict[str, Any]:
    connection = await db.get(LMSConnection, course.connection_id)
    names = await _group_names(db, membership.id)
    external_url = None
    if connection is not None and connection.provider == LMSProvider.MOODLE.value:
        external_url = (
            f"{connection.base_url.rstrip('/')}/course/view.php?"
            f"{urlencode({'id': course.external_id})}"
        )
    payload: dict[str, Any] = {
        "id": course.id,
        "title": course.title,
        "short_name": course.short_name,
        "description": course.description,
        "timezone": course.timezone,
        "role": membership.role,
        "group_name": names[0] if names else None,
        "term": "",
        "provider_name": connection.name if connection else "",
        "external_url": external_url,
        "sync_status": course.sync_status,
        "sync_error_code": course.sync_error_code,
        "sync_error_message": course.sync_error_message,
        "sync_error_at": course.sync_error_at,
        "sync_error_retryable": course.sync_error_retryable,
        "synced_at": membership.synced_at,
        "starts_at": course.starts_at,
        "ends_at": course.ends_at,
        "active_count": None,
        "unchecked_count": None,
    }
    if teacher:
        active_assessments = list(
            (
                await db.scalars(
                    select(Assessment).where(
                        Assessment.course_id == course.id,
                        Assessment.status != "CLOSED",
                    )
                )
            ).all()
        )
        payload.update(
            {
                "connection_id": course.connection_id,
                "external_id": course.external_id,
                "external_revision": course.external_revision,
                "policies": course.policies,
                "archived_at": course.archived_at,
                "created_at": course.created_at,
                "updated_at": course.updated_at,
                "active_count": sum(
                    1
                    for assessment in active_assessments
                    if not (
                        isinstance(assessment.policy, dict)
                        and assessment.policy.get("historical_quiz_split_container") is True
                    )
                ),
            }
        )
    return payload


@router.get("/courses")
async def list_courses(context: CurrentAuth, db: DBSession) -> list[dict[str, Any]]:
    now = utcnow()
    rows = list(
        (
            await db.execute(
                select(CourseMembership, Course)
                .join(Course, Course.id == CourseMembership.course_id)
                .where(
                    CourseMembership.principal_id == context.principal_id,
                    CourseMembership.active.is_(True),
                    or_(
                        CourseMembership.valid_until.is_(None),
                        CourseMembership.valid_until > now,
                    ),
                    Course.archived_at.is_(None),
                    Course.catalog_enabled.is_(True),
                )
                .order_by(Course.title, CourseMembership.role)
            )
        ).all()
    )
    if not await teacher_membership_is_authorized(db, context.principal_id):
        rows = [
            (membership, course)
            for membership, course in rows
            if membership.role != CourseRole.TEACHER.value
        ]
    roles_by_course: dict[uuid.UUID, set[str]] = {}
    for membership, course in rows:
        roles_by_course.setdefault(course.id, set()).add(membership.role)
    if any(len(roles) > 1 for roles in roles_by_course.values()):
        raise _error(
            403,
            "AMBIGUOUS_COURSE_ROLE",
            "The LMS projected more than one active role for a course",
        )
    return [
        await _course_payload(
            db,
            membership,
            course,
            teacher=membership.role == CourseRole.TEACHER.value,
        )
        for membership, course in rows
    ]


def _catalog_payload(course: Course, connection: LMSConnection) -> CourseCatalogRead:
    external_url = (
        f"{connection.base_url.rstrip('/')}/course/view.php?{urlencode({'id': course.external_id})}"
    )
    return CourseCatalogRead(
        id=course.id,
        connection_id=connection.id,
        connection_name=connection.name,
        external_id=course.external_id,
        title=course.title,
        short_name=course.short_name,
        external_url=external_url,
        sync_status=course.sync_status,
        sync_error_code=course.sync_error_code,
        sync_error_message=course.sync_error_message,
        sync_error_at=course.sync_error_at,
        sync_error_retryable=course.sync_error_retryable,
        added_at=course.catalog_added_at,
    )


@router.get("/system/course-catalog", response_model=list[CourseCatalogRead])
async def list_course_catalog(
    _admin: SystemAdmin,
    db: DBSession,
) -> list[CourseCatalogRead]:
    rows = list(
        (
            await db.execute(
                select(Course, LMSConnection)
                .join(LMSConnection, LMSConnection.id == Course.connection_id)
                .where(
                    Course.catalog_enabled.is_(True),
                    Course.archived_at.is_(None),
                    LMSConnection.enabled.is_(True),
                )
                .order_by(Course.title, Course.external_id)
            )
        ).all()
    )
    return [_catalog_payload(course, connection) for course, connection in rows]


@router.put("/system/course-catalog/{course_id}", response_model=CourseCatalogRead)
async def enable_catalog_course(
    course_id: uuid.UUID,
    admin: SystemAdmin,
    request: Request,
    db: DBSession,
) -> CourseCatalogRead:
    course = await db.scalar(select(Course).where(Course.id == course_id).with_for_update())
    if course is None:
        raise _error(404, "COURSE_NOT_FOUND", "Course was not found")
    confirmed_job = await db.scalar(
        select(CourseImportJob.id).where(
            CourseImportJob.confirmed_course_id == course.id,
            CourseImportJob.state == CourseImportState.CONFIRMED.value,
        )
    )
    if confirmed_job is None:
        raise _error(
            409,
            "COURSE_IMPORT_REQUIRED",
            "Course must be discovered and confirmed before it is enabled",
        )
    connection = await db.get(LMSConnection, course.connection_id)
    if connection is None or not connection.enabled:
        raise _error(409, "CONNECTION_DISABLED", "Course LMS connection is disabled")
    if not course.catalog_enabled:
        course.catalog_enabled = True
        course.catalog_added_at = utcnow()
        db.add(
            AuditEntry(
                actor_id=admin.principal_id,
                action="course_catalog.enabled",
                object_type="Course",
                object_id=course.id,
                course_id=course.id,
                request_id=getattr(request.state, "request_id", "")[:100],
                metadata_json={
                    "connection_id": str(connection.id),
                    "external_id": course.external_id,
                },
            )
        )
        await db.commit()
        await db.refresh(course)
    return _catalog_payload(course, connection)


@router.delete(
    "/system/course-catalog/{course_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def disable_catalog_course(
    course_id: uuid.UUID,
    admin: SystemAdmin,
    request: Request,
    db: DBSession,
) -> Response:
    course = await db.scalar(select(Course).where(Course.id == course_id).with_for_update())
    if course is None or not course.catalog_enabled:
        raise _error(404, "COURSE_NOT_FOUND", "Course was not found")
    course.catalog_enabled = False
    course.catalog_added_at = None
    await db.execute(
        update(CourseMembership)
        .where(CourseMembership.course_id == course.id, CourseMembership.active.is_(True))
        .values(active=False, synced_at=utcnow())
    )
    db.add(
        AuditEntry(
            actor_id=admin.principal_id,
            action="course_catalog.disabled",
            object_type="Course",
            object_id=course.id,
            course_id=course.id,
            request_id=getattr(request.state, "request_id", "")[:100],
            metadata_json={
                "connection_id": str(course.connection_id),
                "external_id": course.external_id,
            },
        )
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/courses/{course_id}")
async def get_course(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> dict[str, Any]:
    membership = await _membership(db, context, course_id)
    return await _course_payload(
        db,
        membership.membership,
        membership.course,
        teacher=membership.membership.role == CourseRole.TEACHER.value,
    )


@router.get("/courses/{course_id}/sections", response_model=list[CourseSectionRead])
async def list_sections(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> list[CourseSectionRead]:
    membership = await _membership(db, context, course_id)
    query = select(CourseSection).where(CourseSection.course_id == course_id)
    if membership.membership.role == CourseRole.STUDENT.value:
        query = query.where(CourseSection.visible.is_(True))
    rows = list(
        (await db.scalars(query.order_by(CourseSection.position, CourseSection.title))).all()
    )
    return [CourseSectionRead.model_validate(row) for row in rows]


@router.get(
    "/courses/{course_id}/groups",
    response_model=list[CourseGroupRead],
    response_model_exclude_none=True,
)
async def list_groups(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> list[CourseGroupRead]:
    membership = await _membership(db, context, course_id)
    query = select(CourseGroup).where(CourseGroup.course_id == course_id)
    if membership.membership.role == CourseRole.STUDENT.value:
        query = query.join(
            CourseMembershipGroup,
            CourseMembershipGroup.coursegroup_id == CourseGroup.id,
        ).where(CourseMembershipGroup.coursemembership_id == membership.membership.id)
    elif not context.has_capability("SYSTEM_SETTINGS"):
        allowed_group_ids = await publication_group_ids_for_teacher(
            db,
            principal_id=context.principal_id,
            course_id=course_id,
        )
        query = query.where(CourseGroup.id.in_(allowed_group_ids))
    rows = list((await db.scalars(query.order_by(CourseGroup.name))).all())
    result = [CourseGroupRead.model_validate(row) for row in rows]
    if membership.membership.role == CourseRole.STUDENT.value:
        return [row.model_copy(update={"external_id": None}) for row in result]
    return result


@router.get("/courses/{course_id}/memberships")
async def list_memberships(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> list[dict[str, Any]]:
    requester = await _membership(db, context, course_id)
    query = (
        select(CourseMembership, ExternalPrincipal)
        .join(ExternalPrincipal, ExternalPrincipal.id == CourseMembership.principal_id)
        .where(CourseMembership.course_id == course_id)
    )
    if requester.membership.role != CourseRole.TEACHER.value:
        query = query.where(
            CourseMembership.principal_id == context.principal_id,
            CourseMembership.active.is_(True),
        )
    rows = list((await db.execute(query.order_by(ExternalPrincipal.display_name))).all())
    result: list[dict[str, Any]] = []
    for membership, principal in rows:
        result.append(
            {
                "id": membership.id,
                "principal_id": principal.id,
                "display_name": principal.display_name,
                "role": membership.role,
                "active": membership.active,
                "valid_until": membership.valid_until,
                "groups": await _group_names(db, membership.id),
                "synced_at": membership.synced_at,
            }
        )
    return result


@router.get("/courses/{course_id}/policies")
async def get_course_policies(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> dict[str, Any]:
    membership = await _membership(db, context, course_id, CourseRole.TEACHER)
    return membership.course.policies


@router.get("/courses/{course_id}/lms-activities")
async def list_lms_activities(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> list[dict[str, Any]]:
    """Return the bounded Moodle activity projection used for explicit assessment mapping."""

    membership = await _membership(db, context, course_id, CourseRole.TEACHER)
    rows = membership.course.policies.get("lms_activities", [])
    return rows if isinstance(rows, list) else []


@router.patch("/courses/{course_id}/policies")
async def update_course_policies(
    course_id: uuid.UUID,
    policies: dict[str, Any],
    context: CurrentAuth,
    db: DBSession,
) -> dict[str, Any]:
    membership = await _membership(db, context, course_id, CourseRole.TEACHER)
    if any(key in policies for key in _LMS_POLICY_KEYS):
        raise _error(422, "LMS_PROJECTION_READ_ONLY", "LMS projection fields are read-only")
    try:
        encoded = json.dumps(
            policies,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error(422, "INVALID_COURSE_POLICIES", "Course policies must be valid JSON") from exc
    if len(encoded) > _MAX_POLICIES_BYTES:
        raise _error(413, "COURSE_POLICIES_TOO_LARGE", "Course policies are too large")
    reserved = {
        key: value for key, value in membership.course.policies.items() if key in _LMS_POLICY_KEYS
    }
    membership.course.policies = {**policies, **reserved}
    await db.commit()
    return membership.course.policies


def _job_read(row: CourseImportJob) -> CourseImportRead:
    return CourseImportRead.model_validate(row)


def _browser_lease_seconds(settings: Settings) -> int:
    # The internal HTTP timeout is the hard upper bound of one browser request.
    # A small margin prevents a second request from taking over while httpx is
    # still unwinding a timeout.
    return max(30, min(615, math.ceil(settings.moodle_browser_http_timeout_seconds) + 15))


async def _claim_browser_credential(
    db: AsyncSession,
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> _BrowserCredentialLease:
    now = utcnow()
    credential = await db.scalar(
        select(MoodleCredential)
        .where(
            MoodleCredential.connection_id == connection_id,
            MoodleCredential.principal_id == principal_id,
            MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
        )
        .with_for_update()
    )
    if (
        credential is None
        or credential.status != "ACTIVE"
        or credential.revoked_at is not None
        or (credential.expires_at is not None and _aware(credential.expires_at) <= _aware(now))
    ):
        await db.rollback()
        raise IntegrationConfigurationError("Moodle browser reauthentication is required")
    if (
        credential.lease_owner
        and credential.lease_expires_at is not None
        and _aware(credential.lease_expires_at) > _aware(now)
    ):
        await db.rollback()
        raise IntegrationBusy("Moodle browser session is busy")
    try:
        state = decrypt_moodle_browser_state(
            credential.encrypted_secret,
            settings,
            connection_id=connection_id,
            principal_id=principal_id,
        )
    except CredentialDecryptionError as exc:
        await db.rollback()
        raise IntegrationConfigurationError("Moodle browser reauthentication is required") from exc

    owner = uuid.uuid4().hex
    revision = credential.revision
    lease_until = now + timedelta(seconds=_browser_lease_seconds(settings))
    claimed = await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == credential.id,
            MoodleCredential.revision == revision,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
            or_(MoodleCredential.expires_at.is_(None), MoodleCredential.expires_at > now),
            or_(
                MoodleCredential.lease_owner.is_(None),
                MoodleCredential.lease_expires_at.is_(None),
                MoodleCredential.lease_expires_at <= now,
            ),
        )
        .values(lease_owner=owner, lease_expires_at=lease_until)
        .execution_options(synchronize_session=False)
    )
    if claimed.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise IntegrationBusy("Moodle browser session is busy")
    credential_id = credential.id
    await db.commit()
    return _BrowserCredentialLease(
        credential_id=credential_id,
        owner=owner,
        revision=revision,
        state=state,
    )


async def _release_browser_credential(
    db: AsyncSession,
    lease: _BrowserCredentialLease,
) -> None:
    await db.rollback()
    await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == lease.credential_id,
            MoodleCredential.lease_owner == lease.owner,
        )
        .values(lease_owner=None, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _expire_browser_credential(
    db: AsyncSession,
    lease: _BrowserCredentialLease,
) -> None:
    """Invalidate a browser state which Moodle has explicitly rejected."""

    await db.rollback()
    await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == lease.credential_id,
            MoodleCredential.lease_owner == lease.owner,
        )
        .values(
            status="EXPIRED",
            expires_at=utcnow(),
            lease_owner=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def _store_refreshed_browser_credential(
    db: AsyncSession,
    settings: Settings,
    lease: _BrowserCredentialLease,
    refreshed_state: dict[str, Any],
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> None:
    try:
        encrypted = encrypt_moodle_browser_state(
            refreshed_state,
            settings,
            connection_id=connection_id,
            principal_id=principal_id,
        )
    except ValueError as exc:
        await _release_browser_credential(db, lease)
        raise IntegrationProtocolError("Moodle browser returned an invalid session state") from exc
    now = utcnow()
    try:
        updated = await db.execute(
            update(MoodleCredential)
            .where(
                MoodleCredential.id == lease.credential_id,
                MoodleCredential.lease_owner == lease.owner,
                MoodleCredential.revision == lease.revision,
                MoodleCredential.status == "ACTIVE",
                MoodleCredential.revoked_at.is_(None),
            )
            .values(
                encrypted_secret=encrypted,
                revision=lease.revision + 1,
                lease_owner=None,
                lease_expires_at=None,
                last_used_at=now,
                last_verified_at=now,
            )
            .execution_options(synchronize_session=False)
        )
    except BaseException:
        await asyncio.shield(_release_browser_credential(db, lease))
        raise
    if updated.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        await _release_browser_credential(db, lease)
        raise IntegrationUnavailable("Moodle browser session changed during discovery")
    await db.commit()


async def _read_browser_credential_snapshot(
    db: AsyncSession,
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> _BrowserCredentialSnapshot:
    """Read a valid Moodle state without waiting for a background lease.

    The Playwright service reserves a foreground browser slot for explicit
    course refreshes.  Taking the same exclusive database lease here defeated
    that reservation and made the refresh button fail while historical imports
    were running.  A detached state is safe for discovery because that operation
    only reads Moodle pages.
    """

    now = utcnow()
    credential = await db.scalar(
        select(MoodleCredential).where(
            MoodleCredential.connection_id == connection_id,
            MoodleCredential.principal_id == principal_id,
            MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
        )
    )
    if (
        credential is None
        or credential.status != "ACTIVE"
        or credential.revoked_at is not None
        or (credential.expires_at is not None and _aware(credential.expires_at) <= _aware(now))
    ):
        await db.rollback()
        raise IntegrationConfigurationError("Moodle browser reauthentication is required")
    try:
        state = decrypt_moodle_browser_state(
            credential.encrypted_secret,
            settings,
            connection_id=connection_id,
            principal_id=principal_id,
        )
    except CredentialDecryptionError as exc:
        await db.rollback()
        raise IntegrationConfigurationError("Moodle browser reauthentication is required") from exc
    snapshot = _BrowserCredentialSnapshot(
        credential_id=credential.id,
        revision=credential.revision,
        state=state,
    )
    await db.rollback()
    return snapshot


async def _store_refreshed_browser_snapshot_if_current(
    db: AsyncSession,
    settings: Settings,
    snapshot: _BrowserCredentialSnapshot,
    refreshed_state: dict[str, Any],
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> None:
    """Best-effort CAS for cookies returned by a read-only foreground crawl."""

    try:
        encrypted = encrypt_moodle_browser_state(
            refreshed_state,
            settings,
            connection_id=connection_id,
            principal_id=principal_id,
        )
    except ValueError as exc:
        raise IntegrationProtocolError("Moodle browser returned an invalid session state") from exc
    now = utcnow()
    await db.rollback()
    updated = await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == snapshot.credential_id,
            MoodleCredential.revision == snapshot.revision,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
            or_(MoodleCredential.expires_at.is_(None), MoodleCredential.expires_at > now),
            or_(
                MoodleCredential.lease_owner.is_(None),
                MoodleCredential.lease_expires_at.is_(None),
                MoodleCredential.lease_expires_at <= now,
            ),
        )
        .values(
            encrypted_secret=encrypted,
            revision=snapshot.revision + 1,
            lease_owner=None,
            lease_expires_at=None,
            last_used_at=now,
            last_verified_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount == 1:  # type: ignore[attr-defined]
        await db.commit()
    else:
        # An exclusive background operation refreshed the same credential while
        # discovery was running.  Its newer state is authoritative; the course
        # snapshot obtained by this read-only operation is still valid.
        await db.rollback()


async def _expire_browser_snapshot_if_current(
    db: AsyncSession,
    snapshot: _BrowserCredentialSnapshot,
) -> None:
    """Expire a rejected snapshot without overwriting a concurrent refresh."""

    now = utcnow()
    await db.rollback()
    expired = await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == snapshot.credential_id,
            MoodleCredential.revision == snapshot.revision,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
            or_(
                MoodleCredential.lease_owner.is_(None),
                MoodleCredential.lease_expires_at.is_(None),
                MoodleCredential.lease_expires_at <= now,
            ),
        )
        .values(
            status="EXPIRED",
            expires_at=now,
            lease_owner=None,
            lease_expires_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if expired.rowcount == 1:  # type: ignore[attr-defined]
        await db.commit()
    else:
        await db.rollback()


async def _discover_with_browser_foreground(
    request: Request,
    db: AsyncSession,
    *,
    connection: LMSConnection,
    external_id: str,
    actor_external_subject: str,
    actor_principal_id: uuid.UUID,
) -> CourseDiscovery:
    """Run explicit read-only discovery alongside background Moodle imports."""

    settings = _settings(request)
    connection_id = connection.id
    base_url = connection.base_url
    snapshot = await _read_browser_credential_snapshot(
        db,
        settings,
        connection_id=connection_id,
        principal_id=actor_principal_id,
    )
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            from app.integrations.moodle_browser import MoodleBrowserClient

            try:
                browser = MoodleBrowserClient(
                    settings,
                    client,
                    base_url=base_url,
                    storage_state=snapshot.state,
                )
            except IntegrationProtocolError as exc:
                raise IntegrationConfigurationError(
                    "Moodle browser reauthentication is required"
                ) from exc
            result = await browser.discover_course(
                external_id,
                actor_external_subject,
                interactive=True,
            )
    except MoodleAuthenticationError as exc:
        await asyncio.shield(_expire_browser_snapshot_if_current(db, snapshot))
        raise IntegrationConfigurationError("Moodle browser reauthentication is required") from exc

    await _store_refreshed_browser_snapshot_if_current(
        db,
        settings,
        snapshot,
        result.storage_state,
        connection_id=connection_id,
        principal_id=actor_principal_id,
    )
    return result.discovery


async def _discover_with_browser(
    request: Request,
    db: AsyncSession,
    *,
    connection: LMSConnection,
    external_id: str,
    actor_external_subject: str,
    actor_principal_id: uuid.UUID,
) -> CourseDiscovery:
    settings = _settings(request)
    connection_id = connection.id
    base_url = connection.base_url
    lease = await _claim_browser_credential(
        db,
        settings,
        connection_id=connection_id,
        principal_id=actor_principal_id,
    )
    try:
        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            from app.integrations.moodle_browser import MoodleBrowserClient

            try:
                browser = MoodleBrowserClient(
                    settings,
                    client,
                    base_url=base_url,
                    storage_state=lease.state,
                )
            except IntegrationProtocolError as exc:
                raise IntegrationConfigurationError(
                    "Moodle browser reauthentication is required"
                ) from exc
            result = await browser.discover_course(
                external_id,
                actor_external_subject,
                interactive=True,
            )
    except MoodleAuthenticationError as exc:
        await asyncio.shield(_expire_browser_credential(db, lease))
        raise IntegrationConfigurationError("Moodle browser reauthentication is required") from exc
    except BaseException:
        await asyncio.shield(_release_browser_credential(db, lease))
        raise

    await _store_refreshed_browser_credential(
        db,
        settings,
        lease,
        result.storage_state,
        connection_id=connection_id,
        principal_id=actor_principal_id,
    )
    return result.discovery


async def _discover(
    request: Request,
    db: AsyncSession,
    *,
    connection: LMSConnection,
    external_id: str,
    actor_external_subject: str,
    actor_principal_id: uuid.UUID,
    foreground: bool = False,
) -> CourseDiscovery:
    settings = _settings(request)
    if moodle_auth_mode(connection) == "PLUGINLESS":
        if moodle_pluginless_transport(connection) == "PLAYWRIGHT":
            if foreground:
                return await _discover_with_browser_foreground(
                    request,
                    db,
                    connection=connection,
                    external_id=external_id,
                    actor_external_subject=actor_external_subject,
                    actor_principal_id=actor_principal_id,
                )
            return await _discover_with_browser(
                request,
                db,
                connection=connection,
                external_id=external_id,
                actor_external_subject=actor_external_subject,
                actor_principal_id=actor_principal_id,
            )
        credential = await db.scalar(
            select(MoodleCredential).where(
                MoodleCredential.connection_id == connection.id,
                MoodleCredential.principal_id == actor_principal_id,
                MoodleCredential.kind == "MOBILE_TOKEN",
                MoodleCredential.status == "ACTIVE",
                MoodleCredential.revoked_at.is_(None),
            )
        )
        if credential is None:
            raise IntegrationConfigurationError("Moodle reauthentication is required")
        try:
            token = decrypt_moodle_credential(
                credential.encrypted_secret,
                settings,
                connection_id=connection.id,
                principal_id=actor_principal_id,
                kind=credential.kind,
            )
        except CredentialDecryptionError as exc:
            raise IntegrationConfigurationError("Moodle reauthentication is required") from exc
        base_url = connection.base_url
        await db.rollback()
        from app.integrations.moodle_standard import MoodleStandardClient

        async with httpx.AsyncClient(follow_redirects=False, trust_env=False) as client:
            return await MoodleStandardClient(
                settings,
                client,
                base_url=base_url,
                service_token=token,
            ).discover_course(external_id, actor_external_subject)
    shared_client = getattr(request.app.state, "http_client", None)
    if shared_client is not None:
        bridge = MoodleBridge(settings, shared_client, base_url=connection.base_url)
        return await bridge.discover_course(external_id, actor_external_subject)
    async with httpx.AsyncClient(follow_redirects=False) as client:
        bridge = MoodleBridge(settings, client, base_url=connection.base_url)
        return await bridge.discover_course(external_id, actor_external_subject)


async def _project_course(
    db: AsyncSession,
    *,
    connection: LMSConnection,
    preview: dict[str, Any],
    capabilities: dict[str, Any],
    created_by_id: uuid.UUID,
    actor_external_subject: str,
) -> Course:
    external_id = _bounded(preview.get("external_id"), 255)
    title = _bounded(preview.get("title"), 255)
    if not external_id or not title:
        raise _error(422, "INVALID_COURSE_PREVIEW", "Course preview has no stable id or title")
    # PostgreSQL cannot lock a row that does not exist.  Serialise projections
    # on the stable connector row first so two administrators confirming the
    # same previously unseen course cannot race the (connection, external_id)
    # uniqueness constraint.
    await db.scalar(
        select(LMSConnection.id).where(LMSConnection.id == connection.id).with_for_update()
    )
    course = await db.scalar(
        select(Course)
        .where(
            Course.connection_id == connection.id,
            Course.external_id == external_id,
        )
        .with_for_update()
    )
    if course is None:
        course = Course(
            connection_id=connection.id,
            external_id=external_id,
            title=title,
            catalog_enabled=False,
        )
        db.add(course)
        await db.flush()
    course.title = title
    course.short_name = _bounded(preview.get("short_name"), 120)
    course.external_revision = _bounded(preview.get("external_revision"), 255)
    course.starts_at = _epoch(preview.get("starts_at_epoch"))
    course.ends_at = _epoch(preview.get("ends_at_epoch"))
    course.archived_at = None

    raw_sections = preview.get("sections", [])
    if not isinstance(raw_sections, list) or len(raw_sections) > _MAX_SECTIONS:
        raise _error(422, "INVALID_COURSE_PREVIEW", "Course section snapshot is invalid")
    existing_sections = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseSection).where(CourseSection.course_id == course.id))
        ).all()
    }
    seen_sections: set[str] = set()
    activities: list[dict[str, Any]] = []
    for position, raw in enumerate(raw_sections):
        if not isinstance(raw, dict):
            continue
        section_id = _bounded(raw.get("external_id"), 255)
        section_title = _bounded(raw.get("title"), 255)
        if not section_id or not section_title or section_id in seen_sections:
            continue
        seen_sections.add(section_id)
        section = existing_sections.get(section_id)
        if section is None:
            section = CourseSection(
                course_id=course.id,
                external_id=section_id,
                title=section_title,
            )
            db.add(section)
        section.title = section_title
        raw_position = raw.get("position", position)
        section.position = int(raw_position) if isinstance(raw_position, int) else position
        section.visible = bool(raw.get("visible", True))
        section.external_revision = _bounded(raw.get("external_revision"), 255)
        raw_activities = raw.get("activities", [])
        if not isinstance(raw_activities, list):
            raise _error(422, "INVALID_COURSE_PREVIEW", "Course activities are invalid")
        for activity in raw_activities:
            projected = _project_activity(activity, section_id)
            if projected is not None:
                activities.append(projected)
                if len(activities) > _MAX_ACTIVITIES:
                    raise _error(422, "INVALID_COURSE_PREVIEW", "Course has too many activities")
    for external_section_id, section in existing_sections.items():
        if external_section_id not in seen_sections:
            section.visible = False
    policies = dict(course.policies) if isinstance(course.policies, dict) else {}
    policies["lms_activities"] = [_activity_policy_projection(row) for row in activities]
    policies["lms_activity_revision"] = course.external_revision
    course.policies = policies
    await db.flush()
    await materialize_moodle_activity_drafts(
        db,
        course=course,
        activities=activities,
        created_by_id=created_by_id,
    )
    await _apply_activity_deadlines(db, course, activities)

    snapshot = preview.get("membership_snapshot", {})
    roster_complete = bool(snapshot.get("complete", True)) if isinstance(snapshot, dict) else True
    raw_groups = preview.get("groups", [])
    if not isinstance(raw_groups, list) or len(raw_groups) > _MAX_GROUPS:
        raise _error(422, "INVALID_COURSE_PREVIEW", "Course group snapshot is invalid")
    existing_groups = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseGroup).where(CourseGroup.course_id == course.id))
        ).all()
    }
    group_map: dict[str, CourseGroup] = {}
    for raw in raw_groups:
        if not isinstance(raw, dict):
            continue
        group_id = _bounded(raw.get("external_id") or raw.get("id"), 255)
        group_name = _bounded(raw.get("name"), 255)
        if not group_id or not group_name or group_id in group_map:
            continue
        group = existing_groups.get(group_id)
        if group is None:
            group = CourseGroup(course_id=course.id, external_id=group_id, name=group_name)
            db.add(group)
        group.name = group_name
        group.kind = _bounded(raw.get("kind"), 32, fallback="GROUP") or "GROUP"
        group.active = True
        group_map[group_id] = group
    if roster_complete:
        for external_group_id, group in existing_groups.items():
            if external_group_id not in group_map:
                group.active = False
    await db.flush()

    raw_members = snapshot.get("members", []) if isinstance(snapshot, dict) else None
    if not isinstance(raw_members, list) or len(raw_members) > _MAX_MEMBERS:
        raise _error(422, "INVALID_COURSE_PREVIEW", "Course membership snapshot is invalid")
    membership_revision = _bounded(preview.get("membership_revision"), 255)
    existing_memberships = list(
        (
            await db.scalars(
                select(CourseMembership).where(CourseMembership.course_id == course.id)
            )
        ).all()
    )
    membership_map = {(row.principal_id, row.role): row for row in existing_memberships}
    seen_memberships: set[tuple[uuid.UUID, str]] = set()
    now = utcnow()
    for raw in raw_members:
        if not isinstance(raw, dict):
            continue
        subject = _bounded(raw.get("user_id") or raw.get("username"), 255)
        role_value = _bounded(raw.get("role"), 10).upper()
        if not subject or role_value not in {CourseRole.STUDENT.value, CourseRole.TEACHER.value}:
            continue
        principal = await db.scalar(
            select(ExternalPrincipal).where(
                ExternalPrincipal.connection_id == connection.id,
                ExternalPrincipal.external_subject == subject,
            )
        )
        display_name = _bounded(
            raw.get("display_name", raw.get("fullname", raw.get("username", subject))),
            255,
            fallback=subject,
        )
        if principal is None:
            principal = ExternalPrincipal(
                connection_id=connection.id,
                external_subject=subject,
                display_name=display_name or subject,
                email=_bounded(raw.get("email"), 254),
                active=True,
                profile_revision=membership_revision,
            )
            db.add(principal)
            await db.flush()
        else:
            principal.display_name = display_name or subject
            principal.email = _bounded(raw.get("email"), 254)
            principal.profile_revision = membership_revision
        teacher_token = await teacher_token_for_principal(db, principal.id)
        role_value = (
            CourseRole.TEACHER.value if teacher_token is not None else CourseRole.STUDENT.value
        )
        await db.execute(
            update(CourseMembership)
            .where(
                CourseMembership.course_id == course.id,
                CourseMembership.principal_id == principal.id,
                CourseMembership.role != role_value,
                CourseMembership.active.is_(True),
            )
            .values(active=False, synced_at=now)
        )
        key = (principal.id, role_value)
        membership = membership_map.get(key)
        if membership is None:
            membership = CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role=role_value,
            )
            db.add(membership)
            await db.flush()
            membership_map[key] = membership
        membership.active = not bool(raw.get("suspended", False))
        membership.valid_until = None
        membership.external_revision = (
            teacher_membership_revision(teacher_token.id, membership_revision)
            if teacher_token is not None
            else membership_revision
        )
        membership.synced_at = now
        seen_memberships.add(key)
        existing_membership_group_ids: set[uuid.UUID] = set()
        if roster_complete:
            await db.execute(
                delete(CourseMembershipGroup).where(
                    CourseMembershipGroup.coursemembership_id == membership.id
                )
            )
            await db.flush()
        else:
            # A large Moodle course can return a deliberately bounded roster.
            # The missing rows are not evidence for deletion, but group links
            # present in the returned rows are still authoritative positive
            # evidence and must be merged into the local review scope.
            existing_membership_group_ids = set(
                (
                    await db.scalars(
                        select(CourseMembershipGroup.coursegroup_id).where(
                            CourseMembershipGroup.coursemembership_id == membership.id
                        )
                    )
                ).all()
            )
        member_groups = raw.get("groups", [])
        if membership.active and isinstance(member_groups, list):
            for raw_group in member_groups:
                if not isinstance(raw_group, dict):
                    continue
                group_id = _bounded(raw_group.get("id") or raw_group.get("external_id"), 255)
                group = group_map.get(group_id)
                if group is not None and group.id not in existing_membership_group_ids:
                    existing_membership_group_ids.add(group.id)
                    db.add(
                        CourseMembershipGroup(
                            coursemembership_id=membership.id,
                            coursegroup_id=group.id,
                        )
                    )
    if roster_complete:
        for key, membership in membership_map.items():
            if key not in seen_memberships:
                membership.active = False
                membership.synced_at = now
    connection.capabilities = capabilities
    course.sync_status = "CURRENT"
    _clear_sync_error(course)
    # Queue actor-scoped history crawls only after the fresh teacher roster is
    # projected, otherwise a newly added course would miss its first import.
    await enqueue_historical_submission_imports(
        db,
        course=course,
        actor_external_subject=actor_external_subject,
    )
    await db.flush()
    return course


def _project_activity(raw: object, section_external_id: str) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cmid = raw.get("cmid")
    if isinstance(cmid, bool) or not str(cmid).isdigit() or int(cmid) <= 0:
        return None
    module = _bounded(raw.get("module"), 32).lower()
    if not module or not all(char.isalnum() or char == "_" for char in module):
        return None
    result: dict[str, Any] = {
        "cmid": int(cmid),
        "instance_id": int(raw.get("instance_id") or 0),
        "module": module,
        "name": _bounded(raw.get("name"), 255),
        "visible": bool(raw.get("visible", True)),
        "user_visible": bool(raw.get("uservisible", True)),
        "section_external_id": section_external_id,
        "url": _bounded(raw.get("url"), 2_000),
    }
    for source, target in (
        ("opens_at", "opens_at_epoch"),
        ("due_at", "due_at_epoch"),
        ("cutoff_at", "cutoff_at_epoch"),
    ):
        value = raw.get(source)
        result[target] = int(value) if isinstance(value, int) and value > 0 else 0
    grade = raw.get("grade_max")
    if (
        isinstance(grade, int | float)
        and not isinstance(grade, bool)
        and math.isfinite(float(grade))
    ):
        result["grade_max"] = grade
    description = raw.get("description")
    if isinstance(description, str):
        result["description"] = description.strip()[:50_000]
    for field, maximum in (
        ("duration_seconds", 31_536_000),
        ("attempt_limit", 100),
        ("question_count", 10_000),
        ("essay_question_count", 10_000),
        ("random_question_count", 10_000),
    ):
        value = raw.get(field)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= maximum
            and (field not in {"duration_seconds", "attempt_limit"} or value > 0)
        ):
            result[field] = value
    result["import_supported"] = bool(raw.get("import_supported", False))
    result["random_essay_confirmed"] = raw.get("random_essay_confirmed") is True
    result["statement_deferred"] = raw.get("statement_deferred") is True
    result["attempt_limit_unlimited"] = raw.get("attempt_limit_unlimited") is True
    grading_method = str(raw.get("quiz_grading_method", "")).upper()
    grading_method_confirmed = raw.get(
        "quiz_grading_method_confirmed"
    ) is True and grading_method in {"HIGHEST", "AVERAGE", "FIRST", "LAST"}
    result["quiz_grading_method_confirmed"] = grading_method_confirmed
    if grading_method_confirmed:
        result["quiz_grading_method"] = grading_method
    for field in (
        "title_confirmed",
        "settings_confirmed",
        "statement_confirmed",
        "schedule_confirmed",
        "duration_confirmed",
        "grade_confirmed",
        "attempt_policy_confirmed",
    ):
        result[field] = raw.get(field) is True
    answer_transport = normalize_moodle_essay_answer_transport(raw.get("answer_transport"))
    if answer_transport is not None:
        result["answer_transport"] = answer_transport
    for field in (
        "submission_drafts",
        "requires_submission_statement",
        "team_submission",
        "file_types_confirmed",
        "max_submission_bytes_inherited",
    ):
        if isinstance(raw.get(field), bool):
            result[field] = raw[field]
    for field, maximum in (
        ("max_submission_files", 128),
        ("max_submission_bytes", 4 * 1024 * 1024 * 1024),
    ):
        value = raw.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum:
            result[field] = value
    accepted_file_types = raw.get("accepted_file_types")
    if isinstance(accepted_file_types, str):
        result["accepted_file_types"] = accepted_file_types[:2_000]
    available = raw.get("available_answer_transports")
    if isinstance(available, list):
        normalized_available = [
            transport
            for item in available[:4]
            if (transport := normalize_moodle_essay_answer_transport(item)) is not None
        ]
        if len(normalized_available) == len(set(normalized_available)):
            result["available_answer_transports"] = normalized_available
    return result


def _activity_policy_projection(activity: dict[str, Any]) -> dict[str, Any]:
    """Keep large Moodle descriptions in TaskVersion, not Course.policies."""

    return {key: value for key, value in activity.items() if key != "description"}


def _mapped_moodle_activity(mapping: ExternalMapping) -> tuple[str, str] | None:
    """Return the explicit Moodle activity identity for a supported assessment mapping."""

    if mapping.local_type.lower() not in {"assessment", "core.assessment"}:
        return None
    external_modules = {
        "assign": "assign",
        "mod_assign": "assign",
        "moodle_assignment": "assign",
        "moodle_mod_assign": "assign",
        "quiz": "quiz",
        "mod_quiz": "quiz",
        "moodle_quiz": "quiz",
        "moodle_mod_quiz": "quiz",
    }
    external_type = mapping.external_type.lower().replace("-", "_")
    external_module = external_modules.get(external_type)
    if external_module is None:
        return None
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    raw_module = str(metadata.get("module", external_module)).lower().replace("-", "_")
    module = raw_module.removeprefix("mod_")
    if module != external_module:
        return None
    external_cmid = mapping.external_id
    if not str(external_cmid).isdigit() or int(external_cmid) <= 0:
        return None
    raw_cmid = metadata.get("cmid", external_cmid)
    if isinstance(raw_cmid, bool) or not str(raw_cmid).isdigit() or int(raw_cmid) <= 0:
        return None
    if int(raw_cmid) != int(external_cmid):
        return None
    return module, str(int(raw_cmid))


async def _apply_activity_deadlines(
    db: AsyncSession,
    course: Course,
    activities: list[dict[str, Any]],
) -> None:
    by_activity = {
        (str(item.get("module", "")).removeprefix("mod_"), str(item["cmid"])): item
        for item in activities
        if str(item.get("module", "")).removeprefix("mod_") in {"assign", "quiz"}
        and item.get("cmid")
    }
    sections = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseSection).where(CourseSection.course_id == course.id))
        ).all()
    }
    mappings = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                )
            )
        ).all()
    )
    for mapping in mappings:
        activity_identity = _mapped_moodle_activity(mapping)
        if activity_identity is None:
            continue
        module, cmid = activity_identity
        assessment = await db.get(Assessment, mapping.local_id)
        if assessment is None or assessment.course_id != course.id:
            continue
        metadata = dict(mapping.metadata_json or {})
        activity = by_activity.get((module, cmid))
        mapping.external_revision = course.external_revision
        if activity is None:
            metadata["sync_state"] = "MISSING_IN_MOODLE"
            mapping.metadata_json = metadata
            continue
        answer_transport = confirmed_moodle_activity_answer_transport(
            activity,
            module=module,
        )
        source_confirmation = moodle_source_confirmation_from_activity(activity)
        source_confirmed = moodle_source_is_confirmed(source_confirmation)
        metadata["submission_mode"] = answer_transport or "REQUIRES_CONFIGURATION"
        metadata["moodle_source_confirmation"] = source_confirmation
        sync_state = (
            "CURRENT"
            if answer_transport is not None and source_confirmed
            else (
                "MOODLE_SOURCE_UNCONFIRMED"
                if not source_confirmed
                else "ANSWER_TRANSPORT_UNSUPPORTED"
            )
        )
        if module == "assign":
            grade_max = positive_decimal(activity.get("grade_max"))
            if "grade_max" in activity and grade_max is None:
                sync_state = "UNSUPPORTED_MOODLE_GRADING"
            elif grade_max is not None and grade_max != assessment.max_score:
                sync_state = "GRADE_RANGE_MISMATCH"
        metadata.update({"activity": activity, "sync_state": sync_state})
        mapping.metadata_json = metadata
        if not bool(metadata.get("sync_deadlines", True)):
            continue
        if not source_confirmation["schedule"]:
            continue
        opens_at = _epoch(activity.get("opens_at_epoch"))
        closes_at = _epoch(activity.get("cutoff_at_epoch")) or _epoch(activity.get("due_at_epoch"))
        if opens_at is not None and closes_at is not None and closes_at <= opens_at:
            metadata["sync_state"] = "INVALID_MOODLE_WINDOW"
            mapping.metadata_json = metadata
            continue
        assessment.opens_at = opens_at
        assessment.closes_at = closes_at
        section = sections.get(str(activity.get("section_external_id", "")))
        if section is not None:
            assessment.section_id = section.id
        attempts = list(
            (
                await db.scalars(
                    select(Attempt).where(
                        Attempt.assessment_id == assessment.id,
                        Attempt.state == AttemptState.ACTIVE.value,
                    )
                )
            ).all()
        )
        for attempt in attempts:
            assessment_policy = assessment.policy if isinstance(assessment.policy, dict) else {}
            if assessment_policy.get("moodle_metadata_read_only") is True:
                attempt.deadline_at = None
                continue
            if closes_at is None:
                attempt.deadline_at = attempt.expected_end_at
                continue
            candidates = [closes_at]
            if attempt.expected_end_at is not None:
                candidates.append(attempt.expected_end_at)
            attempt.deadline_at = min(candidates, key=_aware)


@router.post("/courses/{course_id}/sync")
async def sync_course(
    course_id: uuid.UUID,
    _: CourseSyncRequest,
    context: CurrentAuth,
    request: Request,
    db: DBSession,
) -> dict[str, Any]:
    membership = await _membership(db, context, course_id, CourseRole.TEACHER)
    course = membership.course
    connection = await db.get(LMSConnection, course.connection_id)
    if connection is None or not connection.enabled:
        raise _error(409, "CONNECTION_DISABLED", "Course LMS connection is disabled")
    external_id = course.external_id
    actor_subject = membership.principal.external_subject
    connection_id = connection.id
    now = utcnow()
    # Acquire a durable course-level foreground lease with one conditional
    # UPDATE.  Concurrent button presses therefore coalesce instead of issuing
    # multiple browser crawls.  A stale marker is reclaimable by either this
    # endpoint or the scheduler.
    acquired = await db.execute(
        update(Course)
        .where(
            Course.id == course.id,
            or_(
                Course.sync_status != "SYNCING",
                Course.updated_at <= course_sync_stale_before(_settings(request), now),
            ),
        )
        .values(sync_status="SYNCING", updated_at=now)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    await db.refresh(course)
    if acquired.rowcount != 1:  # type: ignore[attr-defined]
        return await _course_payload(
            db,
            membership.membership,
            course,
            teacher=True,
        )

    # A scheduler transaction may have queued the course immediately before
    # the foreground lease became visible.  Reuse that durable request instead
    # of racing it through the same Moodle browser session.  Explicit clicks
    # bring delayed retries forward and the UI polls the SYNCING marker.
    outstanding = await db.scalar(
        select(SyncOutbox.id).where(
            SyncOutbox.course_id == course.id,
            SyncOutbox.event_type == "course.sync",
            SyncOutbox.state.in_(
                [
                    SyncOutboxState.PENDING.value,
                    SyncOutboxState.PROCESSING.value,
                    SyncOutboxState.RETRY.value,
                ]
            ),
        )
    )
    if outstanding is not None:
        await db.execute(
            update(SyncOutbox)
            .where(
                SyncOutbox.course_id == course.id,
                SyncOutbox.event_type == "course.sync",
                SyncOutbox.state.in_([SyncOutboxState.PENDING.value, SyncOutboxState.RETRY.value]),
            )
            .values(next_attempt_at=now)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return await _course_payload(
            db,
            membership.membership,
            course,
            teacher=True,
        )
    try:
        discovery = await _discover(
            request,
            db,
            connection=connection,
            external_id=external_id,
            actor_external_subject=actor_subject,
            actor_principal_id=membership.principal.id,
            foreground=True,
        )
    except IntegrationError as exc:
        row = await db.get(Course, course_id)
        if row is not None:
            _record_sync_error(
                row,
                code=exc.code,
                message=str(exc),
                retryable=exc.retryable,
            )
            await db.commit()
        raise _error(502, exc.code, "LMS synchronization failed") from exc
    connection = await db.get(LMSConnection, connection_id)
    if connection is None:
        row = await db.get(Course, course_id)
        if row is not None:
            _record_sync_error(
                row,
                code="CONNECTION_DISABLED",
                message="Course LMS connection is unavailable",
                retryable=False,
            )
            await db.commit()
        raise _error(409, "CONNECTION_DISABLED", "Course LMS connection is unavailable")
    try:
        projected = await _project_course(
            db,
            connection=connection,
            preview=discovery.preview,
            capabilities=discovery.capabilities,
            created_by_id=context.principal_id,
            actor_external_subject=actor_subject,
        )
        delivered_at = utcnow()
        # Make a successful foreground discovery visible to the periodic
        # scheduler.  Without this durable receipt, the next scheduler tick
        # sees no recent course.sync delivery and immediately queues the same
        # crawl again, often racing the history imports just created above.
        db.add(
            SyncOutbox(
                connection_id=connection_id,
                course_id=projected.id,
                event_type="course.sync",
                aggregate_type="Course",
                aggregate_id=projected.id,
                idempotency_key=(f"course-sync-manual:{projected.id.hex}:{uuid.uuid4().hex[:12]}"),
                payload={"course_id": projected.external_id, "foreground": True},
                state=SyncOutboxState.DELIVERED.value,
                receipt={
                    "status": "DELIVERED",
                    "foreground": True,
                    "external_revision": projected.external_revision,
                },
                delivered_at=delivered_at,
            )
        )
    except Exception:
        await db.rollback()
        row = await db.get(Course, course_id)
        if row is not None:
            _record_sync_error(
                row,
                code="COURSE_PROJECTION_FAILED",
                message="The synchronized Moodle data could not be applied",
                retryable=False,
            )
            await db.commit()
        raise
    await db.commit()
    membership_row = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == projected.id,
            CourseMembership.principal_id == context.principal_id,
            CourseMembership.role == CourseRole.TEACHER.value,
            CourseMembership.active.is_(True),
        )
    )
    if membership_row is None:
        raise _error(403, "COURSE_MEMBERSHIP_REQUIRED", "Teacher membership was removed by LMS")
    return await _course_payload(db, membership_row, projected, teacher=True)


@router.get("/courses/{course_id}/sync-status")
async def course_sync_status(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> dict[str, Any]:
    membership = await _membership(db, context, course_id)
    return {
        "course_id": course_id,
        "status": membership.course.sync_status,
        "updated_at": membership.course.updated_at,
        "error_code": membership.course.sync_error_code,
        "error_message": membership.course.sync_error_message,
        "error_at": membership.course.sync_error_at,
        "retryable": membership.course.sync_error_retryable,
    }


def _can_import(context: CurrentAuth) -> bool:
    return context.has_capability("SYSTEM_SETTINGS")


async def _connection_for_course_url(
    db: AsyncSession,
    request: Request,
    url: str,
) -> tuple[LMSConnection, str]:
    allowed_origins = await _allowed_lms_origins(db, _settings(request))
    connections = list(
        (
            await db.scalars(
                select(LMSConnection).where(
                    LMSConnection.enabled.is_(True),
                    LMSConnection.provider == LMSProvider.MOODLE.value,
                )
            )
        ).all()
    )
    matches: list[tuple[LMSConnection, str]] = []
    async with httpx.AsyncClient(follow_redirects=False) as recognition_client:
        for connection in connections:
            if allowed_origins and _url_origin(connection.base_url) not in allowed_origins:
                continue
            try:
                bridge = MoodleBridge(
                    _settings(request),
                    recognition_client,
                    base_url=connection.base_url,
                )
                external_id = bridge.recognize_course_url(url)
            except IntegrationError:
                continue
            matches.append((connection, external_id))
    if len(matches) != 1:
        raise _error(422, "COURSE_URL_NOT_RECOGNIZED", "Course URL is not uniquely configured")
    return matches[0]


@router.post(
    "/course-imports",
    response_model=CourseImportRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_course_import(
    payload: CourseImportCreateRequest,
    context: CurrentAuth,
    request: Request,
    db: DBSession,
) -> CourseImportRead:
    if not _can_import(context):
        raise _error(403, "SYSTEM_SETTINGS_REQUIRED", "System settings access is required")
    url = str(payload.url)
    connection, external_id = await _connection_for_course_url(db, request, url)
    principal = await db.get(ExternalPrincipal, context.principal_id)
    if principal is None or principal.connection_id != connection.id:
        raise _error(403, "PROVIDER_MISMATCH", "Sign in through the LMS that owns this course")
    job = CourseImportJob(
        connection_id=connection.id,
        requested_by_id=context.principal_id,
        locator_hash=hashlib.sha256(url.encode("utf-8")).hexdigest(),
        external_course_id=external_id,
        state=CourseImportState.DISCOVERED.value,
        preview={},
        capability_report={},
    )
    db.add(job)
    await db.commit()
    job_id = job.id
    try:
        discovery = await _discover(
            request,
            db,
            connection=connection,
            external_id=external_id,
            actor_external_subject=principal.external_subject,
            actor_principal_id=principal.id,
        )
    except IntegrationError as exc:
        job = await db.get(CourseImportJob, job_id)
        if job is not None:
            job.state = CourseImportState.FAILED.value
            job.error = f"{exc.code}: LMS discovery failed"
            await db.commit()
        raise _error(502, exc.code, "LMS course discovery failed") from exc
    job = await db.get(CourseImportJob, job_id)
    if job is None:
        raise _error(409, "IMPORT_CANCELLED", "Course import no longer exists")
    job.preview = discovery.preview
    job.capability_report = discovery.capabilities
    job.error = ""
    await db.commit()
    return _job_read(job)


async def _owned_job(
    db: AsyncSession,
    context: CurrentAuth,
    job_id: uuid.UUID,
    *,
    lock: bool = False,
) -> CourseImportJob:
    query = select(CourseImportJob).where(CourseImportJob.id == job_id)
    if lock:
        query = query.with_for_update()
    job = await db.scalar(query)
    if job is None:
        raise _error(404, "COURSE_IMPORT_NOT_FOUND", "Course import was not found")
    if job.requested_by_id != context.principal_id and not context.has_capability(
        "SYSTEM_SETTINGS"
    ):
        raise _error(403, "COURSE_IMPORT_FORBIDDEN", "Course import belongs to another user")
    return job


@router.get("/course-imports/{job_id}", response_model=CourseImportRead)
async def get_course_import(
    job_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> CourseImportRead:
    return _job_read(await _owned_job(db, context, job_id))


@router.post("/course-imports/{job_id}/confirm", response_model=CourseImportRead)
async def confirm_course_import(
    job_id: uuid.UUID,
    context: CurrentAuth,
    request: Request,
    db: DBSession,
) -> CourseImportRead:
    if not _can_import(context):
        raise _error(403, "SYSTEM_SETTINGS_REQUIRED", "System settings access is required")
    job = await _owned_job(db, context, job_id, lock=True)
    if job.state == CourseImportState.CONFIRMED.value:
        return _job_read(job)
    if job.state != CourseImportState.DISCOVERED.value or not job.preview:
        raise _error(409, "COURSE_IMPORT_NOT_CONFIRMABLE", "Course import cannot be confirmed")
    connection = await db.get(LMSConnection, job.connection_id)
    if connection is None or not connection.enabled:
        raise _error(409, "CONNECTION_DISABLED", "Course LMS connection is disabled")
    discovery_actor = await db.get(ExternalPrincipal, job.requested_by_id)
    if (
        discovery_actor is None
        or discovery_actor.connection_id != connection.id
        or not discovery_actor.active
    ):
        raise _error(
            409,
            "COURSE_IMPORT_ACTOR_UNAVAILABLE",
            "The Moodle account used for course discovery is no longer available",
        )
    course = await _project_course(
        db,
        connection=connection,
        preview=job.preview,
        capabilities=job.capability_report,
        created_by_id=context.principal_id,
        actor_external_subject=discovery_actor.external_subject,
    )
    newly_enabled = not course.catalog_enabled
    course.catalog_enabled = True
    if newly_enabled or course.catalog_added_at is None:
        course.catalog_added_at = utcnow()
    job.confirmed_course_id = course.id
    job.state = CourseImportState.CONFIRMED.value
    if newly_enabled:
        db.add(
            AuditEntry(
                actor_id=context.principal_id,
                action="course_catalog.enabled",
                object_type="Course",
                object_id=course.id,
                course_id=course.id,
                request_id=getattr(request.state, "request_id", "")[:100],
                metadata_json={
                    "connection_id": str(connection.id),
                    "external_id": course.external_id,
                },
            )
        )
    await db.commit()
    return _job_read(job)


@router.post("/course-imports/{job_id}/cancel", response_model=CourseImportRead)
async def cancel_course_import(
    job_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> CourseImportRead:
    job = await _owned_job(db, context, job_id, lock=True)
    if job.state == CourseImportState.CONFIRMED.value:
        raise _error(409, "COURSE_IMPORT_ALREADY_CONFIRMED", "Confirmed import cannot be cancelled")
    if job.state != CourseImportState.CANCELLED.value:
        job.state = CourseImportState.CANCELLED.value
        await db.commit()
    return _job_read(job)
