"""Discard AI drafts on exit, retaining automatic reports for later review."""

from __future__ import annotations

import uuid
from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.integrations.errors import IntegrationProtocolError
from app.integrations.moodle_browser import normalize_history_attempt_refs
from app.models.analysis import AICodeReview
from app.models.attempts import Attempt, Submission
from app.models.courses import Course
from app.models.identity import ExternalPrincipal
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ReviewDraft
from app.models.tasks import Assessment
from app.services.ai_review import ai_review_policy_assessment
from app.services.common import DomainError
from app.services.policy import require_submission_review_access


async def _queue_refresh(
    db: AsyncSession, report: AICodeReview, *, actor_id: uuid.UUID,
    allow_system_settings_read: bool,
) -> None:
    submission = await db.get(Submission, report.submission_id)
    if submission is None or submission.snapshot_id != report.snapshot_id:
        # A later snapshot has its own imported decision and lifecycle.
        return
    await require_submission_review_access(
        db, principal_id=actor_id, submission_id=submission.id,
        allow_system_settings_read=allow_system_settings_read,
    )
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id)
    parent = await ai_review_policy_assessment(db, assessment, attempt)
    course = await db.get(Course, parent.course_id)
    actor = await db.get(ExternalPrincipal, actor_id)
    student = await db.get(ExternalPrincipal, attempt.principal_id)
    if (
        course is None or actor is None or student is None or not actor.active
        or actor.connection_id != course.connection_id
        or student.connection_id != course.connection_id
    ):
        raise DomainError(409, "AI_REFRESH_UNAVAILABLE", "Не удалось подтвердить доступ к Moodle.")
    mapping = await db.scalar(select(ExternalMapping).where(
        ExternalMapping.connection_id == course.connection_id,
        ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
        ExternalMapping.local_id == parent.id,
        ExternalMapping.external_type.in_(["mod_quiz", "mod_assign"]),
    ))
    if mapping is None:
        raise DomainError(
            409, "AI_REFRESH_UNAVAILABLE", "У работы нет подтверждённой связи с заданием Moodle.",
        )
    metadata = mapping.metadata_json or {}
    module = str(metadata.get("module", mapping.external_type)).removeprefix("mod_")
    cmid = str(metadata.get("cmid", mapping.external_id))
    if (
        module not in {"quiz", "assign"} or metadata.get("sync_state") == "MISSING_IN_MOODLE"
        or not cmid.isdigit() or int(cmid) <= 0
    ):
        raise DomainError(
            409, "AI_REFRESH_UNAVAILABLE", "Задание Moodle недоступно для обновления.",
        )
    # Exact remote identity only. Never guess the latest attempt or fetch another
    # student's submission, and never expand this into a course-wide automatic scan.
    remote_attempt = str((submission.external_receipt or {}).get("moodle_parent_attempt_id", ""))
    submitted_at = submission.submitted_at
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=UTC)
    refs = normalize_history_attempt_refs([{
        "attempt_id": remote_attempt,
        "user_id": student.external_subject,
        "display_name": student.display_name,
        "state": "SUBMITTED",
        "submitted_at_epoch": int(submitted_at.timestamp()),
    }], module, maximum=1)
    active = list((await db.scalars(select(SyncOutbox).where(
        SyncOutbox.course_id == course.id,
        SyncOutbox.aggregate_id == parent.id,
        SyncOutbox.event_type == "moodle.history.import",
        SyncOutbox.state.in_(["PENDING", "PROCESSING", "RETRY"]),
        SyncOutbox.payload["actor_external_subject"].as_string() == actor.external_subject,
    ))).all())
    for event in active:
        existing_refs = (event.payload or {}).get("attempt_refs", [])
        if any(
            ref.get("attempt_id") == remote_attempt
            and ref.get("user_id") == student.external_subject
            for ref in existing_refs if isinstance(ref, dict)
        ):
            report.moodle_refresh_outbox_id = event.id
            return
    event = SyncOutbox(
        connection_id=course.connection_id, course_id=course.id,
        event_type="moodle.history.import", aggregate_type="Assessment", aggregate_id=parent.id,
        idempotency_key=f"ai-review-exit:{report.id.hex}:{report.run_token}",
        payload={
            "course_id": course.external_id, "actor_external_subject": actor.external_subject,
            "module": module, "cmid": int(cmid), "cursor": "0:0", "limit": 1,
            "priority_only": False, "attempt_refs": refs, "detail_key": remote_attempt,
            "ai_review_discard_id": str(report.id),
        },
    )
    db.add(event)
    await db.flush()
    report.moodle_refresh_outbox_id = event.id


async def discard_ai_recommendation(
    db: AsyncSession, *, submission_id: uuid.UUID, report_id: uuid.UUID,
    actor_id: uuid.UUID, allow_system_settings_read: bool = False,
) -> AICodeReview:
    """Exit without approval; retain automatic analysis and never call the model."""
    await require_submission_review_access(
        db, principal_id=actor_id, submission_id=submission_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    # Same lock order as creation/approval. Concurrent approval must either win
    # intact, or reject a report already dismissed by another open tab.
    submission = await db.scalar(select(Submission).where(
        Submission.id == submission_id,
    ).with_for_update().execution_options(populate_existing=True))
    report = await db.scalar(select(AICodeReview).where(
        AICodeReview.id == report_id,
        AICodeReview.submission_id == submission.id,
        AICodeReview.snapshot_id == submission.snapshot_id,
    ).with_for_update().execution_options(populate_existing=True))
    if report is None:
        raise DomainError(
            409, "AI_RECOMMENDATION_CHANGED", "ИИ-проверка изменилась. Обновите страницу.",
        )
    if report.approved_decision_id is not None or report.discarded_at is not None:
        return report
    draft = await db.scalar(select(ReviewDraft).where(
        ReviewDraft.submission_id == submission.id,
        ReviewDraft.owner_id == actor_id,
        ReviewDraft.ai_review_id == report.id,
    ).with_for_update())
    if draft is not None:
        # Discard only this teacher's AI-derived draft, never an unrelated
        # handwritten draft or an approved Moodle/teacher decision.
        await db.delete(draft)
    if not report.manual_requested:
        # An automatic analysis belongs to the submitted snapshot, not to a
        # teacher's viewing session. Keep its result/retry budget/run token and
        # let queued/running work finish even when the viewer leaves. Only the
        # exiting teacher's AI-derived draft is abandoned; the official decision
        # was never changed, so no Moodle refresh or model call is necessary.
        return report
    report.discarded_at = utcnow()
    if report.state == "QUEUED":
        # A job that has not started must not spend a model request after exit.
        report.state = "SKIPPED"
    try:
        await _queue_refresh(
            db, report, actor_id=actor_id,
            allow_system_settings_read=allow_system_settings_read,
        )
    except DomainError as exc:
        report.moodle_refresh_error = (
            str(exc.message) if exc.code == "AI_REFRESH_UNAVAILABLE"
            else "Сдача недоступна для обновления из Moodle."
        )
    except IntegrationProtocolError:
        report.moodle_refresh_error = (
            "Нет точной связи с попыткой Moodle. Синхронизируйте работу вручную."
        )
    return report
