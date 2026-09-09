from __future__ import annotations

import hashlib
import re
import secrets
import string
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.identity import TeacherAccessToken, TeacherTokenGrant

_TOKEN_PREFIX = "edut"
_SHORT_TOKEN_LENGTH = 8
_SHORT_TOKEN_ALPHABET = string.ascii_letters + string.digits
_SHORT_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8}$")
_PUBLIC_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,24}$")


def generate_teacher_token() -> tuple[str, str]:
    """Return ``(public_id, complete_secret)`` for one administrator issue."""

    # The new human-friendly format deliberately excludes punctuation.  The
    # selector remains derived instead of embedded, and the legacy parser below
    # keeps both earlier eight-character URL-safe tokens and long ``edut_*``
    # tokens valid.
    token = "".join(secrets.choice(_SHORT_TOKEN_ALPHABET) for _ in range(_SHORT_TOKEN_LENGTH))
    return _short_token_public_id(token), token


def _short_token_public_id(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()[:16]


def teacher_token_public_id(token: str) -> str | None:
    if _SHORT_TOKEN_RE.fullmatch(token):
        return _short_token_public_id(token)

    # Keep tokens issued before the eight-character format change valid.
    parts = token.split("_", 2)
    if len(parts) != 3 or parts[0] != _TOKEN_PREFIX or not parts[2]:
        return None
    public_id = parts[1]
    return public_id if _PUBLIC_ID_RE.fullmatch(public_id) else None


async def teacher_token_for_principal(
    db: AsyncSession,
    principal_id: uuid.UUID,
) -> TeacherAccessToken | None:
    return await db.scalar(
        select(TeacherAccessToken)
        .join(TeacherTokenGrant, TeacherTokenGrant.token_id == TeacherAccessToken.id)
        .where(TeacherTokenGrant.principal_id == principal_id)
    )


async def has_teacher_grant(db: AsyncSession, principal_id: uuid.UUID) -> bool:
    return (
        await db.scalar(
            select(TeacherTokenGrant.id).where(TeacherTokenGrant.principal_id == principal_id)
        )
        is not None
    )


async def teacher_membership_is_authorized(
    db: AsyncSession,
    principal_id: uuid.UUID,
) -> bool:
    """Require an active grant even when the configured token pool is empty."""

    return await has_teacher_grant(db, principal_id)


async def bind_teacher_token(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    token: TeacherAccessToken,
) -> TeacherTokenGrant:
    """Bind one issued token to exactly one principal, idempotently."""

    now = utcnow()
    existing_for_token = await db.scalar(
        select(TeacherTokenGrant).where(TeacherTokenGrant.token_id == token.id)
    )
    if existing_for_token is not None and existing_for_token.principal_id != principal_id:
        raise ValueError("TOKEN_ALREADY_BOUND")
    existing_for_principal = await db.scalar(
        select(TeacherTokenGrant)
        .where(TeacherTokenGrant.principal_id == principal_id)
        .with_for_update()
    )
    if existing_for_principal is not None:
        if existing_for_principal.token_id != token.id:
            raise ValueError("PRINCIPAL_ALREADY_BOUND")
        existing_for_principal.last_used_at = now
        grant = existing_for_principal
    elif existing_for_token is not None:
        existing_for_token.last_used_at = now
        grant = existing_for_token
    else:
        grant = TeacherTokenGrant(
            token_id=token.id,
            principal_id=principal_id,
            granted_at=now,
            last_used_at=now,
        )
        db.add(grant)
    token.last_used_at = now
    token.use_count += 1
    await db.flush()
    return grant


def teacher_membership_revision(token_id: uuid.UUID, source_revision: str = "") -> str:
    suffix = source_revision[:180]
    return f"teacher-token:{token_id}:{suffix}" if suffix else f"teacher-token:{token_id}"


def teacher_membership_prefix(token_id: uuid.UUID) -> str:
    return f"teacher-token:{token_id}"
