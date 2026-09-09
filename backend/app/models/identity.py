from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel, utcnow
from app.models.enums import LMSProvider
from app.models.types import JSONValue


class LMSConnection(UUIDTimestampModel):
    __tablename__ = "core_lmsconnection"

    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(20), default=LMSProvider.MOODLE.value)
    base_url: Mapped[str] = mapped_column(String(200), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    capabilities: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ExternalPrincipal(UUIDTimestampModel):
    __tablename__ = "core_externalprincipal"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_subject", name="unique_external_principal"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="RESTRICT"), index=True
    )
    external_subject: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str] = mapped_column(String(254), default="")
    locale: Mapped[str] = mapped_column(String(20), default="ru")
    profile_revision: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    preferences: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class PrincipalSession(UUIDTimestampModel):
    """Revocable server-side session; only a high-entropy bearer is stored in the cookie."""

    __tablename__ = "core_principalsession"
    __table_args__ = (
        Index("core_principalsession_expires_revoked_idx", "expires_at", "revoked_at"),
    )

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user_agent_hash: Mapped[str] = mapped_column(String(64), default="")
    request_ip_prefix: Mapped[str] = mapped_column(String(64), default="")


class AdminElevation(UUIDTimestampModel):
    __tablename__ = "core_adminelevation"
    __table_args__ = (Index("core_admine_session_expires_idx", "session_key", "expires_at"),)

    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    session_key: Mapped[str] = mapped_column(String(80), index=True)
    token_version: Mapped[str] = mapped_column(String(64), default="env-v1")
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    request_ip_prefix: Mapped[str] = mapped_column(String(64), default="")


class TeacherAccessToken(UUIDTimestampModel):
    """Revocable, administrator-issued teacher token.

    An Argon2id verifier is authoritative for authentication. Newly created or
    rotated tokens also have an AES-GCM-encrypted recoverable copy for the
    protected administrator reveal endpoint; legacy hash-only rows remain
    valid but cannot be revealed. ``public_id`` lets authentication verify
    exactly one hash instead of walking the whole pool.
    """

    __tablename__ = "core_teacheraccesstoken"
    # Keep both objects explicit: migration 0010 creates the named uniqueness
    # constraint as well as the unique lookup index used by token verification.
    __table_args__ = (UniqueConstraint("public_id", name="uq_core_teacheraccesstoken_public_id"),)

    public_id: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    label: Mapped[str] = mapped_column(String(120))
    secret_hash: Mapped[str] = mapped_column(String(255))
    encrypted_secret: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)


class TeacherTokenGrant(UUIDTimestampModel):
    """Persistent one-to-one binding between a token and an LMS principal."""

    __tablename__ = "core_teachertokengrant"
    __table_args__ = (
        UniqueConstraint("token_id", name="unique_teacher_token_grant"),
        UniqueConstraint("principal_id", name="unique_principal_teacher_grant"),
    )

    token_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_teacheraccesstoken.id", ondelete="CASCADE"), index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UsedLaunchNonce(UUIDTimestampModel):
    __tablename__ = "core_usedlaunchnonce"
    __table_args__ = (
        UniqueConstraint("connection_id", "nonce_hash", name="unique_launch_nonce"),
        Index("core_usedla_expires_idx", "expires_at"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="CASCADE"), index=True
    )
    nonce_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class LoginTransaction(UUIDTimestampModel):
    __tablename__ = "core_logintransaction"
    __table_args__ = (Index("core_logint_expires_used_idx", "expires_at", "used_at"),)

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="CASCADE"), index=True
    )
    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    verifier_hash: Mapped[str] = mapped_column(String(64))
    admin_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    teacher_token_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_teacheraccesstoken.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MoodleCredential(UUIDTimestampModel):
    """Server-only, encrypted credential for one principal and Moodle connection."""

    __tablename__ = "core_moodlecredential"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "principal_id",
            "kind",
            name="unique_moodle_principal_credential",
        ),
        Index("core_moodlecredential_status_idx", "connection_id", "status"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="CASCADE"), index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="MOBILE_TOKEN")
    encrypted_secret: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ACTIVE")
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    last_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MoodleLoginAttempt(UUIDTimestampModel):
    """Minimal audit/rate-limit record; no username, password, or token is retained."""

    __tablename__ = "core_moodleloginattempt"
    __table_args__ = (
        Index(
            "core_moodleloginattempt_rate_idx",
            "connection_id",
            "network_hash",
            "attempted_at",
        ),
        Index(
            "core_moodleloginattempt_user_rate_idx",
            "connection_id",
            "username_hash",
            "attempted_at",
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="CASCADE"), index=True
    )
    network_hash: Mapped[str] = mapped_column(String(64), default="")
    username_hash: Mapped[str] = mapped_column(String(64), default="")
    succeeded: Mapped[bool] = mapped_column(Boolean, default=False)
    outcome: Mapped[str] = mapped_column(String(32), default="REJECTED")
    attempted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
