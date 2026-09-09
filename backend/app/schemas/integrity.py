from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.models.enums import (
    AnalysisState,
    AuthorshipAnalysisState,
    PlagiarismCaseState,
)
from app.schemas.common import (
    EmptyMutation,
    JsonObject,
    MutationModel,
    Probability,
    ReadModel,
    Revision,
    SHA256Hex,
)


class AuthorshipExportFile(ReadModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    content: Annotated[str, StringConstraints(max_length=2_097_152)]
    content_hash: SHA256Hex


class AuthorshipExportEditEvent(ReadModel):
    epoch: int = Field(ge=1)
    sequence: Revision
    file_path: Annotated[str, StringConstraints(max_length=512)] | None = None
    source: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    event_type: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    changes: list[JsonObject] = Field(default_factory=list, max_length=100_000)
    received_at: datetime
    event_hash: SHA256Hex


class AuthorshipSubmissionExport(ReadModel):
    """Pseudonymous analyzer payload; identity, group, LMS and grade fields do not exist."""

    schema_version: Annotated[str, StringConstraints(min_length=1, max_length=16)]
    pseudonymous_submission_id: Annotated[
        str,
        StringConstraints(min_length=32, max_length=128),
    ]
    manifest_hash: SHA256Hex
    submitted_at: datetime
    revision: Revision
    files: list[AuthorshipExportFile] = Field(min_length=1, max_length=256)
    edit_history: list[AuthorshipExportEditEvent] = Field(max_length=1_000_000)


class AuthorshipAnalyzerResultInput(MutationModel):
    manifest_hash: SHA256Hex
    probability: Probability
    confidence: Probability
    uncertainty: Probability
    analyzer: Annotated[
        str,
        StringConstraints(min_length=1, max_length=200, strip_whitespace=True),
    ]
    model: Annotated[
        str,
        StringConstraints(min_length=1, max_length=200, strip_whitespace=True),
    ]
    calibration: JsonObject
    features: JsonObject = Field(default_factory=dict)
    warnings: list[Annotated[str, StringConstraints(max_length=2_000)]] = Field(
        default_factory=list,
        max_length=100,
    )

    @field_validator("calibration")
    @classmethod
    def calibration_is_explicit(cls, value: JsonObject) -> JsonObject:
        version = value.get("version")
        if not isinstance(version, str) or not version.strip() or len(version.strip()) > 200:
            raise ValueError("calibration.version must identify the calibration")
        return value


class AuthorshipAnalysisTriggerRequest(EmptyMutation):
    pass


class AuthorshipAnalysisResultRead(ReadModel):
    manifest_hash: SHA256Hex
    probability: Probability
    confidence: Probability
    uncertainty: Probability
    analyzer: str
    model: str
    calibration: JsonObject
    features: JsonObject = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    response_hash: SHA256Hex
    created_at: datetime


class AuthorshipAnalysisRead(ReadModel):
    id: UUID
    submission_id: UUID
    manifest_hash: SHA256Hex
    payload_hash: SHA256Hex
    export_schema_version: str
    state: AuthorshipAnalysisState
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error_code: Annotated[str, StringConstraints(max_length=64)] = ""
    error: Annotated[str, StringConstraints(max_length=20_000)] = ""
    result: AuthorshipAnalysisResultRead | None = None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def completed_result_is_exact(self) -> AuthorshipAnalysisRead:
        if self.state == AuthorshipAnalysisState.COMPLETED and self.result is None:
            raise ValueError("a completed analysis must contain a validated result")
        if self.state != AuthorshipAnalysisState.COMPLETED and self.result is not None:
            raise ValueError(
                "an unfinished or failed analysis must not expose a probability result"
            )
        if self.result is not None and self.result.manifest_hash != self.manifest_hash:
            raise ValueError("result manifest_hash must match the exact exported manifest")
        return self


class SimilarityAnalysisTriggerRequest(MutationModel):
    task_version_id: UUID | None = None


class SimilarityEvidenceFragment(ReadModel):
    file_a: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    start_line_a: int = Field(ge=1)
    end_line_a: int = Field(ge=1)
    file_b: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    start_line_b: int = Field(ge=1)
    end_line_b: int = Field(ge=1)
    token_count: int = Field(ge=1)
    excerpt_a: Annotated[str, StringConstraints(max_length=8_000)] = ""
    excerpt_b: Annotated[str, StringConstraints(max_length=8_000)] = ""

    @model_validator(mode="after")
    def ordered_lines(self) -> SimilarityEvidenceFragment:
        if self.end_line_a < self.start_line_a or self.end_line_b < self.start_line_b:
            raise ValueError("evidence end lines must not precede start lines")
        return self


class PlagiarismCaseUpdateRequest(MutationModel):
    state: PlagiarismCaseState
    teacher_comment: Annotated[str, StringConstraints(max_length=50_000)] = ""

    @field_validator("teacher_comment")
    @classmethod
    def normalize_comment(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def terminal_state_has_comment(self) -> PlagiarismCaseUpdateRequest:
        terminal_states = {
            PlagiarismCaseState.CONFIRMED,
            PlagiarismCaseState.DISMISSED,
            PlagiarismCaseState.INCONCLUSIVE,
        }
        if self.state in terminal_states and not self.teacher_comment:
            raise ValueError("teacher_comment is required for a terminal case state")
        return self


class PlagiarismCaseRead(ReadModel):
    id: UUID
    match_id: UUID
    state: PlagiarismCaseState
    teacher_comment: str
    updated_by_id: UUID | None = None
    state_changed_at: datetime
    created_at: datetime
    updated_at: datetime


class SimilarityMatchRead(ReadModel):
    id: UUID
    submission_a_id: UUID
    submission_b_id: UUID
    manifest_hash_a: SHA256Hex
    manifest_hash_b: SHA256Hex
    score: Probability
    fingerprint_count_a: int = Field(ge=0)
    fingerprint_count_b: int = Field(ge=0)
    shared_fingerprint_count: int = Field(ge=0)
    evidence: list[SimilarityEvidenceFragment] = Field(default_factory=list, max_length=10_000)
    case: PlagiarismCaseRead | None = None


class SimilaritySourceFileRead(ReadModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    language: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    content: Annotated[str, StringConstraints(max_length=2_097_152)]


class SimilaritySubmissionSideRead(ReadModel):
    submission_id: UUID
    student_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    student_group: Annotated[str, StringConstraints(max_length=255)] = ""
    submitted_at: datetime
    files: list[SimilaritySourceFileRead] = Field(default_factory=list, max_length=256)


class SimilarityComparisonRead(ReadModel):
    """A narrowly-authorised, immutable two-submission plagiarism view."""

    match: SimilarityMatchRead
    assessment_id: UUID
    assessment_title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    left: SimilaritySubmissionSideRead
    right: SimilaritySubmissionSideRead


class SimilarityAnalysisRead(ReadModel):
    id: UUID
    assessment_id: UUID
    task_version_id: UUID
    algorithm_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    config: JsonObject = Field(default_factory=dict)
    state: AnalysisState
    submission_count: int = Field(ge=0)
    comparison_count: int = Field(ge=0)
    match_count: int = Field(ge=0)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: Annotated[str, StringConstraints(max_length=20_000)] = ""
    matches: list[SimilarityMatchRead] = Field(default_factory=list, max_length=100_000)
    created_at: datetime
    updated_at: datetime


__all__ = [
    "AuthorshipAnalysisRead",
    "AuthorshipAnalysisResultRead",
    "AuthorshipAnalysisTriggerRequest",
    "AuthorshipAnalyzerResultInput",
    "AuthorshipExportEditEvent",
    "AuthorshipExportFile",
    "AuthorshipSubmissionExport",
    "PlagiarismCaseRead",
    "PlagiarismCaseUpdateRequest",
    "SimilarityAnalysisRead",
    "SimilarityAnalysisTriggerRequest",
    "SimilarityComparisonRead",
    "SimilarityEvidenceFragment",
    "SimilarityMatchRead",
    "SimilaritySourceFileRead",
    "SimilaritySubmissionSideRead",
]
