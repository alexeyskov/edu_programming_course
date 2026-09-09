from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AliasChoices, Field, StringConstraints, model_validator

from app.models.enums import AssessmentStatus, AssessmentType, AvailabilityTarget
from app.schemas.common import (
    EmptyMutation,
    JsonObject,
    MutationModel,
    ReadModel,
    Score,
    ValidationResult,
)
from app.schemas.tasks import TaskVersionStudentRead

PastePolicy = Literal["INTERNAL_ONLY", "ALLOW", "UNRESTRICTED"]


class AssessmentCreateRequest(MutationModel):
    section_id: UUID | None = None
    type: AssessmentType
    title: Annotated[str, StringConstraints(min_length=1, max_length=255, strip_whitespace=True)]
    instructions: Annotated[str, StringConstraints(max_length=500_000)] = ""
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=604_800)
    attempt_limit: int = Field(default=1, ge=1, le=100)
    max_score: Score
    paste_policy: PastePolicy = "INTERNAL_ONLY"
    student_ai_enabled: bool = False
    teacher_ai_enabled: bool = True
    review_required: bool = True
    decision_support_enabled: bool = True
    autosubmit: bool = True
    multi_file: bool = False
    policy: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_window(self) -> AssessmentCreateRequest:
        if self.opens_at is not None and self.closes_at is not None:
            if self.closes_at <= self.opens_at:
                raise ValueError("closes_at must be later than opens_at")
        return self


class AssessmentUpdateRequest(MutationModel):
    section_id: UUID | None = None
    type: AssessmentType | None = None
    title: (
        Annotated[
            str,
            StringConstraints(min_length=1, max_length=255, strip_whitespace=True),
        ]
        | None
    ) = None
    instructions: Annotated[str, StringConstraints(max_length=500_000)] | None = None
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=604_800)
    attempt_limit: int | None = Field(default=None, ge=1, le=100)
    max_score: Score | None = None
    paste_policy: PastePolicy | None = None
    student_ai_enabled: bool | None = None
    teacher_ai_enabled: bool | None = None
    review_required: bool | None = None
    decision_support_enabled: bool | None = None
    autosubmit: bool | None = None
    multi_file: bool | None = None
    policy: JsonObject | None = None

    @model_validator(mode="after")
    def valid_update(self) -> AssessmentUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        if {"opens_at", "closes_at"}.issubset(self.model_fields_set):
            if self.opens_at is not None and self.closes_at is not None:
                if self.closes_at <= self.opens_at:
                    raise ValueError("closes_at must be later than opens_at")
        return self


class AssessmentItemCreateRequest(MutationModel):
    task_version: UUID
    position: int = Field(default=0, ge=0, le=10_000)
    points: Score
    assignment_rule: JsonObject = Field(default_factory=dict)


class AssessmentItemTeacherRead(ReadModel):
    id: UUID
    assessment_id: UUID
    task_version: UUID = Field(
        validation_alias=AliasChoices("task_version", "task_version_id"),
    )
    position: int = Field(ge=0)
    points: Score
    assignment_rule: JsonObject = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class AvailabilityRuleCreateRequest(MutationModel):
    target_type: AvailabilityTarget
    target_external_id: Annotated[str, StringConstraints(max_length=255)] = ""
    allowed: bool = True
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    attempt_limit: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def valid_target_and_window(self) -> AvailabilityRuleCreateRequest:
        if self.target_type != AvailabilityTarget.COURSE and not self.target_external_id:
            raise ValueError("target_external_id is required for a group or principal rule")
        if self.opens_at is not None and self.closes_at is not None:
            if self.closes_at <= self.opens_at:
                raise ValueError("closes_at must be later than opens_at")
        return self


class AvailabilityRuleRead(ReadModel):
    id: UUID
    assessment_id: UUID
    target_type: AvailabilityTarget
    target_external_id: str
    allowed: bool
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    attempt_limit: int | None = Field(default=None, ge=0, le=100)
    authored_by_id: UUID
    created_at: datetime
    updated_at: datetime


