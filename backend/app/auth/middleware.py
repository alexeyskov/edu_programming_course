from __future__ import annotations

import ipaddress
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response

from app.auth.context import AuthContext
from app.auth.csrf import CSRFProtection
from app.core.config import Settings
from app.core.security import hash_opaque_secret
from app.db.base import utcnow
from app.models.courses import Course, CourseMembership
from app.models.identity import (
    AdminElevation,
    ExternalPrincipal,
    LMSConnection,
    PrincipalSession,
)
from app.services.teacher_tokens import teacher_membership_is_authorized

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _request_ip_prefix(request: Request) -> str:
    host = request.client.host if request.client else ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""
    bits = 24 if address.version == 4 else 64
    return str(ipaddress.ip_network(f"{address}/{bits}", strict=False))


class SessionAuthenticationMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request.state.auth = None
        bearer = request.cookies.get(self.settings.session_cookie_name)
        clear_cookie = False
        if bearer:
            clear_cookie = not await self._authenticate(request, bearer)
        response = await call_next(request)
        if clear_cookie:
            response.delete_cookie(
                self.settings.session_cookie_name,
                httponly=True,
                secure=self.settings.cookie_secure,
                samesite=self.settings.session_cookie_samesite,
                path="/",
            )
        return response

    async def _authenticate(self, request: Request, bearer: str) -> bool:
        now = utcnow()
        token_hash = hash_opaque_secret(bearer)
        factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
        async with factory() as db:
            authenticated = (
                await db.execute(
                    select(PrincipalSession, ExternalPrincipal, LMSConnection)
                    .join(
                        ExternalPrincipal,
                        ExternalPrincipal.id == PrincipalSession.principal_id,
                    )
                    .join(
                        LMSConnection,
                        LMSConnection.id == ExternalPrincipal.connection_id,
                    )
                    .where(
                        PrincipalSession.token_hash == token_hash,
                        PrincipalSession.revoked_at.is_(None),
                        PrincipalSession.expires_at > now,
                    )
                )
            ).one_or_none()
            if authenticated is None:
                return False
            session_row, principal, connection = authenticated
            if not principal.active or not connection.enabled:
                # A disabled identity source is an authorization boundary, not merely a
                # presentation setting. Revoke the bearer so that replaying a cookie after
                # the connector is re-enabled cannot silently restore an old session.
                session_row.revoked_at = now
                await db.commit()
                return False
            teacher_authorized = await teacher_membership_is_authorized(db, principal.id)
            roles = tuple(
                sorted(
                    set(
                        (
                            await db.scalars(
                                select(CourseMembership.role)
                                .join(Course, Course.id == CourseMembership.course_id)
                                .join(
                                    LMSConnection,
                                    LMSConnection.id == Course.connection_id,
                                )
                                .where(
                                    CourseMembership.principal_id == principal.id,
                                    CourseMembership.active.is_(True),
                                    Course.connection_id == principal.connection_id,
                                    Course.catalog_enabled.is_(True),
                                    Course.archived_at.is_(None),
                                    LMSConnection.enabled.is_(True),
                                    or_(
                                        CourseMembership.valid_until.is_(None),
                                        CourseMembership.valid_until > now,
                                    ),
                                )
                            )
                        ).all()
                    )
                    - ({"TEACHER"} if not teacher_authorized else set())
                )
            )
            elevation = await db.scalar(
                select(AdminElevation)
                .where(
                    AdminElevation.principal_id == principal.id,
                    AdminElevation.session_key == token_hash,
                    AdminElevation.revoked_at.is_(None),
                    AdminElevation.expires_at > now,
                    AdminElevation.absolute_expires_at > now,
                )
                .order_by(AdminElevation.granted_at.desc())
            )
            capabilities: tuple[str, ...] = ()
            elevation_expires_at = None
            if elevation is not None:
                current_prefix = _request_ip_prefix(request)
                if elevation.request_ip_prefix and not secrets.compare_digest(
                    elevation.request_ip_prefix,
                    current_prefix,
                ):
                    elevation.revoked_at = now
                else:
                    capabilities = ("SYSTEM_SETTINGS",)
                    elevation.last_used_at = now
                    elevation.expires_at = min(
                        now + timedelta(seconds=self.settings.admin_elevation_idle_seconds),
                        _aware(elevation.absolute_expires_at),
                    )
                    elevation_expires_at = elevation.expires_at
            if _aware(session_row.last_seen_at) < now - timedelta(
                seconds=self.settings.session_touch_interval_seconds
            ):
                session_row.last_seen_at = now
                session_row.expires_at = now + timedelta(seconds=self.settings.session_ttl_seconds)
            await db.commit()
            request.state.auth = AuthContext(
                principal_id=principal.id,
                display_name=principal.display_name,
                session_id=session_row.id,
                session_key=token_hash,
                roles=roles,
                capabilities=capabilities,
                elevation_expires_at=elevation_expires_at,
            )
            return True


class CSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, *, settings: Settings, protection: CSRFProtection):
        super().__init__(app)
        self.settings = settings
        self.protection = protection
        self.exempt_paths = {f"{settings.api_prefix}/auth/moodle/callback"}

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.method not in SAFE_METHODS and request.url.path not in self.exempt_paths:
            cookie = request.cookies.get(self.settings.csrf_cookie_name, "")
            header = request.headers.get("X-CSRFToken", "")
            if (
                not cookie
                or not header
                or not secrets.compare_digest(cookie, header)
                or not self.protection.valid(cookie)
            ):
                return JSONResponse(
                    status_code=403,
                    content={
                        "code": "CSRF_FAILED",
                        "message": "CSRF token is missing, expired, or does not match",
                        "trace_id": getattr(request.state, "request_id", ""),
                    },
                )
        return await call_next(request)
