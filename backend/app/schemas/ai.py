from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, Field, StringConstraints, field_validator

from app.models.enums import ChatMode
from app.schemas.common import MutationModel, ReadModel, Revision, Score


class StudentAIThreadCreateRequest(MutationModel):
    attempt: UUID
    course: UUID
    revision: Revision
    title: Annotated[str, StringConstraints(max_length=255)] = ""


class TeacherAIThreadCreateRequest(MutationModel):
    submission: UUID
    course: UUID
    title: Annotated[str, StringConstraints(max_length=255)] = ""


class ChatThreadRead(ReadModel):
    """Thread response deliberately omits the internal prompt/policy version."""

    id: UUID
    mode: ChatMode
    course_id: UUID
    attempt_id: UUID | None = None
    submission_id: UUID | None = None
    title: str = ""
    status: Literal["OPEN", "CLOSED", "ARCHIVED"]
    created_at: datetime
    updated_at: datetime


class ChatMessageCreateRequest(MutationModel):
    content: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    revision: Revision | None = None
    teacher_comment: Annotated[str, StringConstraints(max_length=20_000)] | None = None

    @field_validator("content")
    @classmethod
    def content_is_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class DocumentationCitationRead(ReadModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=500)]
    url: AnyHttpUrl
    source: Annotated[str, StringConstraints(max_length=120)] = ""
    excerpt: Annotated[str, StringConstraints(max_length=2_000)] | None = None


class ChatMessageRead(ReadModel):
    id: UUID
    thread_id: UUID
    role: Literal["USER", "ASSISTANT", "SYSTEM"]
    content: str
    citations: list[DocumentationCitationRead] = Field(default_factory=list, max_length=100)
    safety_outcome: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    created_at: datetime


class TeacherChatMessageRead(ChatMessageRead):
    model: Annotated[str, StringConstraints(max_length=100)] = ""


class AIAvailabilityRead(ReadModel):
    enabled: bool
    student_enabled: bool
    teacher_enabled: bool
    reason: Annotated[str, StringConstraints(max_length=1_000)] = ""


class AICodeReviewRequest(MutationModel):
    retry: bool = False


class AICodeReviewFinding(MutationModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    line: int = Field(ge=1, strict=True)
    end_line: int | None = Field(default=None, ge=1, strict=True)
    severity: Literal["warning", "info", "error"]
    message: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class AICodeReviewReport(MutationModel):
    summary: Annotated[str, StringConstraints(min_length=1, max_length=6_000)]
    findings: list[AICodeReviewFinding] = Field(max_length=100)
    recommended_grade: Score | None = None
    recommended_comment: Annotated[str, StringConstraints(max_length=6_000)] = ""


class AICodeReviewDiscardRequest(MutationModel):
    review_id: UUID


class AICodeReviewRead(ReadModel):
    # Availability of reviewing this submission, not the automatic assessment opt-in.
    enabled: bool
    automatic: bool = False
    id: UUID | None = None
    recommendation_status: Literal["NONE", "PENDING", "APPROVED", "DISCARDED"] = "NONE"
    moodle_refresh_state: str = ""
    moodle_refresh_error: str = ""
    state: Literal[
        "DISABLED", "UNAVAILABLE", "NOT_STARTED", "QUEUED", "RUNNING", "COMPLETED", "FAILED"
    ]
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = 3
    summary: str = ""
    recommended_grade: Score | None = None
    recommended_comment: str = ""
    findings: list[AICodeReviewFinding] = Field(default_factory=list)
    model: str = ""
    error: str = ""


__all__ = [
    "AICodeReviewDiscardRequest",
    "AICodeReviewFinding",
    "AICodeReviewRead",
    "AICodeReviewReport",
    "AICodeReviewRequest",
    "AIAvailabilityRead",
    "ChatMessageCreateRequest",
    "ChatMessageRead",
    "ChatThreadRead",
    "DocumentationCitationRead",
    "StudentAIThreadCreateRequest",
    "TeacherAIThreadCreateRequest",
    "TeacherChatMessageRead",
]
