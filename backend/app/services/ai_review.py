"""Immutable, teacher-only advisory analysis, separate from both chat modes."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.analysis import AICodeReview
from app.models.attempts import Attempt, Snapshot, Submission
from app.models.review import ReviewDecision
from app.models.tasks import Assessment
from app.services.common import DomainError
from app.services.moodle_quiz_session import quiz_question_for_attempt

AI_REVIEW_MAX_ATTEMPTS = 3


def fail_or_retry_ai_review(report: AICodeReview, error: str) -> None:
    """Keep intermediate failures private; a browser refresh cannot reset the budget."""
    report.error = error
    report.updated_at = utcnow()
    if report.discarded_at is None and report.attempt_count < AI_REVIEW_MAX_ATTEMPTS:
        report.state = "QUEUED"
        report.next_attempt_at = utcnow() + timedelta(seconds=2 ** report.attempt_count)
        report.completed_at = None
    else:
        report.state = "FAILED"
        report.next_attempt_at = None
        report.completed_at = utcnow()


def recommendation_status(report: AICodeReview) -> str:
    if report.approved_decision_id is not None:
        return "APPROVED"
    if report.discarded_at is not None:
        return "DISCARDED"
    if report.state != "COMPLETED" or report.recommended_grade is None:
        return "NONE"
    return "PENDING"


def reset_recommendation_lifecycle(report: AICodeReview) -> None:
    report.approved_decision_id = None
    report.discarded_at = None
    report.moodle_refresh_outbox_id = None
    report.moodle_refresh_error = ""


async def require_pending_ai_recommendation(
    db: AsyncSession, submission: Submission, report_id: uuid.UUID,
) -> AICodeReview:
    report = await db.scalar(select(AICodeReview).where(
        AICodeReview.id == report_id,
    ).with_for_update().execution_options(populate_existing=True))
    if (
        report is None or report.submission_id != submission.id
        or report.snapshot_id != submission.snapshot_id
        or recommendation_status(report) != "PENDING"
    ):
        raise DomainError(
            409, "AI_RECOMMENDATION_DISCARDED",
            "Рекомендация ИИ больше неактуальна. "
            "Обновите страницу; действует результат Moodle.",
        )
    return report


async def ai_review_policy_assessment(
    db: AsyncSession,
    assessment: Assessment,
    attempt: Attempt | None = None,
) -> Assessment:
    """Live Quiz children and historical imports share the visible activity's policy."""
    parent_id = None
    if attempt is not None:
        relation = await quiz_question_for_attempt(db, attempt.id)
        if relation is not None:
            root = await db.get(Attempt, relation.root_attempt_id)
            if root is None:
                raise DomainError(409, "AI_REVIEW_CONTEXT_MISSING", "Quiz context is missing")
            parent_id = root.assessment_id
    if parent_id is None and (assessment.policy or {}).get("moodle_quiz_question_split") is True:
        try:
            parent_id = uuid.UUID(str(assessment.policy.get("moodle_parent_assessment_id", "")))
        except ValueError as exc:
            raise DomainError(409, "AI_REVIEW_CONTEXT_MISSING", "Quiz context is missing") from exc
    if parent_id is None or parent_id == assessment.id:
        return assessment
    parent = await db.get(Assessment, parent_id)
    if parent is None or parent.course_id != assessment.course_id:
        raise DomainError(409, "AI_REVIEW_CONTEXT_MISSING", "Quiz context is inconsistent")
    return parent


async def enqueue_ai_code_review(
    db: AsyncSession,
    submission: Submission,
    *,
    retry_failed: bool = False,
) -> AICodeReview | None:
    """Enqueue in the submission transaction; never delay submission with a model call."""
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    if assessment is None or attempt is None:
        return None
    policy = await ai_review_policy_assessment(db, assessment, attempt)
    if not policy.ai_review_enabled or await has_teacher_review(db, submission):
        return None
    if submission.source == "MOODLE_IMPORT" and (
        (submission.external_receipt or {}).get("source_complete") is False
        or (submission.external_receipt or {}).get("source_refresh_complete") is False
    ):
        return None
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        return None
    query = select(AICodeReview).where(
        AICodeReview.submission_id == submission.id,
        AICodeReview.snapshot_id == snapshot.id,
    )
    existing = await db.scalar(query)
    if existing is not None:
        if existing.discarded_at is not None:
            # Only another explicit teacher request may restart a dismissed job.
            return existing
        if existing.state == "SKIPPED" or (retry_failed and existing.state == "FAILED"):
            # A fresh automatic opt-in is independent of an older failed manual request.
            existing.manual_requested = False
            existing.requested_with_system_access = False
            existing.requested_by_id = policy.created_by_id
            existing.state = "QUEUED"
            existing.attempt_count = 0
            existing.next_attempt_at = None
            existing.run_token = uuid.uuid4().hex
            existing.summary = ""
            existing.recommended_grade = None
            existing.recommended_comment = ""
            reset_recommendation_lifecycle(existing)
            existing.prompt_version = "code-review-v2"
            existing.findings = []
            existing.error = ""
            existing.model = ""
            existing.completed_at = None
            existing.updated_at = utcnow()
        return existing
    try:
        async with db.begin_nested():
            report = AICodeReview(
                submission_id=submission.id,
                snapshot_id=snapshot.id,
                requested_by_id=policy.created_by_id,
                manifest_hash=snapshot.manifest_hash,
            )
            db.add(report)
            await db.flush()
    except IntegrityError:
        return await db.scalar(query)
    return report


async def has_teacher_review(db: AsyncSession, submission: Submission) -> bool:
    """Human decisions (including a zero mark or comment-only Moodle feedback) win."""
    candidates = [submission]
    receipt = submission.external_receipt or {}
    remote_attempt = receipt.get("moodle_parent_attempt_id")
    slot = receipt.get("moodle_response_id")
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    if remote_attempt and slot and attempt is not None and assessment is not None:
        # Native Quiz answers and their Moodle read-backs have different local
        # IDs. A reviewed read-back must also prevent backfilling AI on its twin.
        candidates.extend((await db.scalars(
            select(Submission)
            .join(Attempt, Attempt.id == Submission.attempt_id)
            .join(Assessment, Assessment.id == Attempt.assessment_id)
            .where(
                Submission.id != submission.id,
                Submission.source == "MOODLE_IMPORT",
                Attempt.state != "VOID",
                Attempt.principal_id == attempt.principal_id,
                Assessment.course_id == assessment.course_id,
                Submission.external_receipt["moodle_parent_attempt_id"].as_string()
                == str(remote_attempt),
                Submission.external_receipt["moodle_response_id"].as_string() == str(slot),
            )
        )).all())
    for candidate in candidates:
        feedback = (candidate.external_receipt or {}).get("moodle_review") or {}
        if feedback.get("grade") is not None or str(feedback.get("comment") or "").strip():
            return True
    return await db.scalar(select(ReviewDecision.id).where(
        ReviewDecision.submission_id.in_([candidate.id for candidate in candidates]),
        ReviewDecision.status == "APPLIED",
    ).limit(1)) is not None
