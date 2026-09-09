from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel, utcnow
from app.models.enums import ChatMode, ReviewClaimState
from app.models.types import JSONValue


class ReviewClaim(UUIDTimestampModel):
    __tablename__ = "core_reviewclaim"
    __table_args__ = (
        Index(
            "core_review_submission_state_lease_idx",
            "submission_id",
            "state",
            "lease_expires_at",
        ),
        Index(
            "one_active_review_claim_per_submission",
            "submission_id",
            unique=True,
            postgresql_where=text("state = 'ACTIVE'"),
            sqlite_where=text("state = 'ACTIVE'"),
        ),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    state: Mapped[str] = mapped_column(String(16), default=ReviewClaimState.ACTIVE.value)
    takeover_reason: Mapped[str] = mapped_column(Text, default="")


class ReviewDraft(UUIDTimestampModel):
    __tablename__ = "core_reviewdraft"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="CASCADE"), unique=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer, default=1)
    grade: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    comment: Mapped[str] = mapped_column(Text, default="")
    criterion_scores: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class TeacherExperiment(UUIDTimestampModel):
    __tablename__ = "core_teacherexperiment"

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="CASCADE"), index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    base_snapshot_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(BigInteger, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TeacherExperimentFile(UUIDTimestampModel):
    __tablename__ = "core_teacherexperimentfile"
    __table_args__ = (UniqueConstraint("experiment_id", "path", name="unique_experiment_path"),)

    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_teacherexperiment.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(512))
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))


class ReviewDecision(UUIDTimestampModel):
    __tablename__ = "core_reviewdecision"
    __table_args__ = (
        UniqueConstraint("submission_id", "revision", name="unique_decision_revision"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), index=True
    )
    reviewer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(Integer)
    grade: Mapped[Decimal] = mapped_column(Numeric(8, 2))
    comment: Mapped[str] = mapped_column(Text, default="")
    criterion_scores: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    evidence_ids: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    status: Mapped[str] = mapped_column(String(16), default="APPLIED")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_reviewdecision.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    lms_export_state: Mapped[str] = mapped_column(String(32), default="PENDING")


class ChatThread(UUIDTimestampModel):
    __tablename__ = "core_chatthread"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    mode: Mapped[str] = mapped_column(String(10), default=ChatMode.STUDENT.value)
    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_course.id", ondelete="CASCADE"), index=True
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="CASCADE"), nullable=True, index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_submission.id", ondelete="CASCADE"), nullable=True, index=True
    )
    title: Mapped[str] = mapped_column(String(255), default="")
    policy_version: Mapped[str] = mapped_column(String(64), default="v1")
    status: Mapped[str] = mapped_column(String(16), default="OPEN")


class ChatMessage(UUIDTimestampModel):
    __tablename__ = "core_chatmessage"

    thread_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_chatthread.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    model: Mapped[str] = mapped_column(String(100), default="")
    safety_outcome: Mapped[str] = mapped_column(String(32), default="ALLOWED")
