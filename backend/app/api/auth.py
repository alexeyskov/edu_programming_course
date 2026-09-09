from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import CurrentAuth
from app.auth.login_verifier import (
    clear_login_verifier_cookie,
    create_login_secrets,
    set_login_verifier_cookie,
    verify_login_verifier,
)
from app.auth.sessions import (
    clear_session_cookie,
    create_principal_session,
    revoke_principal_session,
    set_session_cookie,
)
from app.core.config import Settings
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    encrypt_moodle_browser_state,
    encrypt_moodle_credential,
)
from app.core.security import (
    hash_opaque_secret,
    hash_teacher_token,
    verify_admin_token,
    verify_teacher_token_hash,
)
from app.core.teacher_token_crypto import encrypt_teacher_token
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.errors import IntegrationError
from app.integrations.moodle_modes import (
    moodle_auth_mode,
    moodle_login_mode,
    moodle_pluginless_transport,
)
from app.models.attempts import Attempt
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
    CourseSection,
)
from app.models.enums import CourseRole, LMSProvider, SyncOutboxState
from app.models.identity import (
    AdminElevation,
    ExternalPrincipal,
    LMSConnection,
    LoginTransaction,
    MoodleCredential,
    MoodleLoginAttempt,
    TeacherAccessToken,
    UsedLaunchNonce,
)
from app.models.integration import AuditEntry, SyncOutbox
from app.schemas.auth import (
    AdminElevationRequest,
    AuthConnectionRead,
    CourseMembershipRead,
    DevLoginRequest,
    LMSLoginStartRead,
    LMSLoginStartRequest,
    MoodleCredentialLoginRequest,
    PrincipalRead,
    SessionProviderRead,
    SessionRead,
)
from app.services.teacher_tokens import (
    bind_teacher_token,
    generate_teacher_token,
    teacher_membership_is_authorized,
    teacher_membership_revision,
    teacher_token_for_principal,
    teacher_token_public_id,
)

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger(__name__)
DBSession = Annotated[AsyncSession, Depends(get_db)]

_ASSERTION_LIMIT = 8_192
_FORM_LIMIT = 12_288
_NONCE_RE = re.compile(r"^[0-9a-fA-F]{32,128}$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,512}$")
_DUMMY_TEACHER_TOKEN_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$UfNd2ZcKMPzRRLKQfEpZ/Q$"
    "IreckNfDylnbT6EfRZ9scjfcXFBNAqk+P/DFMVMb4N0"
)


def _error(status_code: int, code: str, message: str) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _secret(value: object | None) -> str:
    if value is None:
        return ""
    getter = getattr(value, "get_secret_value", None)
    return str(getter() if getter else value)


async def _verify_teacher_token(
    db: AsyncSession,
    supplied: object | None,
) -> TeacherAccessToken | None:
    raw = _secret(supplied)
    public_id = teacher_token_public_id(raw)
    token = (
        await db.scalar(select(TeacherAccessToken).where(TeacherAccessToken.public_id == public_id))
        if public_id is not None
        else None
    )
    encoded_hash = token.secret_hash if token is not None else _DUMMY_TEACHER_TOKEN_HASH
    valid = await asyncio.to_thread(
        verify_teacher_token_hash,
        raw or "invalid-teacher-token-placeholder",
        encoded_hash,
    )
    return token if valid else None


async def _bind_teacher_token_or_error(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    token_id: uuid.UUID,
    expected_public_id: str | None = None,
    expected_secret_hash: str | None = None,
) -> TeacherAccessToken:
    conditions = [TeacherAccessToken.id == token_id]
    if expected_public_id is not None:
        conditions.append(TeacherAccessToken.public_id == expected_public_id)
    if expected_secret_hash is not None:
        conditions.append(TeacherAccessToken.secret_hash == expected_secret_hash)
    token = await db.scalar(select(TeacherAccessToken).where(*conditions).with_for_update())
    if token is None:
        raise _error(
            409,
            "TEACHER_TOKEN_REVOKED",
            "Токен преподавателя был отозван или изменён; повторите вход",
        )
    try:
        async with db.begin_nested():
            await bind_teacher_token(db, principal_id=principal_id, token=token)
    except ValueError as exc:
        if str(exc) == "PRINCIPAL_ALREADY_BOUND":
            raise _error(
                409,
                "TEACHER_PRINCIPAL_ALREADY_BOUND",
                "Эта учётная запись уже привязана к другому токену преподавателя",
            ) from exc
        raise _error(
            409,
            "TEACHER_TOKEN_ALREADY_BOUND",
            "Этот токен преподавателя уже привязан к другой учётной записи",
        ) from exc
    except IntegrityError as exc:
        raise _error(
            409,
            "TEACHER_TOKEN_ALREADY_BOUND",
            "Токен или учётная запись уже имеют другую привязку преподавателя",
        ) from exc
    return token


