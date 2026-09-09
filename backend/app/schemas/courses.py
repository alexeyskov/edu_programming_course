from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AliasChoices, AnyHttpUrl, Field, StringConstraints

from app.models.enums import CourseImportState, CourseRole
from app.schemas.common import EmptyMutation, JsonObject, MutationModel, ReadModel


class CourseRead(ReadModel):
    """Course projection returned to a member; it intentionally excludes course policies."""

    id: UUID
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    short_name: Annotated[str, StringConstraints(max_length=120)] = ""
    description: str = ""
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    role: CourseRole
    group_name: Annotated[str, StringConstraints(max_length=255)] | None = None
    term: Annotated[str, StringConstraints(max_length=120)] = ""
    provider_name: Annotated[str, StringConstraints(max_length=120)] = ""
    external_url: AnyHttpUrl | None = None
    sync_status: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    sync_error_code: Annotated[str, StringConstraints(max_length=64)] = ""
    sync_error_message: Annotated[str, StringConstraints(max_length=4000)] = ""
    sync_error_at: datetime | None = None
    sync_error_retryable: bool = False
    synced_at: datetime | None = None
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    active_count: int | None = Field(default=None, ge=0)
    unchecked_count: int | None = Field(default=None, ge=0)


class CourseTeacherRead(CourseRead):
    connection_id: UUID
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    external_revision: Annotated[str, StringConstraints(max_length=255)] = ""
    policies: JsonObject = Field(default_factory=dict)
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CourseSectionRead(ReadModel):
    id: UUID
    course_id: UUID
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    position: int = Field(ge=0)
    visible: bool


class CourseGroupRead(ReadModel):
    id: UUID
    course_id: UUID
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    kind: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    active: bool


class CourseCatalogRead(ReadModel):
    id: UUID
    connection_id: UUID
    connection_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    short_name: Annotated[str, StringConstraints(max_length=120)] = ""
    external_url: AnyHttpUrl
    sync_status: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    sync_error_code: Annotated[str, StringConstraints(max_length=64)] = ""
    sync_error_message: Annotated[str, StringConstraints(max_length=4000)] = ""
    sync_error_at: datetime | None = None
    sync_error_retryable: bool = False
    added_at: datetime | None = None


class CourseSyncRequest(EmptyMutation):
    pass


class CourseSyncRead(ReadModel):
    course_id: UUID
    status: Literal["PENDING", "RUNNING", "COMPLETED", "FAILED"]
    requested_at: datetime


class CourseImportCreateRequest(MutationModel):
    url: AnyHttpUrl


class CourseImportConfirmRequest(EmptyMutation):
    pass


class CourseImportRead(ReadModel):
    id: UUID
    state: CourseImportState
    external_course_id: Annotated[str, StringConstraints(max_length=255)]
    preview: JsonObject = Field(default_factory=dict)
    capability_report: JsonObject = Field(default_factory=dict)
    confirmed_course: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("confirmed_course", "confirmed_course_id"),
    )
    error: Annotated[str, StringConstraints(max_length=20_000)] = ""
    created_at: datetime
    updated_at: datetime


__all__ = [
    "CourseCatalogRead",
    "CourseGroupRead",
    "CourseImportConfirmRequest",
    "CourseImportCreateRequest",
    "CourseImportRead",
    "CourseRead",
    "CourseSectionRead",
    "CourseSyncRead",
    "CourseSyncRequest",
    "CourseTeacherRead",
]
