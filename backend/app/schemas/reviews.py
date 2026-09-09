from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from app.models.enums import ReviewClaimState
from app.schemas.attempts import AttemptHistoryEventRead, WorkspaceFileRead
from app.schemas.common import EmptyMutation, JsonObject, MutationModel, ReadModel, Revision, Score

SubmissionReviewStatus = Literal["UNGRADED", "CLAIMED", "GRADED", "CONFLICT"]
IntegrityRisk = Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
OriginVerificationState = Literal[
    "VERIFIED", "EXTERNAL_ORIGIN", "MISMATCH", "PENDING", "UNAVAILABLE"
]


class SubmissionOriginVerificationRead(ReadModel):
    state: OriginVerificationState
    transport: Annotated[str, StringConstraints(max_length=32)] | None = None
    checked_at: datetime | None = None
    message: Annotated[str, StringConstraints(min_length=1, max_length=1_000)]


class ReviewClaimCreateRequest(MutationModel):
    takeover_reason: Annotated[str, StringConstraints(max_length=2_000)] | None = None


class ReviewClaimHeartbeatRequest(EmptyMutation):
    pass


class ReviewClaimRead(ReadModel):
    id: UUID
    submission_id: UUID
    owner_id: UUID
    owner_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    lease_expires_at: datetime
    heartbeat_at: datetime
    state: ReviewClaimState
    mine: bool
    takeover_reason: Annotated[str, StringConstraints(max_length=2_000)] = ""


class SubmissionReviewGroupItemRead(ReadModel):
    submission_id: UUID
    position: int = Field(ge=1, le=1_000)
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    score: Score | None = None
    max_score: Score
    status: SubmissionReviewStatus


class SubmissionReviewGroupRead(ReadModel):
    id: UUID
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None
    items: list[SubmissionReviewGroupItemRead] = Field(default_factory=list, max_length=1_000)


class SubmissionListItemRead(ReadModel):
    id: UUID
    assessment_id: UUID
    course_id: UUID
    course_title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    assessment_title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    student_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    student_group: Annotated[str, StringConstraints(max_length=255)] = ""
    submitted_at: datetime
    status: SubmissionReviewStatus
    score: Score | None = None
    max_score: Score
    claim: ReviewClaimRead | None = None
    risk: IntegrityRisk = "UNKNOWN"
    tests_passed: int = Field(default=0, ge=0)
    tests_total: int = Field(default=0, ge=0)
    review_required: bool = True
    decision_support_enabled: bool = True
    can_review: bool = False
    review_group: SubmissionReviewGroupRead | None = None


class SubmissionDecisionRead(ReadModel):
    id: UUID
    submission_id: UUID
    reviewer_id: UUID
    reviewer_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    revision: int = Field(ge=1)
    grade: Score
    comment: str
    criterion_scores: JsonObject = Field(default_factory=dict)
    evidence_ids: list[UUID] = Field(default_factory=list)
    status: Annotated[str, StringConstraints(min_length=1, max_length=16)]
    supersedes_id: UUID | None = None
    lms_export_state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    created_at: datetime
    updated_at: datetime


class SubmissionTeacherRead(SubmissionListItemRead):
    attempt_id: UUID
    assigned_task_version_id: UUID | None
    snapshot_id: UUID
    revision: Revision
    source: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    late: bool
    lms_export_state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    files: list[WorkspaceFileRead] = Field(default_factory=list, max_length=256)
    history: list[AttemptHistoryEventRead] = Field(default_factory=list, max_length=1_000_000)
    latest_decision: SubmissionDecisionRead | None = None
    decision_history: list[SubmissionDecisionRead] = Field(default_factory=list, max_length=10_000)
    origin_verification: SubmissionOriginVerificationRead
    created_at: datetime
    updated_at: datetime


class ReviewDraftUpsertRequest(MutationModel):
    grade: Score | None = None
    comment: Annotated[str, StringConstraints(max_length=50_000)] = ""
    criterion_scores: JsonObject = Field(default_factory=dict)


class ReviewDraftRead(ReadModel):
    id: UUID
    submission_id: UUID
    owner_id: UUID
    revision: int = Field(ge=1)
    grade: Score | None = None
    comment: str
    criterion_scores: JsonObject = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class ReviewDecisionCreateRequest(MutationModel):
    grade: Score
    comment: Annotated[str, StringConstraints(max_length=50_000)] = ""
    criterion_scores: JsonObject = Field(default_factory=dict)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=10_000)


class ReviewDecisionRead(ReadModel):
    id: UUID
    submission_id: UUID
    reviewer_id: UUID
    revision: int = Field(ge=1)
    grade: Score
    comment: str
    criterion_scores: JsonObject = Field(default_factory=dict)
    evidence_ids: list[UUID] = Field(default_factory=list)
    status: Annotated[str, StringConstraints(min_length=1, max_length=16)]
    supersedes_id: UUID | None = None
    lms_export_state: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    created_at: datetime
    updated_at: datetime


class TeacherExperimentCreateRequest(EmptyMutation):
    pass


class TeacherExperimentFileRead(ReadModel):
    id: UUID
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    content: str
    language: Annotated[str, StringConstraints(max_length=20)] | None = None
    read_only: bool = False


class TeacherExperimentRead(ReadModel):
    id: UUID
    submission_id: UUID
    revision: Revision
    files: list[TeacherExperimentFileRead] = Field(default_factory=list, max_length=256)
    changed: bool
    expires_at: datetime
    reset_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class TeacherExperimentFilePatchRequest(MutationModel):
    content: Annotated[str, StringConstraints(max_length=2_097_152)]


class TeacherExperimentFilePatchRead(ReadModel):
    file: TeacherExperimentFileRead
    revision: Revision


class TeacherExperimentResetRequest(EmptyMutation):
    pass


__all__ = [
    "IntegrityRisk",
    "ReviewClaimCreateRequest",
    "ReviewClaimHeartbeatRequest",
    "ReviewClaimRead",
    "ReviewDecisionCreateRequest",
    "ReviewDecisionRead",
    "ReviewDraftRead",
    "ReviewDraftUpsertRequest",
    "SubmissionListItemRead",
    "SubmissionDecisionRead",
    "SubmissionReviewGroupItemRead",
    "SubmissionReviewGroupRead",
    "SubmissionReviewStatus",
    "SubmissionTeacherRead",
    "TeacherExperimentCreateRequest",
    "TeacherExperimentFilePatchRead",
    "TeacherExperimentFilePatchRequest",
    "TeacherExperimentFileRead",
    "TeacherExperimentRead",
    "TeacherExperimentResetRequest",
]