class AssessmentStudentRead(ReadModel):
    """Assessment projection with no assignment rules, policies, or variant linkage."""

    id: UUID
    course_id: UUID
    type: AssessmentType
    title: str
    instructions: str = ""
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=60)
    # ``None`` means that Moodle does not limit the number of attempts.
    attempt_limit: int | None = Field(default=None, ge=1)
    max_score: Score
    paste_policy: PastePolicy
    student_ai_enabled: bool
    review_required: bool
    autosubmit: bool
    multi_file: bool
    status: AssessmentStatus
    published_at: datetime | None = None
    attempt_id: UUID | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    score: Score | None = None
    task: TaskVersionStudentRead | None = None
    # Safe admission hint for clients.  LMS-owned policy and mappings remain
    # teacher-only, but students must not resume these attempts by opening the
    # IDE directly: the start endpoint first has to verify the live Moodle
    # form and bind the concrete Essay question/answer transport.
    requires_live_lms_preparation: bool = False


class AssessmentTeacherRead(AssessmentStudentRead):
    section_id: UUID | None = None
    teacher_ai_enabled: bool
    decision_support_enabled: bool
    policy: JsonObject = Field(default_factory=dict)
    items: list[AssessmentItemTeacherRead] = Field(default_factory=list)
    availability_rules: list[AvailabilityRuleRead] = Field(default_factory=list)
    created_by_id: UUID
    created_at: datetime
    updated_at: datetime


class AssessmentValidateRequest(EmptyMutation):
    pass


class AssessmentPublishRequest(EmptyMutation):
    """Enable an imported LMS work for explicit teacher-owned groups.

    ``principal_ids`` is retained only so an older frontend receives a clear
    domain error instead of a request-shape error.  Moodle decides individual
    availability live when the student opens the work; the application does
    not copy or administer Moodle user overrides.
    """

    group_ids: list[UUID] | None = Field(default=None, max_length=200)
    principal_ids: list[UUID] | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def unique_targets(self) -> AssessmentPublishRequest:
        if self.group_ids is not None and len(self.group_ids) != len(set(self.group_ids)):
            raise ValueError("group_ids must not contain duplicates")
        if self.principal_ids is not None and len(self.principal_ids) != len(
            set(self.principal_ids)
        ):
            raise ValueError("principal_ids must not contain duplicates")
        return self


class AssessmentPublicationGroupRead(ReadModel):
    id: UUID
    external_id: str
    name: str


class AssessmentPublicationPrincipalRead(ReadModel):
    principal_id: UUID
    external_subject: str
    display_name: str
    groups: list[str] = Field(default_factory=list)
    opens_at: datetime | None = None
    closes_at: datetime | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=31_536_000)
    attempt_limit: int | None = Field(default=None, ge=1, le=100)
    attempts_unlimited: bool = False


class AssessmentPublicationTargetsRead(ReadModel):
    groups: list[AssessmentPublicationGroupRead] = Field(default_factory=list)
    # Compatibility fields for already deployed clients. New clients publish
    # only to groups and do not render individual Moodle overrides.
    principals: list[AssessmentPublicationPrincipalRead] = Field(default_factory=list)
    overrides_confirmed: bool = True


class AssessmentValidationRead(ValidationResult):
    assessment_id: UUID


__all__ = [
    "AssessmentCreateRequest",
    "AssessmentItemCreateRequest",
    "AssessmentItemTeacherRead",
    "AssessmentPublishRequest",
    "AssessmentPublicationGroupRead",
    "AssessmentPublicationPrincipalRead",
    "AssessmentPublicationTargetsRead",
    "AssessmentStudentRead",
    "AssessmentTeacherRead",
    "AssessmentUpdateRequest",
    "AssessmentValidateRequest",
    "AssessmentValidationRead",
    "AvailabilityRuleCreateRequest",
    "AvailabilityRuleRead",
    "PastePolicy",
]