async def _resume_outbox_after_moodle_reauthentication(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> int:
    """Resume only deliveries which were stopped by an expired Moodle session.

    A terminal checkpoint uses a stable idempotency key, so enqueueing it again
    after a fresh login would merely return the existing FAILED/BLOCKED row.
    Reactivating that row is therefore part of completing reauthentication.
    Unrelated protocol and configuration failures remain terminal.
    """

    now = utcnow()
    principal = await db.get(ExternalPrincipal, principal_id)
    actor_external_subject = principal.external_subject if principal is not None else ""
    attempt_ids = select(Attempt.id).where(Attempt.principal_id == principal_id)
    teacher_course_ids = (
        select(CourseMembership.course_id)
        .join(Course, Course.id == CourseMembership.course_id)
        .where(
            CourseMembership.principal_id == principal_id,
            CourseMembership.role == CourseRole.TEACHER.value,
            CourseMembership.active.is_(True),
            Course.catalog_enabled.is_(True),
            Course.archived_at.is_(None),
        )
    )
    if not await teacher_membership_is_authorized(db, principal_id):
        teacher_course_ids = select(CourseMembership.course_id).where(CourseMembership.id.is_(None))
    resumable_error = or_(
        SyncOutbox.last_error.startswith("LMS_REAUTH_REQUIRED:"),
        SyncOutbox.last_error.startswith("MOODLE_AUTHENTICATION_FAILED:"),
    )
    resumed = await db.execute(
        update(SyncOutbox)
        .where(
            SyncOutbox.connection_id == connection_id,
            SyncOutbox.state.in_([SyncOutboxState.BLOCKED.value, SyncOutboxState.FAILED.value]),
            resumable_error,
            or_(
                and_(
                    SyncOutbox.event_type == "attempt.checkpoint",
                    SyncOutbox.attempt_id.in_(attempt_ids),
                ),
                and_(
                    SyncOutbox.event_type == "course.sync",
                    SyncOutbox.course_id.in_(teacher_course_ids),
                ),
                and_(
                    SyncOutbox.event_type == "moodle.history.import",
                    SyncOutbox.course_id.in_(teacher_course_ids),
                    SyncOutbox.payload["actor_external_subject"].as_string()
                    == actor_external_subject,
                ),
            ),
        )
        .values(
            state=SyncOutboxState.PENDING.value,
            attempts=0,
            next_attempt_at=now,
            last_error="",
            locked_at=None,
            delivered_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    return int(resumed.rowcount or 0)  # type: ignore[attr-defined]


def _request_ip_prefix(request: Request) -> str:
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    bits = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


def _login_dimension_hash(settings: Settings, namespace: str, value: str) -> str:
    key = settings.secret_key.get_secret_value().encode("utf-8")
    normalized = value.strip().casefold().encode("utf-8")
    return hmac.new(key, namespace.encode("ascii") + b"\0" + normalized, hashlib.sha256).hexdigest()


def _login_semaphore(request: Request, settings: Settings) -> asyncio.Semaphore:
    semaphore = getattr(request.app.state, "moodle_login_semaphore", None)
    if semaphore is None:
        semaphore = asyncio.Semaphore(settings.moodle_max_concurrent_logins_per_worker)
        request.app.state.moodle_login_semaphore = semaphore
    return semaphore


def _login_admission_lock(request: Request) -> asyncio.Lock:
    """Serialize the short rate-limit transaction inside one ASGI worker.

    The database row lock acquired below is authoritative across workers. This
    process-local guard is also needed for SQLite's shared in-memory connection
    in tests and avoids needless lock retries for production requests handled by
    the same worker.
    """

    lock = getattr(request.app.state, "moodle_login_admission_lock", None)
    if lock is None:
        lock = asyncio.Lock()
        request.app.state.moodle_login_admission_lock = lock
    return lock


def _url_origin(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower() if parsed.hostname else ""
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        return None
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, hostname, port


def _require_direct_launch_origin(request: Request, *, expected_base_url: str) -> None:
    """Bind a state-less browser launch to the configured LMS web origin."""

    expected = _url_origin(expected_base_url)
    supplied = _url_origin(request.headers.get("origin", ""))
    if supplied is None:
        supplied = _url_origin(request.headers.get("referer", ""))
    if expected is None or supplied != expected:
        raise _error(
            403,
            "INVALID_LAUNCH_ORIGIN",
            "Direct Moodle launch did not originate from the configured LMS",
        )


async def _membership_group_name(db: AsyncSession, membership_id: uuid.UUID) -> str | None:
    return await db.scalar(
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
        .limit(1)
    )


async def _session_read(
    db: AsyncSession,
    principal_id: uuid.UUID,
    *,
    capabilities: tuple[str, ...] = (),
    elevation_expires_at: datetime | None = None,
) -> SessionRead:
    principal = await db.get(ExternalPrincipal, principal_id)
    if principal is None or not principal.active:
        raise _error(401, "AUTHENTICATION_REQUIRED", "Authentication required")
    connection = await db.get(LMSConnection, principal.connection_id)
    if connection is None or not connection.enabled:
        raise _error(401, "AUTH_PROVIDER_DISABLED", "The authentication provider is disabled")
    now = utcnow()
    rows = list(
        (
            await db.execute(
                select(CourseMembership, Course)
                .join(Course, Course.id == CourseMembership.course_id)
                .where(
                    CourseMembership.principal_id == principal.id,
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
    if not await teacher_membership_is_authorized(db, principal.id):
        rows = [
            (membership, course)
            for membership, course in rows
            if membership.role != CourseRole.TEACHER.value
        ]
    memberships: list[CourseMembershipRead] = []
    for membership, course in rows:
        memberships.append(
            CourseMembershipRead(
                course_id=course.id,
                course_name=course.title,
                role=CourseRole(membership.role),
                group_name=await _membership_group_name(db, membership.id),
            )
        )
    roles = sorted({membership.role for membership in memberships}, key=lambda item: item.value)
    return SessionRead(
        principal=PrincipalRead(id=principal.id, display_name=principal.display_name),
        provider=SessionProviderRead(
            id=connection.id,
            name=connection.name,
            provider=LMSProvider(connection.provider),
        ),
        roles=roles,
        memberships=memberships,
        capabilities=sorted(set(capabilities)),
        admin_elevation_expires_at=elevation_expires_at,
    )


async def _grant_elevation(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    session_key: str,
    request_ip_prefix: str,
    settings: Settings,
) -> AdminElevation:
    now = utcnow()
    await db.execute(
        update(AdminElevation)
        .where(
            AdminElevation.principal_id == principal_id,
            AdminElevation.session_key == session_key,
            AdminElevation.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    absolute = now + timedelta(seconds=settings.admin_elevation_absolute_seconds)
    row = AdminElevation(
        principal_id=principal_id,
        session_key=session_key,
        granted_at=now,
        last_used_at=now,
        expires_at=min(
            now + timedelta(seconds=settings.admin_elevation_idle_seconds),
            absolute,
        ),
        absolute_expires_at=absolute,
        request_ip_prefix=request_ip_prefix,
    )
    db.add(row)
    await db.flush()
    return row


@router.get("/connections", response_model=list[AuthConnectionRead])
async def list_connections(db: DBSession) -> list[AuthConnectionRead]:
    rows = list(
        (
            await db.scalars(
                select(LMSConnection)
                .where(LMSConnection.enabled.is_(True))
                .order_by(LMSConnection.name)
            )
        ).all()
    )
    return [
        AuthConnectionRead(
            id=row.id,
            name=row.name,
            provider=LMSProvider(row.provider),
            enabled=row.enabled,
            login_mode=(
                moodle_login_mode(row) if row.provider == LMSProvider.MOODLE.value else "REDIRECT"
            ),
        )
        for row in rows
    ]


@router.get("/session", response_model=SessionRead)
async def get_session(context: CurrentAuth, db: DBSession) -> SessionRead:
    return await _session_read(
        db,
        context.principal_id,
        capabilities=context.capabilities,
        elevation_expires_at=context.elevation_expires_at,
    )


async def _ensure_dev_identity(
    db: AsyncSession, role: CourseRole, settings: Settings
) -> tuple[ExternalPrincipal, Course]:
    connection = await db.scalar(
        select(LMSConnection).where(LMSConnection.base_url == "https://mock-lms.local")
    )
    if connection is None:
        connection = LMSConnection(
            name="Development LMS",
            provider=LMSProvider.MOCK.value,
            base_url="https://mock-lms.local",
            enabled=True,
            config={"development_only": True},
            capabilities={"courses": True, "memberships": True},
        )
        db.add(connection)
        await db.flush()
    course = await db.scalar(
        select(Course).where(
            Course.connection_id == connection.id,
            Course.external_id == "dev-cpp",
        )
    )
    if course is None:
        course = Course(
            connection_id=connection.id,
            external_id="dev-cpp",
            title="Демонстрационный курс C/C++",
            short_name="C/C++",
            description="Локальный курс только для разработки",
            sync_status="CURRENT",
            catalog_enabled=True,
            catalog_added_at=utcnow(),
        )
        db.add(course)
        await db.flush()
        db.add(
            CourseSection(
                course_id=course.id,
                external_id="dev-section-1",
                title="Основы C/C++",
                position=0,
                visible=True,
            )
        )
    elif not course.catalog_enabled:
        # The explicit development login is itself the opt-in for this local,
        # non-LMS fixture.  Keep it usable after the production catalogue
        # switched to deny-by-default semantics.
        course.catalog_enabled = True
        course.catalog_added_at = utcnow()
    subject = f"dev-{role.value.lower()}"
    principal = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == connection.id,
            ExternalPrincipal.external_subject == subject,
        )
    )
    if principal is None:
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject=subject,
            display_name="Dev преподаватель" if role == CourseRole.TEACHER else "Dev студент",
            email=f"{subject}@example.invalid",
            active=True,
            last_login_at=utcnow(),
        )
        db.add(principal)
        await db.flush()
    else:
        principal.active = True
        principal.last_login_at = utcnow()
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == principal.id,
            CourseMembership.role == role.value,
        )
    )
    if membership is None:
        membership = CourseMembership(
            course_id=course.id,
            principal_id=principal.id,
            role=role.value,
            active=True,
            external_revision="development",
        )
        db.add(membership)
    else:
        membership.active = True
        membership.valid_until = None
        membership.synced_at = utcnow()
    await db.flush()
    # Development identities model the production review scope: a teacher can
    # review only students linked to the same active course group.
    review_group = await db.scalar(
        select(CourseGroup).where(
            CourseGroup.course_id == course.id,
            CourseGroup.external_id == "dev-review-group",
        )
    )
    if review_group is None:
        review_group = CourseGroup(
            course_id=course.id,
            external_id="dev-review-group",
            name="1.1",
            active=True,
        )
        db.add(review_group)
        await db.flush()
    else:
        review_group.active = True
    group_link = await db.scalar(
        select(CourseMembershipGroup).where(
            CourseMembershipGroup.coursemembership_id == membership.id,
            CourseMembershipGroup.coursegroup_id == review_group.id,
        )
    )
    if group_link is None:
        db.add(
            CourseMembershipGroup(
                coursemembership_id=membership.id,
                coursegroup_id=review_group.id,
            )
        )
    if role == CourseRole.TEACHER and await teacher_token_for_principal(db, principal.id) is None:
        public_id, complete_token = generate_teacher_token()
        token_id = uuid.uuid4()
        token = TeacherAccessToken(
            id=token_id,
            public_id=public_id,
            label="Development teacher grant",
            secret_hash=await asyncio.to_thread(hash_teacher_token, complete_token),
            encrypted_secret=encrypt_teacher_token(
                complete_token,
                settings,
                token_id=token_id,
            ),
            created_by_id=principal.id,
        )
        db.add(token)
        await db.flush()
        await bind_teacher_token(db, principal_id=principal.id, token=token)
    return principal, course


@router.post("/dev-login", response_model=SessionRead, status_code=status.HTTP_201_CREATED)
async def dev_login(
    payload: DevLoginRequest,
    request: Request,
    response: Response,
    db: DBSession,
) -> SessionRead:
    settings = _settings(request)
    if not settings.debug or not settings.dev_auth_enabled:
        raise _error(404, "DEV_LOGIN_DISABLED", "Development login is disabled")
    requested_admin = payload.admin_token is not None
    if requested_admin and not verify_admin_token(_secret(payload.admin_token), settings):
        raise _error(403, "INVALID_ADMIN_TOKEN", "Administrator token is invalid")
    principal, _ = await _ensure_dev_identity(db, payload.role, settings)
    credentials = await create_principal_session(
        db,
        principal.id,
        settings,
        user_agent=request.headers.get("user-agent", ""),
        request_ip_prefix=_request_ip_prefix(request),
    )
    elevation = None
    capabilities: tuple[str, ...] = ()
    if requested_admin:
        elevation = await _grant_elevation(
            db,
            principal_id=principal.id,
            session_key=credentials.token_hash,
            request_ip_prefix=_request_ip_prefix(request),
            settings=settings,
        )
        capabilities = ("SYSTEM_SETTINGS",)
    result = await _session_read(
        db,
        principal.id,
        capabilities=capabilities,
        elevation_expires_at=elevation.expires_at if elevation else None,
    )
    await db.commit()
    set_session_cookie(response, credentials, settings)
    return result


def _course_external_id(value: str | None) -> tuple[uuid.UUID | None, str]:
    if not value:
        return None, ""
    try:
        return uuid.UUID(value), ""
    except ValueError:
        if value.isdigit() and int(value) > 0:
            return None, value
        raise _error(
            422, "INVALID_COURSE_ID", "course_id must identify an imported course"
        ) from None


async def _reserve_bridge_secret_attempt(
    db: AsyncSession,
    *,
    connection: LMSConnection,
    request: Request,
    settings: Settings,
) -> MoodleLoginAttempt:
    """Rate-limit costly bridge token verification before running Argon2."""

    network_value = _request_ip_prefix(request) or "unknown"
    network_hash = _login_dimension_hash(settings, "network", network_value)
    bridge_hash = _login_dimension_hash(
        settings,
        "username",
        f"bridge:{connection.id}:{network_value}",
    )
    cutoff = utcnow() - timedelta(seconds=settings.moodle_login_rate_limit_window_seconds)
    async with _login_admission_lock(request):
        try:
            await db.execute(
                update(LMSConnection)
                .where(LMSConnection.id == connection.id)
                .values(
                    enabled=LMSConnection.enabled,
                    updated_at=LMSConnection.updated_at,
                )
            )
            bridge_attempts = int(
                await db.scalar(
                    select(func.count(MoodleLoginAttempt.id)).where(
                        MoodleLoginAttempt.connection_id == connection.id,
                        MoodleLoginAttempt.attempted_at >= cutoff,
                        MoodleLoginAttempt.succeeded.is_(False),
                        MoodleLoginAttempt.username_hash == bridge_hash,
                    )
                )
                or 0
            )
            network_attempts = int(
                await db.scalar(
                    select(func.count(MoodleLoginAttempt.id)).where(
                        MoodleLoginAttempt.connection_id == connection.id,
                        MoodleLoginAttempt.attempted_at >= cutoff,
                        MoodleLoginAttempt.succeeded.is_(False),
                        MoodleLoginAttempt.network_hash == network_hash,
                    )
                )
                or 0
            )
            if (
                bridge_attempts >= settings.moodle_login_rate_limit_attempts
                or network_attempts >= settings.moodle_login_network_rate_limit_attempts
            ):
                raise _error(
                    429,
                    "LOGIN_RATE_LIMITED",
                    "Слишком много попыток входа. Повторите через несколько минут.",
                )
            attempt = MoodleLoginAttempt(
                connection_id=connection.id,
                network_hash=network_hash,
                username_hash=bridge_hash,
                succeeded=False,
                outcome="BRIDGE_PENDING",
            )
            db.add(attempt)
            await db.execute(
                delete(MoodleLoginAttempt).where(
                    MoodleLoginAttempt.connection_id == connection.id,
                    MoodleLoginAttempt.attempted_at < cutoff - timedelta(days=1),
                )
            )
            await db.commit()
            return attempt
        except Exception:
            await db.rollback()
            raise


@router.post(
    "/lms/{connection_id}/start",
    response_model=LMSLoginStartRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_lms_login(
    connection_id: uuid.UUID,
    payload: LMSLoginStartRequest,
    request: Request,
    response: Response,
    db: DBSession,
) -> LMSLoginStartRead:
    settings = _settings(request)
    connection = await db.scalar(
        select(LMSConnection).where(
            LMSConnection.id == connection_id,
            LMSConnection.enabled.is_(True),
        )
    )
    if connection is None:
        raise _error(404, "CONNECTION_NOT_FOUND", "LMS connection was not found")
    if connection.provider != LMSProvider.MOODLE.value:
        raise _error(409, "UNSUPPORTED_LOGIN_PROVIDER", "This provider has no Moodle launch")
    if moodle_auth_mode(connection) != "BRIDGE":
        raise _error(
            409,
            "CREDENTIAL_LOGIN_REQUIRED",
            "This Moodle connection uses direct credential login",
        )
    requested_admin = payload.admin_token is not None
    requested_teacher = payload.teacher_token is not None
    local_course_id, external_course_id = _course_external_id(payload.course_id)
    if local_course_id is not None:
        course = await db.scalar(
            select(Course).where(
                Course.id == local_course_id,
                Course.connection_id == connection.id,
                Course.catalog_enabled.is_(True),
                Course.archived_at.is_(None),
            )
        )
        if course is None:
            raise _error(404, "COURSE_NOT_FOUND", "Course was not found for this provider")
        external_course_id = course.external_id
    secret_attempt = (
        await _reserve_bridge_secret_attempt(
            db,
            connection=connection,
            request=request,
            settings=settings,
        )
        if requested_admin or requested_teacher
        else None
    )
    if requested_admin:
        async with _login_semaphore(request, settings):
            admin_valid = await asyncio.to_thread(
                verify_admin_token,
                _secret(payload.admin_token),
                settings,
            )
        if not admin_valid:
            assert secret_attempt is not None
            await _finish_moodle_login_attempt(
                db,
                secret_attempt.id,
                succeeded=False,
                outcome="INVALID_ADMIN_TOKEN",
            )
            raise _error(403, "INVALID_ADMIN_TOKEN", "Administrator token is invalid")
    teacher_token = None
    if requested_teacher:
        async with _login_semaphore(request, settings):
            teacher_token = await _verify_teacher_token(db, payload.teacher_token)
        if teacher_token is None:
            assert secret_attempt is not None
            await _finish_moodle_login_attempt(
                db,
                secret_attempt.id,
                succeeded=False,
                outcome="INVALID_TEACHER_TOKEN",
            )
            raise _error(403, "INVALID_TEACHER_TOKEN", "Токен преподавателя недействителен")
    secrets = create_login_secrets()
    now = utcnow()
    db.add(
        LoginTransaction(
            connection_id=connection.id,
            state_hash=secrets.state_hash,
            verifier_hash=secrets.verifier_hash,
            admin_requested=requested_admin,
            teacher_token_id=teacher_token.id if teacher_token is not None else None,
            expires_at=now + timedelta(seconds=settings.login_verifier_ttl_seconds),
        )
    )
    if secret_attempt is not None:
        attempt_row = await db.get(MoodleLoginAttempt, secret_attempt.id)
        if attempt_row is not None:
            attempt_row.succeeded = True
            attempt_row.outcome = "BRIDGE_STARTED"
    await db.commit()
    parameters = {"state": secrets.state}
    if external_course_id:
        parameters["courseid"] = external_course_id
    redirect_url = (
        f"{connection.base_url.rstrip('/')}/local/programming_bridge/launch.php?"
        f"{urlencode(parameters)}"
    )
    set_login_verifier_cookie(response, secrets.verifier, settings)
    return LMSLoginStartRead(redirect_url=redirect_url)


async def _finish_moodle_login_attempt(
    db: AsyncSession,
    attempt_id: uuid.UUID,
    *,
    succeeded: bool,
    outcome: str,
) -> None:
    row = await db.get(MoodleLoginAttempt, attempt_id)
    if row is not None:
        row.succeeded = succeeded
        row.outcome = outcome[:32]
    await db.commit()


async def _project_pluginless_identity(
    db: AsyncSession,
    *,
    connection: LMSConnection,
    identity: Any,
    authoritative_roles: bool = True,
    teacher_token_id: uuid.UUID | None = None,
    teacher_token_selector: str | None = None,
    teacher_token_verifier: str | None = None,
) -> ExternalPrincipal:
    now = utcnow()
    principal = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == connection.id,
            ExternalPrincipal.external_subject == identity.external_subject,
        )
    )
    if principal is None:
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject=identity.external_subject,
            display_name=identity.display_name,
        )
        db.add(principal)
        await db.flush()
    principal.display_name = identity.display_name
    principal.email = identity.email
    principal.locale = identity.locale or "ru"
    principal.active = True
    principal.last_login_at = now

    if teacher_token_id is not None:
        teacher_token = await _bind_teacher_token_or_error(
            db,
            principal_id=principal.id,
            token_id=teacher_token_id,
            expected_public_id=teacher_token_selector,
            expected_secret_hash=teacher_token_verifier,
        )
    else:
        teacher_token = await teacher_token_for_principal(db, principal.id)

    catalog_courses = {
        course.external_id: course
        for course in (
            await db.scalars(
                select(Course).where(
                    Course.connection_id == connection.id,
                    Course.catalog_enabled.is_(True),
                    Course.archived_at.is_(None),
                )
            )
        ).all()
    }
    if authoritative_roles:
        connection_course_ids = select(Course.id).where(Course.connection_id == connection.id)
        await db.execute(
            update(CourseMembership)
            .where(
                CourseMembership.principal_id == principal.id,
                CourseMembership.course_id.in_(connection_course_ids),
                CourseMembership.active.is_(True),
            )
            .values(active=False, synced_at=now)
        )
    for external in identity.courses:
        external_id = str(external.external_id)
        # Moodle proves that the principal can see the course.  The platform
        # teacher-token binding is the sole global role boundary; brittle HTML
        # role heuristics must never grant teacher permissions by themselves.
        role = CourseRole.TEACHER.value if teacher_token is not None else CourseRole.STUDENT.value
        if not external_id:
            continue
        course = catalog_courses.get(external_id)
        if course is None:
            continue
        # Role evidence is authoritative only for this admitted course. Keep
        # the historical row, but deactivate a conflicting active role before
        # projecting the newly confirmed one.
        await db.execute(
            update(CourseMembership)
            .where(
                CourseMembership.course_id == course.id,
                CourseMembership.principal_id == principal.id,
                CourseMembership.role != role,
                CourseMembership.active.is_(True),
            )
            .values(active=False, synced_at=now)
        )
        membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == course.id,
                CourseMembership.principal_id == principal.id,
                CourseMembership.role == role,
            )
        )
        if membership is None:
            membership = CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role=role,
            )
            db.add(membership)
        membership.active = True
        membership.valid_until = None
        membership.external_revision = (
            teacher_membership_revision(teacher_token.id, "pluginless-login")
            if teacher_token is not None
            else "pluginless-login"
        )
        membership.synced_at = now
    await db.flush()
    return principal


