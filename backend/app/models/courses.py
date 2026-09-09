from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDTimestampModel, utcnow
from app.models.enums import CourseImportState, CourseRole
from app.models.types import JSONValue


class Course(UUIDTimestampModel):
    __tablename__ = "core_course"
    __table_args__ = (
        UniqueConstraint("connection_id", "external_id", name="unique_course"),
        Index("core_course_connection_catalog_idx", "connection_id", "catalog_enabled"),
    )

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="RESTRICT"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    short_name: Mapped[str] = mapped_column(String(120), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Moscow")
    external_revision: Mapped[str] = mapped_column(String(255), default="")
    sync_status: Mapped[str] = mapped_column(String(32), default="CURRENT")
    sync_error_code: Mapped[str] = mapped_column(String(64), default="")
    sync_error_message: Mapped[str] = mapped_column(Text, default="")
    sync_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sync_error_retryable: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    policies: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The global catalogue is an application-level allow-list.  No code path
    # may expose a newly materialised LMS course until an administrator has
    # explicitly confirmed it.
    catalog_enabled: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default=false(),
    )
    catalog_added_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class CourseSection(UUIDTimestampModel):
    __tablename__ = "core_coursesection"
    __table_args__ = (UniqueConstraint("course_id", "external_id", name="unique_course_section"),)

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_course.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(255))
    position: Mapped[int] = mapped_column(Integer, default=0)
    visible: Mapped[bool] = mapped_column(Boolean, default=True)
    external_revision: Mapped[str] = mapped_column(String(255), default="")


class CourseGroup(UUIDTimestampModel):
    __tablename__ = "core_coursegroup"
    __table_args__ = (UniqueConstraint("course_id", "external_id", name="unique_course_group"),)

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_course.id", ondelete="CASCADE"), index=True
    )
    external_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(32), default="GROUP")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CourseMembership(UUIDTimestampModel):
    __tablename__ = "core_coursemembership"
    __table_args__ = (
        CheckConstraint(
            "role IN ('STUDENT', 'TEACHER')",
            name="coursemembership_supported_role",
        ),
        UniqueConstraint("course_id", "principal_id", "role", name="unique_course_membership_role"),
        Index("core_course_principal_role_active_idx", "principal_id", "role", "active"),
    )

    course_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_course.id", ondelete="CASCADE"), index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(10), default=CourseRole.STUDENT.value)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    external_revision: Mapped[str] = mapped_column(String(255), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CourseMembershipGroup(Base):
    """Stable association table for LMS-projected membership groups."""

    __tablename__ = "core_coursemembership_groups"
    __table_args__ = (
        UniqueConstraint(
            "coursemembership_id", "coursegroup_id", name="unique_course_membership_group"
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True
    )
    coursemembership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_coursemembership.id", ondelete="CASCADE"), index=True
    )
    coursegroup_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_coursegroup.id", ondelete="CASCADE"), index=True
    )


class CourseImportJob(UUIDTimestampModel):
    __tablename__ = "core_courseimportjob"

    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_lmsconnection.id", ondelete="RESTRICT"), index=True
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    locator_hash: Mapped[str] = mapped_column(String(64))
    external_course_id: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20), default=CourseImportState.DISCOVERED.value)
    preview: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    capability_report: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    confirmed_course_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_course.id", ondelete="SET NULL"), nullable=True, index=True
    )
