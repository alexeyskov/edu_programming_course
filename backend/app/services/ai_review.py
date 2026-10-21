"""Immutable, teacher-only advisory analysis, separate from both chat modes."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import or_, select
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


def _feedback_remote_attempt_id(value: object) -> str | None:
    """Only numeric Quiz IDs have the documented ``attempt:`` spelling alias."""
    if isinstance(value, bool) or not isinstance(value, str | int):
        return None
    raw = str(value).strip()
    if not raw or len(raw) > 160:
        return None
    numeric = raw.removeprefix("attempt:")
    if numeric.isascii() and numeric.isdecimal():
        return str(int(numeric)) if int(numeric) > 0 else None
    return raw  # Assignment/other opaque identities remain exact, not fuzzy aliases.


async def _feedback_question_identity(
    db: AsyncSession, submission: Submission, attempt: Attempt, assessment: Assessment,
) -> tuple[uuid.UUID, str, str] | None:
    """Resolve one parent activity, remote attempt and exact question slot.

    Before delivery, a native Quiz receipt can be empty. The durable relation
    and both pinned integrity policies must agree before filling that gap;
    conflicting receipt fields never get overridden by a convenient fallback.
    """
    receipt = submission.external_receipt or {}
    integrity = attempt.integrity_policy or {}
    policy = assessment.policy or {}
    question = await quiz_question_for_attempt(db, attempt.id)
    parent_id = assessment.id
    remote = _feedback_remote_attempt_id(receipt.get("moodle_parent_attempt_id"))
    slot = str(receipt.get("moodle_response_id") or "").strip()
    if question is not None:
        root = await db.get(Attempt, question.root_attempt_id)
        parent = await db.get(Assessment, root.assessment_id) if root is not None else None
        root_integrity = (root.integrity_policy or {}) if root is not None else {}
        pinned_remote = _feedback_remote_attempt_id(integrity.get("moodle_attempt_id"))
        if (
            root is None or parent is None
            or attempt.state not in {"SUBMITTED", "AUTO_SUBMITTED", "LOCKED"}
            or root.state not in {"SUBMITTED", "AUTO_SUBMITTED", "LOCKED"}
            or root.principal_id != attempt.principal_id
            or parent.course_id != assessment.course_id
            or str(integrity.get("moodle_quiz_root_attempt_id")) != str(root.id)
            or str(root_integrity.get("moodle_quiz_root_attempt_id")) != str(root.id)
            or str(integrity.get("moodle_parent_assessment_id")) != str(parent.id)
            or str(root_integrity.get("moodle_parent_assessment_id")) != str(parent.id)
            or not pinned_remote or not pinned_remote.isascii() or not pinned_remote.isdecimal()
            or pinned_remote != _feedback_remote_attempt_id(root_integrity.get("moodle_attempt_id"))
            or (remote is not None and remote != pinned_remote)
            or (slot and slot != question.question_slot)
            or str(integrity.get("moodle_question_slot")) != question.question_slot
        ):
            return None
        parent_id, remote, slot = parent.id, pinned_remote, question.question_slot
    else:
        if integrity.get("moodle_quiz_root_attempt_id"):
            return None  # A textual root pointer without its durable relation is not proof.
        raw_parent = policy.get("moodle_parent_assessment_id")
        if raw_parent not in (None, ""):
            try:
                parent_id = uuid.UUID(str(raw_parent))
            except (ValueError, TypeError, AttributeError):
                return None
        elif policy.get("moodle_quiz_question_split") is True:
            return None
        if parent_id != assessment.id:
            parent = await db.get(Assessment, parent_id)
            if parent is None or parent.course_id != assessment.course_id:
                return None
    for value in (
        policy.get("moodle_parent_assessment_id"),
        receipt.get("moodle_parent_assessment_id"),
        integrity.get("moodle_parent_assessment_id"),
    ):
        if value not in (None, ""):
            try:
                if uuid.UUID(str(value)) != parent_id:
                    return None
            except (ValueError, TypeError, AttributeError):
                return None
    if (
        not remote or not slot
        or (policy.get("moodle_response_id") not in (None, "", slot))
        or (receipt.get("moodle_parent_attempt_id") not in (None, "")
            and _feedback_remote_attempt_id(receipt["moodle_parent_attempt_id"]) != remote)
    ):
        return None
    return parent_id, remote, slot


async def has_teacher_review(db: AsyncSession, submission: Submission) -> bool:
    """Human decisions (including a zero mark or comment-only Moodle feedback) win."""
    own_feedback = (submission.external_receipt or {}).get("moodle_review") or {}
    if own_feedback.get("grade") is not None or str(own_feedback.get("comment") or "").strip():
        return True
    candidates = [submission]
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    identity = (
        await _feedback_question_identity(db, submission, attempt, assessment)
        if attempt is not None and assessment is not None and attempt.state != "VOID"
        else None
    )
    if identity is not None:
        parent_id, remote, slot = identity
        # Native Quiz answers and their Moodle read-backs have different local
        # IDs. A reviewed read-back must also prevent backfilling AI on its twin.
        rows = (await db.execute(
            select(Submission, Attempt, Assessment)
            .join(Attempt, Attempt.id == Submission.attempt_id)
            .join(Assessment, Assessment.id == Attempt.assessment_id)
            .where(
                Submission.id != submission.id,
                Submission.source == "MOODLE_IMPORT",
                Attempt.state != "VOID",
                Attempt.principal_id == attempt.principal_id,
                Assessment.course_id == assessment.course_id,
                or_(
                    Assessment.id == parent_id,
                    Assessment.policy["moodle_parent_assessment_id"].as_string() == str(parent_id),
                ),
                Submission.external_receipt["moodle_response_id"].as_string() == str(slot),
            )
        )).all()
        for imported, imported_attempt, imported_assessment in rows:
            if _feedback_remote_attempt_id(
                (imported.external_receipt or {}).get("moodle_parent_attempt_id")
            ) != remote:
                continue
            if await _feedback_question_identity(
                db, imported, imported_attempt, imported_assessment,
            ) == identity:
                candidates.append(imported)
    for candidate in candidates:
        feedback = (candidate.external_receipt or {}).get("moodle_review") or {}
        if feedback.get("grade") is not None or str(feedback.get("comment") or "").strip():
            return True
    return await db.scalar(select(ReviewDecision.id).where(
        ReviewDecision.submission_id.in_([candidate.id for candidate in candidates]),
        ReviewDecision.status == "APPLIED",
    ).limit(1)) is not None