@router.post(
    "/lms/{connection_id}/credentials",
    response_model=SessionRead,
    status_code=status.HTTP_201_CREATED,
)
async def login_with_moodle_credentials(
    connection_id: uuid.UUID,
    payload: MoodleCredentialLoginRequest,
    request: Request,
    response: Response,
    db: DBSession,
) -> SessionRead:
    """Exchange a Moodle password once for a server-side local session."""

    settings = _settings(request)
    if (
        not settings.debug
        and not settings.moodle_credential_login_allow_insecure_http
        and request.url.scheme.lower() != "https"
    ):
        raise _error(
            400,
            "HTTPS_REQUIRED",
            "Вход с паролем Moodle доступен только через HTTPS",
        )

    requested_admin = payload.admin_token is not None
    requested_teacher = payload.teacher_token is not None
    username = payload.username.strip()
    network_hash = _login_dimension_hash(
        settings, "network", _request_ip_prefix(request) or "unknown"
    )
    username_hash = _login_dimension_hash(settings, "username", username)
    cutoff = utcnow() - timedelta(seconds=settings.moodle_login_rate_limit_window_seconds)

    # The no-op UPDATE is deliberately the first database statement in this
    # transaction. It takes a PostgreSQL row lock and a SQLite write lock, so
    # count + reservation cannot be raced by another worker or process. The
    # local guard handles SQLite StaticPool, where test sessions share one
    # physical connection. Commit releases both database locks before any
    # network request is made.
    async with _login_admission_lock(request):
        try:
            await db.execute(
                update(LMSConnection)
                .where(LMSConnection.id == connection_id)
                .values(
                    enabled=LMSConnection.enabled,
                    updated_at=LMSConnection.updated_at,
                )
            )
            connection = await db.scalar(
                select(LMSConnection).where(
                    LMSConnection.id == connection_id,
                    LMSConnection.enabled.is_(True),
                    LMSConnection.provider == LMSProvider.MOODLE.value,
                )
            )
            if connection is None:
                raise _error(404, "CONNECTION_NOT_FOUND", "Moodle connection was not found")
            if moodle_auth_mode(connection) != "PLUGINLESS":
                raise _error(
                    409,
                    "REDIRECT_LOGIN_REQUIRED",
                    "This Moodle connection uses bridge login",
                )
            pluginless_transport = moodle_pluginless_transport(connection)
            username_attempts = int(
                await db.scalar(
                    select(func.count(MoodleLoginAttempt.id)).where(
                        MoodleLoginAttempt.connection_id == connection.id,
                        MoodleLoginAttempt.attempted_at >= cutoff,
                        MoodleLoginAttempt.succeeded.is_(False),
                        MoodleLoginAttempt.username_hash == username_hash,
                    )
                )
                or 0
            )
            network_attempts = int(
                await db.scalar(
                    select(func.count(MoodleLoginAttempt.id)).where(
                        MoodleLoginAttempt.connection_id == connection.id,
                        MoodleLoginAttempt.attempted_at >= cutoff,
                        MoodleLoginAttempt.succeeded.is_(False),
                        MoodleLoginAttempt.network_hash == network_hash,
                    )
                )
                or 0
            )
            if (
                username_attempts >= settings.moodle_login_rate_limit_attempts
                or network_attempts >= settings.moodle_login_network_rate_limit_attempts
            ):
                raise _error(
                    429,
                    "LOGIN_RATE_LIMITED",
                    "Слишком много попыток входа. Повторите через несколько минут.",
                )
            attempt = MoodleLoginAttempt(
                connection_id=connection.id,
                network_hash=network_hash,
                username_hash=username_hash,
                succeeded=False,
                outcome="PENDING",
            )
            db.add(attempt)
            await db.execute(
                delete(MoodleLoginAttempt).where(
                    MoodleLoginAttempt.connection_id == connection.id,
                    MoodleLoginAttempt.attempted_at < cutoff - timedelta(days=1),
                )
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    attempt_id = attempt.id
    moodle_base_url = connection.base_url
    catalog_course_ids = tuple(
        (
            await db.scalars(
                select(Course.external_id)
                .where(
                    Course.connection_id == connection.id,
                    Course.catalog_enabled.is_(True),
                    Course.archived_at.is_(None),
                )
                .order_by(Course.external_id)
            )
        ).all()
    )
    catalog_course_id_set = set(catalog_course_ids)

    # Verify the optional elevation secret only after the atomic login
    # reservation. This subjects bad admin-token guesses to the same durable
    # per-user/network limits and keeps expensive Argon2 work out of the
    # connection row-lock transaction. The semaphore bounds CPU pressure per
    # worker; no Moodle request is made for an invalid elevation secret.
    if requested_admin:
        async with _login_semaphore(request, settings):
            admin_token_valid = await asyncio.to_thread(
                verify_admin_token,
                _secret(payload.admin_token),
                settings,
            )
        if not admin_token_valid:
            await _finish_moodle_login_attempt(
                db,
                attempt_id,
                succeeded=False,
                outcome="INVALID_ADMIN_TOKEN",
            )
            raise _error(403, "INVALID_ADMIN_TOKEN", "Administrator token is invalid")

    teacher_token: TeacherAccessToken | None = None
    if requested_teacher:
        async with _login_semaphore(request, settings):
            teacher_token = await _verify_teacher_token(db, payload.teacher_token)
        if teacher_token is None:
            await _finish_moodle_login_attempt(
                db,
                attempt_id,
                succeeded=False,
                outcome="INVALID_TEACHER_TOKEN",
            )
            raise _error(403, "INVALID_TEACHER_TOKEN", "Токен преподавателя недействителен")

    # Never use a shared cookie jar for authentication. Both the Moodle origin
    # and the internal browser endpoint come only from administrator-controlled
    # configuration.
    browser_state: dict[str, Any] | None = None
    transport_timeout = (
        float(
            getattr(
                settings,
                "moodle_browser_http_timeout_seconds",
                max(30.0, settings.moodle_http_timeout_seconds * 5),
            )
        )
        if pluginless_transport == "PLAYWRIGHT"
        else max(30.0, settings.moodle_http_timeout_seconds * 5)
    )
    logger.info(
        "Moodle pluginless login selected connection_id=%s transport=%s",
        connection_id,
        pluginless_transport,
    )
    try:
        async with asyncio.timeout(transport_timeout + 5):
            async with _login_semaphore(request, settings):
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    trust_env=False,
                    headers={"User-Agent": f"SFEDU-MMCS/{settings.app_build}"},
                ) as client:
                    if pluginless_transport == "PLAYWRIGHT":
                        from app.integrations.moodle_browser import MoodleBrowserClient

                        authenticated = await MoodleBrowserClient(
                            settings,
                            client,
                            base_url=moodle_base_url,
                        ).authenticate(
                            username,
                            payload.password.get_secret_value(),
                            allowed_course_ids=catalog_course_ids,
                        )
                        identity = authenticated.identity
                        browser_state = authenticated.storage_state
                    else:
                        from app.integrations.moodle_standard import MoodleStandardClient

                        identity = await MoodleStandardClient(
                            settings,
                            client,
                            base_url=moodle_base_url,
                        ).authenticate(username, payload.password.get_secret_value())
    except TimeoutError as exc:
        await _finish_moodle_login_attempt(db, attempt_id, succeeded=False, outcome="TIMEOUT")
        raise _error(503, "MOODLE_LOGIN_BUSY", "Moodle не успел ответить на запрос входа") from exc
    except IntegrationError as exc:
        # Connector exceptions are safe-to-log by contract and never contain
        # passwords, tokens or upstream response bodies.
        logger.warning(
            "Moodle pluginless login failed connection_id=%s code=%s type=%s detail=%s",
            connection_id,
            exc.code,
            type(exc).__name__,
            exc,
        )
        await _finish_moodle_login_attempt(db, attempt_id, succeeded=False, outcome=exc.code)
        if exc.code in {"INVALID_CREDENTIALS", "MOODLE_AUTHENTICATION_FAILED"}:
            raise _error(401, "INVALID_CREDENTIALS", "Неверный логин или пароль Moodle") from exc
        if pluginless_transport == "PLAYWRIGHT" and exc.code in {
            "NOT_CONFIGURED",
            "TIMEOUT",
            "UNAVAILABLE",
        }:
            raise _error(
                503,
                "MOODLE_BROWSER_UNAVAILABLE",
                "Сервис входа Moodle временно недоступен",
            ) from exc
        if pluginless_transport == "MOBILE_TOKEN" and exc.code in {
            "TIMEOUT",
            "UNAVAILABLE",
        }:
            raise _error(
                503,
                "MOODLE_WEB_SERVICE_UNAVAILABLE",
                "Штатный web service Moodle недоступен; подключение нужно перевести на Playwright",
            ) from exc
        if exc.code in {
            "NOT_CONFIGURED",
            "SERVICE_UNAVAILABLE",
            "MOODLE_WEB_SERVICES_DISABLED",
        }:
            raise _error(
                503,
                "MOODLE_WEB_SERVICE_UNAVAILABLE",
                "Штатный вход через web service отключён или недоступен в Moodle",
            ) from exc
        raise _error(502, exc.code, "Moodle вернул некорректный ответ при входе") from exc

    confirmed_course_roles = [
        f"{external.external_id}:{str(external.role).upper()}"
        for external in identity.courses
        if str(external.external_id) in catalog_course_id_set
        if str(external.role).upper() in {CourseRole.STUDENT.value, CourseRole.TEACHER.value}
    ][:64]
    logger.info(
        "Moodle pluginless identity received connection_id=%s confirmed_course_roles=%s",
        connection_id,
        confirmed_course_roles,
    )
    connection = await db.scalar(
        select(LMSConnection).where(
            LMSConnection.id == connection_id,
            LMSConnection.enabled.is_(True),
        )
    )
    if (
        connection is None
        or moodle_auth_mode(connection) != "PLUGINLESS"
        or moodle_pluginless_transport(connection) != pluginless_transport
    ):
        await _finish_moodle_login_attempt(db, attempt_id, succeeded=False, outcome="DISABLED")
        raise _error(409, "CONNECTION_DISABLED", "Moodle connection changed during login")
    try:
        principal = await _project_pluginless_identity(
            db,
            connection=connection,
            identity=identity,
            # Dashboard/catalog intersection is authoritative for enrollment.
            # The bounded role probe may remain UNKNOWN because the global
            # teacher token, not HTML controls, determines platform role.
            authoritative_roles=True,
            teacher_token_id=teacher_token.id if teacher_token is not None else None,
            teacher_token_selector=teacher_token.public_id if teacher_token is not None else None,
            teacher_token_verifier=teacher_token.secret_hash if teacher_token is not None else None,
        )
    except HTTPException as exc:
        await db.rollback()
        detail = exc.detail if isinstance(exc.detail, dict) else {}
        await _finish_moodle_login_attempt(
            db,
            attempt_id,
            succeeded=False,
            outcome=str(detail.get("code", "TEACHER_TOKEN_CONFLICT"))[:32],
        )
        raise
    credential_kind = (
        BROWSER_STATE_CREDENTIAL_KIND if pluginless_transport == "PLAYWRIGHT" else "MOBILE_TOKEN"
    )
    credential = await db.scalar(
        select(MoodleCredential).where(
            MoodleCredential.connection_id == connection.id,
            MoodleCredential.principal_id == principal.id,
            MoodleCredential.kind == credential_kind,
        )
    )
    if pluginless_transport == "PLAYWRIGHT":
        if browser_state is None:
            await db.rollback()
            await _finish_moodle_login_attempt(
                db, attempt_id, succeeded=False, outcome="INVALID_RESPONSE"
            )
            raise _error(502, "INVALID_RESPONSE", "Moodle не вернул состояние сессии")
        try:
            encrypted = encrypt_moodle_browser_state(
                browser_state,
                settings,
                connection_id=connection.id,
                principal_id=principal.id,
            )
        except ValueError as exc:
            await db.rollback()
            await _finish_moodle_login_attempt(
                db, attempt_id, succeeded=False, outcome="INVALID_BROWSER_STATE"
            )
            raise _error(
                502,
                "INVALID_RESPONSE",
                "Moodle вернул некорректное состояние сессии",
            ) from exc
        metadata = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
            "state_schema": BROWSER_STATE_CREDENTIAL_KIND,
        }
    else:
        encrypted = encrypt_moodle_credential(
            identity.token,
            settings,
            connection_id=connection.id,
            principal_id=principal.id,
            kind="MOBILE_TOKEN",
        )
        metadata = {
            "functions": sorted(identity.functions),
            "upload_files": bool(identity.upload_files),
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "MOBILE_TOKEN",
        }
    if credential is None:
        credential = MoodleCredential(
            connection_id=connection.id,
            principal_id=principal.id,
            kind=credential_kind,
            encrypted_secret=encrypted,
        )
        db.add(credential)
    elif pluginless_transport == "PLAYWRIGHT":
        credential.revision += 1
    credential.encrypted_secret = encrypted
    credential.status = "ACTIVE"
    credential.metadata_json = metadata
    credential.last_verified_at = utcnow()
    credential.last_used_at = utcnow()
    credential.lease_owner = None
    credential.lease_expires_at = None
    credential.expires_at = None
    credential.revoked_at = None

    resumed_outbox_events = await _resume_outbox_after_moodle_reauthentication(
        db,
        connection_id=connection.id,
        principal_id=principal.id,
    )

    credentials = await create_principal_session(
        db,
        principal.id,
        settings,
        user_agent=request.headers.get("user-agent", ""),
        request_ip_prefix=_request_ip_prefix(request),
    )
    elevation = None
    capabilities: tuple[str, ...] = ()
    if requested_admin:
        elevation = await _grant_elevation(
            db,
            principal_id=principal.id,
            session_key=credentials.token_hash,
            request_ip_prefix=_request_ip_prefix(request),
            settings=settings,
        )
        capabilities = ("SYSTEM_SETTINGS",)
    attempt_row = await db.get(MoodleLoginAttempt, attempt_id)
    if attempt_row is not None:
        attempt_row.succeeded = True
        attempt_row.outcome = "SUCCESS"
    db.add(
        AuditEntry(
            actor_id=principal.id,
            action="auth.moodle_pluginless_login",
            object_type="LMSConnection",
            object_id=connection.id,
            request_id=getattr(request.state, "request_id", "")[:100],
            metadata_json={
                "course_count": len(identity.courses),
                "admin_elevation": requested_admin,
                "teacher_token_used": requested_teacher,
                "teacher_token_id": str(teacher_token.id) if teacher_token is not None else None,
                "pluginless_transport": pluginless_transport,
                "resumed_outbox_events": resumed_outbox_events,
            },
        )
    )
    result = await _session_read(
        db,
        principal.id,
        capabilities=capabilities,
        elevation_expires_at=elevation.expires_at if elevation else None,
    )
    await db.commit()
    set_session_cookie(response, credentials, settings)
    response.headers["Cache-Control"] = "no-store"
    return result


