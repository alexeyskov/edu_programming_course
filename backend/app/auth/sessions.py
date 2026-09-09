from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta

from fastapi import Response
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import generate_opaque_secret, hash_opaque_secret
from app.db.base import utcnow
from app.models.identity import PrincipalSession


@dataclass(frozen=True, slots=True)
class SessionCredentials:
    session_id: uuid.UUID
    bearer: str
    token_hash: str


async def create_principal_session(
    db: AsyncSession,
    principal_id: uuid.UUID,
    settings: Settings,
    *,
    user_agent: str = "",
    request_ip_prefix: str = "",
) -> SessionCredentials:
    bearer = generate_opaque_secret()
    token_hash = hash_opaque_secret(bearer)
    now = utcnow()
    row = PrincipalSession(
        principal_id=principal_id,
        token_hash=token_hash,
        expires_at=now + timedelta(seconds=settings.session_ttl_seconds),
        last_seen_at=now,
        user_agent_hash=hashlib.sha256(user_agent.encode()).hexdigest() if user_agent else "",
        request_ip_prefix=request_ip_prefix,
    )
    db.add(row)
    await db.flush()
    return SessionCredentials(row.id, bearer, token_hash)


async def revoke_principal_session(db: AsyncSession, session_id: uuid.UUID) -> None:
    await db.execute(
        update(PrincipalSession)
        .where(PrincipalSession.id == session_id, PrincipalSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def set_session_cookie(
    response: Response, credentials: SessionCredentials, settings: Settings
) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        credentials.bearer,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.session_cookie_name,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.session_cookie_samesite,
        path="/",
    )


def stable_session_key(bearer: str) -> str:
    return hash_opaque_secret(bearer)


def new_csrf_nonce() -> str:
    return secrets.token_urlsafe(32)
