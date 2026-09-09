from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel, utcnow
from app.models.enums import AnalysisState, AuthorshipAnalysisState, PlagiarismCaseState
from app.models.types import JSONValue


class AuthorshipAnalysisJob(UUIDTimestampModel):
    __tablename__ = "core_authorshipanalysisjob"
    __table_args__ = (
        Index("core_author_submission_state_created_idx", "submission_id", "state", "created_at"),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), index=True
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    manifest_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    export_schema_version: Mapped[str] = mapped_column(String(16), default="1.0")
    state: Mapped[str] = mapped_column(String(16), default=AuthorshipAnalysisState.PENDING.value)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str] = mapped_column(String(64), default="")
    error: Mapped[str] = mapped_column(Text, default="")


class AuthorshipAnalysisResult(UUIDTimestampModel):
    __tablename__ = "core_authorshipanalysisresult"
    __table_args__ = (
        CheckConstraint(
            "probability >= 0 AND probability <= 1",
            name="authorship_probability_between_zero_and_one",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="authorship_confidence_between_zero_and_one",
        ),
        CheckConstraint(
            "uncertainty >= 0 AND uncertainty <= 1",
            name="authorship_uncertainty_between_zero_and_one",
        ),
    )

    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_authorshipanalysisjob.id", ondelete="CASCADE"), unique=True
    )
    manifest_hash: Mapped[str] = mapped_column(String(64))
    probability: Mapped[Decimal] = mapped_column(Numeric(7, 6))
    confidence: Mapped[Decimal] = mapped_column(Numeric(7, 6))
    uncertainty: Mapped[Decimal] = mapped_column(Numeric(7, 6))
    analyzer: Mapped[str] = mapped_column(String(200))
    model: Mapped[str] = mapped_column(String(200))
    calibration: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    features: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    response_hash: Mapped[str] = mapped_column(String(64))


class SimilarityAnalysis(UUIDTimestampModel):
    __tablename__ = "core_similarityanalysis"
    __table_args__ = (
        Index(
            "core_similarity_assessment_task_created_idx",
            "assessment_id",
            "task_version_id",
            "created_at",
        ),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_assessment.id", ondelete="RESTRICT"), index=True
    )
    task_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_taskversion.id", ondelete="RESTRICT"), index=True
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    algorithm_version: Mapped[str] = mapped_column(String(64), default="cpp-lexical-winnowing-v1")
    config: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    state: Mapped[str] = mapped_column(String(16), default=AnalysisState.PENDING.value)
    submission_count: Mapped[int] = mapped_column(Integer, default=0)
    comparison_count: Mapped[int] = mapped_column(Integer, default=0)
    match_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class SimilarityMatch(UUIDTimestampModel):
    __tablename__ = "core_similaritymatch"
    __table_args__ = (
        UniqueConstraint(
            "analysis_id",
            "submission_a_id",
            "submission_b_id",
            name="unique_similarity_pair_per_analysis",
        ),
        CheckConstraint("score >= 0 AND score <= 1", name="similarity_score_between_zero_and_one"),
        CheckConstraint(
            "submission_a_id <> submission_b_id",
            name="similarity_pair_uses_distinct_submissions",
        ),
    )

    analysis_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_similarityanalysis.id", ondelete="CASCADE"), index=True
    )
    submission_a_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), index=True
    )
    submission_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), index=True
    )
    manifest_hash_a: Mapped[str] = mapped_column(String(64))
    manifest_hash_b: Mapped[str] = mapped_column(String(64))
    score: Mapped[Decimal] = mapped_column(Numeric(7, 6))
    fingerprint_count_a: Mapped[int] = mapped_column(Integer, default=0)
    fingerprint_count_b: Mapped[int] = mapped_column(Integer, default=0)
    shared_fingerprint_count: Mapped[int] = mapped_column(Integer, default=0)
    evidence: Mapped[list[Any]] = mapped_column(JSONValue, default=list)


class PlagiarismCase(UUIDTimestampModel):
    __tablename__ = "core_plagiarismcase"

    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_similaritymatch.id", ondelete="CASCADE"), unique=True
    )
    state: Mapped[str] = mapped_column(String(20), default=PlagiarismCaseState.SUSPECTED.value)
    teacher_comment: Mapped[str] = mapped_column(Text, default="")
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    state_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