def _decode_segment(value: str, *, limit: int) -> bytes:
    if not value or len(value) > limit or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise _error(400, "INVALID_LAUNCH_ASSERTION", "Launch assertion is malformed")
    try:
        return base64.b64decode(
            value + "=" * (-len(value) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, TypeError) as exc:
        raise _error(400, "INVALID_LAUNCH_ASSERTION", "Launch assertion is malformed") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _parse_assertion(assertion: str) -> tuple[str, bytes, dict[str, Any]]:
    if len(assertion) > _ASSERTION_LIMIT or assertion.count(".") != 1:
        raise _error(400, "INVALID_LAUNCH_ASSERTION", "Launch assertion is malformed")
    body_segment, signature_segment = assertion.split(".", 1)
    body = _decode_segment(body_segment, limit=_ASSERTION_LIMIT)
    signature = _decode_segment(signature_segment, limit=256)
    if len(body) > 6_144 or len(signature) != hashlib.sha256().digest_size:
        raise _error(400, "INVALID_LAUNCH_ASSERTION", "Launch assertion is malformed")
    try:
        payload = json.loads(body, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _error(400, "INVALID_LAUNCH_ASSERTION", "Launch assertion JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise _error(400, "INVALID_LAUNCH_ASSERTION", "Launch assertion JSON is invalid")
    return body_segment, signature, payload


def _integer_claim(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error(400, "INVALID_LAUNCH_ASSERTION", f"Launch claim {name} is invalid")
    return value


def _string_claim(payload: dict[str, Any], name: str, *, limit: int = 255) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value or len(value) > limit:
        raise _error(400, "INVALID_LAUNCH_ASSERTION", f"Launch claim {name} is invalid")
    return value


async def _read_callback_form(request: Request) -> tuple[str, str | None]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        raise _error(415, "INVALID_CALLBACK_MEDIA_TYPE", "Expected form-encoded callback")
    body = await request.body()
    if len(body) > _FORM_LIMIT:
        raise _error(413, "CALLBACK_TOO_LARGE", "Callback body is too large")
    try:
        values = parse_qs(
            body.decode("ascii"),
            keep_blank_values=True,
            strict_parsing=True,
            max_num_fields=3,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _error(400, "INVALID_CALLBACK_FORM", "Callback form is invalid") from exc
    if set(values) - {"assertion", "state"}:
        raise _error(400, "INVALID_CALLBACK_FORM", "Callback form contains unknown fields")
    assertions = values.get("assertion", [])
    states = values.get("state", [])
    if len(assertions) != 1 or len(states) > 1:
        raise _error(400, "INVALID_CALLBACK_FORM", "Callback form is invalid")
    state_value = states[0] if states else None
    if state_value is not None and not _TOKEN_RE.fullmatch(state_value):
        raise _error(400, "INVALID_LOGIN_STATE", "Login state is invalid")
    return assertions[0], state_value


def _validate_launch_claims(
    payload: dict[str, Any], settings: Settings
) -> tuple[str, str, str, CourseRole, int | None, str | None, datetime]:
    issuer = _string_claim(payload, "iss", limit=500).rstrip("/")
    subject = _string_claim(payload, "sub")
    audience = _string_claim(payload, "aud", limit=500).rstrip("/")
    if not hmac.compare_digest(audience, settings.public_base_url.rstrip("/")):
        raise _error(403, "INVALID_LAUNCH_AUDIENCE", "Launch assertion audience does not match")
    issued_at = _integer_claim(payload, "iat")
    expires_at = _integer_claim(payload, "exp")
    now_epoch = int(utcnow().timestamp())
    if (
        expires_at <= now_epoch - 5
        or issued_at > now_epoch + 30
        or issued_at < now_epoch - 180
        or expires_at <= issued_at
        or expires_at - issued_at > 180
    ):
        raise _error(403, "EXPIRED_LAUNCH_ASSERTION", "Launch assertion is expired or invalid")
    nonce = _string_claim(payload, "nonce")
    if not _NONCE_RE.fullmatch(nonce):
        raise _error(400, "INVALID_LAUNCH_NONCE", "Launch nonce is invalid")
    try:
        role = CourseRole(_string_claim(payload, "role", limit=10).upper())
    except ValueError as exc:
        raise _error(403, "INVALID_COURSE_ROLE", "Launch role is not supported") from exc
    course_value = payload.get("course_id")
    course_external_id: int | None
    if course_value is None:
        course_external_id = None
    elif isinstance(course_value, bool) or not isinstance(course_value, int) or course_value <= 0:
        raise _error(400, "INVALID_LAUNCH_COURSE", "Launch course id is invalid")
    else:
        course_external_id = course_value
    payload_state = payload.get("state")
    if payload_state is not None and (
        not isinstance(payload_state, str) or not _TOKEN_RE.fullmatch(payload_state)
    ):
        raise _error(400, "INVALID_LOGIN_STATE", "Login state is invalid")
    display_name = payload.get("display_name", subject)
    if not isinstance(display_name, str) or not display_name or len(display_name) > 255:
        display_name = subject
    return (
        issuer,
        subject,
        display_name,
        role,
        course_external_id,
        payload_state,
        datetime.fromtimestamp(expires_at, UTC),
    )


async def _upsert_launch_identity(
    db: AsyncSession,
    *,
    connection: LMSConnection,
    subject: str,
    display_name: str,
    role: CourseRole,
    course_external_id: int | None,
    teacher_token_id: uuid.UUID | None = None,
) -> ExternalPrincipal:
    principal = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == connection.id,
            ExternalPrincipal.external_subject == subject,
        )
    )
    if principal is None:
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject=subject,
            display_name=display_name,
            active=True,
            last_login_at=utcnow(),
        )
        db.add(principal)
        await db.flush()
    else:
        principal.display_name = display_name
        principal.active = True
        principal.last_login_at = utcnow()
    if teacher_token_id is not None:
        teacher_token = await _bind_teacher_token_or_error(
            db,
            principal_id=principal.id,
            token_id=teacher_token_id,
        )
    else:
        teacher_token = await teacher_token_for_principal(db, principal.id)
    effective_role = CourseRole.TEACHER if teacher_token is not None else CourseRole.STUDENT
    if course_external_id is None:
        return principal
    course = await db.scalar(
        select(Course).where(
            Course.connection_id == connection.id,
            Course.external_id == str(course_external_id),
            Course.catalog_enabled.is_(True),
            Course.archived_at.is_(None),
        )
    )
    if course is None:
        return principal
    await db.execute(
        update(CourseMembership)
        .where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == principal.id,
            CourseMembership.role != effective_role.value,
            CourseMembership.active.is_(True),
        )
        .values(active=False, synced_at=utcnow())
    )
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == principal.id,
            CourseMembership.role == effective_role.value,
        )
    )
    if membership is None:
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role=effective_role.value,
                active=True,
                external_revision=(
                    teacher_membership_revision(teacher_token.id, "bridge-login")
                    if teacher_token is not None
                    else "bridge-login"
                ),
                synced_at=utcnow(),
            )
        )
    else:
        membership.active = True
        membership.valid_until = None
        membership.external_revision = (
            teacher_membership_revision(teacher_token.id, "bridge-login")
            if teacher_token is not None
            else "bridge-login"
        )
        membership.synced_at = utcnow()
    return principal


