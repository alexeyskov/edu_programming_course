from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    true,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel
from app.models.enums import (
    AssessmentStatus,
    AssessmentType,
    AvailabilityTarget,
    TaskScope,
    TaskVersionStatus,
)
from app.models.types import JSONValue


class TaskBankItem(UUIDTimestampModel):
    __tablename__ = "core_taskbankitem"
    __table_args__ = (UniqueConstraint("course_id", "slug", name="unique_course_task_slug"),)

    scope: Mapped[str] = mapped_column(String(10), default=TaskScope.COURSE.value)
    course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_course.id", ondelete="CASCADE"), nullable=True, index=True
    )
    slug: Mapped[str] = mapped_column(String(160))
    category: Mapped[str] = mapped_column(String(255), default="")
    tags: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskVersion(UUIDTimestampModel):
    __tablename__ = "core_taskversion"
    __table_args__ = (UniqueConstraint("item_id", "number", name="unique_task_version"),)

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_taskbankitem.id", ondelete="CASCADE"), index=True
    )
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(255))
    statement: Mapped[str] = mapped_column(Text)
    language: Mapped[str] = mapped_column(String(20), default="CPP")
    language_standard: Mapped[str] = mapped_column(String(20), default="C++17")
    multi_file: Mapped[bool] = mapped_column(Boolean, default=False)
    starter_files: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    build_profile: Mapped[str] = mapped_column(String(100), default="cpp-gcc-c++20-single")
    public_examples: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    hidden_test_manifest: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    max_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("10"))
    difficulty: Mapped[str] = mapped_column(String(32), default="")
    ai_policy: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default=TaskVersionStatus.DRAFT.value)
    authored_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Assessment(UUIDTimestampModel):
    __tablename__ = "core_assessment"
    __table_args__ = (
        Index("core_assess_course_status_opens_idx", "course_id", "status", "opens_at"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_course.id", ondelete="CASCADE"), index=True
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_coursesection.id", ondelete="SET NULL"), nullable=True, index=True
    )
    type: Mapped[str] = mapped_column(String(16), default=AssessmentType.LAB.value)
    title: Mapped[str] = mapped_column(String(255))
    instructions: Mapped[str] = mapped_column(Text, default="")
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``NULL`` is the explicit Moodle value "unlimited attempts".  Local
    # assessments still default to one attempt, while imported activities keep
    # the LMS semantics instead of silently turning unlimited into ``1``.
    attempt_limit: Mapped[int | None] = mapped_column(
        SmallInteger,
        default=1,
        nullable=True,
    )
    max_score: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("10"))
    paste_policy: Mapped[str] = mapped_column(String(32), default="INTERNAL_ONLY")
    student_ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    teacher_ai_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    review_required: Mapped[bool] = mapped_column(Boolean, default=True, server_default=true())
    decision_support_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default=true(),
    )
    autosubmit: Mapped[bool] = mapped_column(Boolean, default=True)
    multi_file: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=AssessmentStatus.DRAFT.value)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AssessmentItem(UUIDTimestampModel):
    __tablename__ = "core_assessmentitem"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_assessment.id", ondelete="CASCADE"), index=True
    )
    task_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_taskversion.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    points: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("10"))
    assignment_rule: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class AvailabilityRule(UUIDTimestampModel):
    __tablename__ = "core_availabilityrule"

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_assessment.id", ondelete="CASCADE"), index=True
    )
    target_type: Mapped[str] = mapped_column(String(16), default=AvailabilityTarget.COURSE.value)
    target_external_id: Mapped[str] = mapped_column(String(255), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    opens_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closes_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # ``NULL`` inherits the assessment-wide duration.  A positive value is a
    # Moodle override for the selected group or student.
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # ``NULL`` inherits the assessment-wide Moodle policy, ``0`` explicitly
    # means unlimited attempts, and a positive value is a target-specific
    # Moodle override.
    attempt_limit: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    authored_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
