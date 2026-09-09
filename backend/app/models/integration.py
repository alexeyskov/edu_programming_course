from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel, utcnow
from app.models.enums import SyncOutboxState
from app.models.types import JSONValue


class ExternalMapping(UUIDTimestampModel):
    __tablename__ = "core_externalmapping"
    __table_args__ = (
        UniqueConstraint(
            "connection_id",
            "external_type",
            "external_id",
            name="unique_external_mapping",
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="CASCADE"), index=True
    )
    local_type: Mapped[str] = mapped_column(String(64))
    local_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    external_type: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str] = mapped_column(String(255))
    external_revision: Mapped[str] = mapped_column(String(255), default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONValue, default=dict)


class SyncOutbox(UUIDTimestampModel):
    __tablename__ = "core_syncoutbox"
    __table_args__ = (
        Index("core_syncout_state_next_idx", "state", "next_attempt_at"),
        Index("core_syncout_course_state_created_idx", "course_id", "state", "created_at"),
        Index(
            "core_syncout_attempt_event_created_idx",
            "attempt_id",
            "event_type",
            "created_at",
        ),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="RESTRICT"), index=True
    )
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_course.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100))
    aggregate_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    state: Mapped[str] = mapped_column(String(20), default=SyncOutboxState.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    receipt: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class LMSSubmissionFingerprint(UUIDTimestampModel):
    """Immutable fingerprint of the exact answer successfully delivered to an LMS.

    MD5 is retained because it is useful for interoperating with existing
    operational tooling.  Security decisions are made with SHA-256; MD5 is
    never authoritative on its own.
    """

    __tablename__ = "core_lmssubmissionfingerprint"
    __table_args__ = (
        UniqueConstraint("outbox_id", name="unique_lms_submission_fingerprint_outbox"),
        Index(
            "core_lmsfingerprint_lookup_idx",
            "course_id",
            "assessment_id",
            "principal_id",
            "delivered_at",
        ),
        Index(
            "core_lmsfingerprint_remote_lookup_idx",
            "course_id",
            "module",
            "external_activity_id",
            "principal_id",
            "external_attempt_id",
            "external_question_slot",
            "delivered_at",
        ),
    )

    outbox_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_syncoutbox.id", ondelete="RESTRICT"), index=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="RESTRICT"), index=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_course.id", ondelete="RESTRICT"), index=True
    )
    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_assessment.id", ondelete="RESTRICT"), index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="RESTRICT"), index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_snapshot.id", ondelete="RESTRICT"), index=True
    )
    module: Mapped[str] = mapped_column(String(16))
    external_activity_id: Mapped[str] = mapped_column(String(64))
    external_attempt_id: Mapped[str] = mapped_column(String(160), default="")
    external_question_slot: Mapped[str] = mapped_column(String(64), default="")
    answer_transport: Mapped[str] = mapped_column(String(32))
    artifact_filename: Mapped[str] = mapped_column(String(255))
    artifact_size: Mapped[int] = mapped_column(BigInteger)
    artifact_md5: Mapped[str] = mapped_column(String(32))
    artifact_sha256: Mapped[str] = mapped_column(String(64))
    comparison_md5: Mapped[str] = mapped_column(String(32))
    comparison_sha256: Mapped[str] = mapped_column(String(64))
    canonicalization: Mapped[str] = mapped_column(String(32))
    checkpoint_reason: Mapped[str] = mapped_column(String(32), default="")
    terminal: Mapped[bool] = mapped_column(Boolean, default=False)
    delivered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SystemSetting(UUIDTimestampModel):
    __tablename__ = "core_systemsetting"

    key: Mapped[str] = mapped_column(String(100), unique=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    updated_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)


class AuditEntry(UUIDTimestampModel):
    __tablename__ = "core_auditentry"
    __table_args__ = (Index("core_audit_course_occurred_idx", "course_id", "occurred_at"),)

    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(100), default="")
    object_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_course.id", ondelete="SET NULL"), nullable=True, index=True
    )
    request_id: Mapped[str] = mapped_column(String(100), default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSONValue, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