@router.post("/moodle/callback", include_in_schema=False)
async def moodle_callback(request: Request, db: DBSession) -> RedirectResponse:
    settings = _settings(request)
    assertion, form_state = await _read_callback_form(request)
    body_segment, supplied_signature, payload = _parse_assertion(assertion)
    (
        issuer,
        subject,
        display_name,
        role,
        course_external_id,
        payload_state,
        assertion_expires_at,
    ) = _validate_launch_claims(payload, settings)
    if form_state != payload_state:
        raise _error(403, "LOGIN_STATE_MISMATCH", "Callback state does not match assertion")

    transaction = None
    if form_state:
        transaction = await db.scalar(
            select(LoginTransaction)
            .where(LoginTransaction.state_hash == hash_opaque_secret(form_state))
            .with_for_update()
        )
        if (
            transaction is None
            or transaction.used_at is not None
            or _aware(transaction.expires_at) <= utcnow()
        ):
            raise _error(403, "INVALID_LOGIN_STATE", "Login transaction is invalid or expired")
        verifier = request.cookies.get(settings.login_verifier_cookie_name)
        if not verify_login_verifier(verifier, transaction.verifier_hash):
            raise _error(403, "LOGIN_VERIFIER_MISMATCH", "Login verifier is missing or invalid")
        connection = await db.get(LMSConnection, transaction.connection_id)
    else:
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
        matching = [row for row in connections if row.base_url.rstrip("/") == issuer]
        connection = matching[0] if len(matching) == 1 else None
    if (
        connection is None
        or not connection.enabled
        or connection.provider != LMSProvider.MOODLE.value
        or connection.base_url.rstrip("/") != issuer
    ):
        raise _error(403, "INVALID_LAUNCH_ISSUER", "Launch issuer is not configured")
    shared_secret = settings.moodle_launch_shared_secret.get_secret_value()
    if len(shared_secret) < 20:
        raise _error(503, "MOODLE_LAUNCH_NOT_CONFIGURED", "Moodle launch is not configured")
    expected_signature = hmac.new(
        shared_secret.encode("utf-8"),
        body_segment.encode("ascii"),
        hashlib.sha256,
    ).digest()
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise _error(403, "INVALID_LAUNCH_SIGNATURE", "Launch signature is invalid")
    if transaction is None:
        # Platform-initiated login is bound to this browser by state + verifier.
        # Direct course navigation has no verifier, so bind it to the LMS origin
        # to prevent login-CSRF/session swapping with somebody else's assertion.
        _require_direct_launch_origin(request, expected_base_url=connection.base_url)

    nonce = _string_claim(payload, "nonce")
    db.add(
        UsedLaunchNonce(
            connection_id=connection.id,
            nonce_hash=hash_opaque_secret(nonce),
            expires_at=assertion_expires_at,
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _error(409, "LAUNCH_ASSERTION_REPLAYED", "Launch assertion was already used") from exc
    if transaction is not None:
        transaction.used_at = utcnow()
    principal = await _upsert_launch_identity(
        db,
        connection=connection,
        subject=subject,
        display_name=display_name,
        role=role,
        course_external_id=course_external_id,
        teacher_token_id=transaction.teacher_token_id if transaction is not None else None,
    )
    credentials = await create_principal_session(
        db,
        principal.id,
        settings,
        user_agent=request.headers.get("user-agent", ""),
        request_ip_prefix=_request_ip_prefix(request),
    )
    if transaction is not None and transaction.admin_requested:
        await _grant_elevation(
            db,
            principal_id=principal.id,
            session_key=credentials.token_hash,
            request_ip_prefix=_request_ip_prefix(request),
            settings=settings,
        )
    await db.commit()
    response = RedirectResponse(settings.frontend_url, status_code=status.HTTP_303_SEE_OTHER)
    set_session_cookie(response, credentials, settings)
    clear_login_verifier_cookie(response, settings)
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(context: CurrentAuth, request: Request, response: Response, db: DBSession) -> None:
    await revoke_principal_session(db, context.session_id)
    await db.execute(
        update(AdminElevation)
        .where(
            AdminElevation.principal_id == context.principal_id,
            AdminElevation.session_key == context.session_key,
            AdminElevation.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    await db.commit()
    clear_session_cookie(response, _settings(request))


@router.post("/admin-elevation", response_model=SessionRead)
async def elevate_admin(
    payload: AdminElevationRequest,
    context: CurrentAuth,
    request: Request,
    db: DBSession,
) -> SessionRead:
    settings = _settings(request)
    if not verify_admin_token(_secret(payload.admin_token), settings):
        raise _error(403, "INVALID_ADMIN_TOKEN", "Administrator token is invalid")
    elevation = await _grant_elevation(
        db,
        principal_id=context.principal_id,
        session_key=context.session_key,
        request_ip_prefix=_request_ip_prefix(request),
        settings=settings,
    )
    result = await _session_read(
        db,
        context.principal_id,
        capabilities=("SYSTEM_SETTINGS",),
        elevation_expires_at=elevation.expires_at,
    )
    await db.commit()
    return result


@router.delete("/admin-elevation", status_code=status.HTTP_204_NO_CONTENT)
async def drop_admin_elevation(context: CurrentAuth, db: DBSession) -> None:
    await db.execute(
        update(AdminElevation)
        .where(
            AdminElevation.principal_id == context.principal_id,
            AdminElevation.session_key == context.session_key,
            AdminElevation.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    await db.commit()
