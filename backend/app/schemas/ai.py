from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, Field, StringConstraints, field_validator

from app.models.enums import ChatMode
from app.schemas.common import MutationModel, ReadModel, Revision


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


__all__ = [
    "AIAvailabilityRead",
    "ChatMessageCreateRequest",
    "ChatMessageRead",
    "ChatThreadRead",
    "DocumentationCitationRead",
    "StudentAIThreadCreateRequest",
    "TeacherAIThreadCreateRequest",
    "TeacherChatMessageRead",
]
