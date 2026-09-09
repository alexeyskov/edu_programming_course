from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.analysis import (
    AuthorshipAnalysisJob,
    AuthorshipAnalysisResult,
    PlagiarismCase,
    SimilarityAnalysis,
    SimilarityMatch,
)
from app.models.attempts import (
    Attempt,
    EditEvent,
    RunRequest,
    RunResult,
    Snapshot,
    Submission,
    Workspace,
)
from app.models.courses import Course
from app.models.enums import ReviewClaimState, RunOrigin, SyncOutboxState
from app.models.evidence import EvidenceReport
from app.models.integration import SyncOutbox
from app.models.review import (
    ReviewClaim,
    ReviewDecision,
    ReviewDraft,
    TeacherExperiment,
    TeacherExperimentFile,
)
from app.models.tasks import Assessment, TaskVersion
from app.services.common import DomainError, sha256_text
from app.services.moodle_attempt_selection import is_latest_completed_moodle_attempt
from app.services.policy import require_review_required, require_submission_review_access


def _as_utc(value: datetime) -> datetime:
    """SQLite drops timezone metadata even for timezone-aware columns."""

    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _submission_context(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    lock: bool,
    allow_system_settings_read: bool = False,
) -> tuple[Submission, Attempt, Assessment]:
    statement = select(Submission).where(Submission.id == submission_id)
    if lock:
        statement = statement.with_for_update()
    submission = await db.scalar(statement)
    if submission is None:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
    attempt = await db.get(Attempt, submission.attempt_id)
    if attempt is None:
        raise DomainError(500, "ATTEMPT_MISSING", "Submission attempt is missing")
    assessment = await db.get(Assessment, attempt.assessment_id)
    if assessment is None:
        raise DomainError(500, "ASSESSMENT_MISSING", "Submission assessment is missing")
    access = await require_submission_review_access(
        db,
        principal_id=teacher_id,
        submission_id=submission_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    return submission, access.attempt, access.assessment


async def _locked_submission_context(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> tuple[Submission, Attempt, Assessment]:
    return await _submission_context(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
        lock=True,
        allow_system_settings_read=allow_system_settings_read,
    )


async def _active_claim(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
) -> ReviewClaim | None:
    now = utcnow()
    claim = await db.scalar(
        select(ReviewClaim)
        .where(
            ReviewClaim.submission_id == submission_id,
            ReviewClaim.state == ReviewClaimState.ACTIVE.value,
        )
        .order_by(ReviewClaim.created_at.desc())
        .with_for_update()
    )
    if claim is not None and _as_utc(claim.lease_expires_at) <= now:
        claim.state = ReviewClaimState.EXPIRED.value
        return None
    return claim


async def claim_submission(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    lease_seconds: int = 300,
    allow_system_settings_read: bool = False,
) -> ReviewClaim:
    submission, attempt, assessment = await _locked_submission_context(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    if not allow_system_settings_read:
        require_review_required(assessment)
    if not await is_latest_completed_moodle_attempt(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    ):
        raise DomainError(
            409,
            "MOODLE_ATTEMPT_SUPERSEDED",
            "This Moodle attempt has been superseded by a newer attempt",
        )
    claim = await _active_claim(db, submission_id=submission_id)
    now = utcnow()
    if claim is not None:
        if claim.owner_id != teacher_id:
            raise DomainError(
                409,
                "SUBMISSION_ALREADY_CLAIMED",
                "Another teacher is reviewing this submission",
                {"lease_expires_at": claim.lease_expires_at.isoformat()},
            )
        claim.heartbeat_at = now
        claim.lease_expires_at = now + timedelta(seconds=lease_seconds)
        await db.flush()
        return claim
    claim = ReviewClaim(
        submission_id=submission_id,
        owner_id=teacher_id,
        lease_expires_at=now + timedelta(seconds=lease_seconds),
        heartbeat_at=now,
    )
    db.add(claim)
    await db.flush()
    return claim


async def heartbeat_claim(
    db: AsyncSession,
    *,
    claim_id: uuid.UUID,
    teacher_id: uuid.UUID,
    lease_seconds: int = 300,
    allow_system_settings_read: bool = False,
) -> ReviewClaim:
    claim_hint = await db.get(ReviewClaim, claim_id)
    if claim_hint is None:
        raise DomainError(404, "CLAIM_NOT_FOUND", "Review claim was not found")
    await _locked_submission_context(
        db,
        submission_id=claim_hint.submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    claim = await db.scalar(select(ReviewClaim).where(ReviewClaim.id == claim_id).with_for_update())
    now = utcnow()
    if (
        claim is None
        or claim.owner_id != teacher_id
        or claim.state != ReviewClaimState.ACTIVE.value
        or _as_utc(claim.lease_expires_at) <= now
    ):
        raise DomainError(409, "CLAIM_NOT_ACTIVE", "Review claim is no longer active")
    claim.heartbeat_at = now
    claim.lease_expires_at = now + timedelta(seconds=lease_seconds)
    await db.flush()
    return claim


async def release_claim(
    db: AsyncSession,
    *,
    claim_id: uuid.UUID,
    teacher_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> None:
    hint = await db.get(ReviewClaim, claim_id)
    if hint is None:
        return
    await _locked_submission_context(
        db,
        submission_id=hint.submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    claim = await db.scalar(select(ReviewClaim).where(ReviewClaim.id == claim_id).with_for_update())
    if claim is None:
        return
    if claim.owner_id != teacher_id:
        raise DomainError(403, "CLAIM_NOT_OWNED", "Review claim belongs to another teacher")
    if claim.state == ReviewClaimState.ACTIVE.value:
        claim.state = ReviewClaimState.RELEASED.value
    await db.flush()


async def require_owned_active_claim(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
) -> ReviewClaim:
    claim = await _active_claim(db, submission_id=submission_id)
    if claim is None or claim.owner_id != teacher_id:
        raise DomainError(409, "ACTIVE_REVIEW_CLAIM_REQUIRED", "An active review claim is required")
    return claim


async def require_teacher_experiment_access(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
) -> None:
    """Keep ungraded work claimed, but allow private runs of decided work.

    Callers must resolve the submission through ``_submission_context`` first;
    that is the server-side course/student visibility check.  A teacher
    experiment is private and cannot change an applied decision, so reopening
    it after grading must not require starting an official re-check.
    """

    applied_decision_id = await db.scalar(
        select(ReviewDecision.id).where(
            ReviewDecision.submission_id == submission_id,
            ReviewDecision.status == "APPLIED",
        )
    )
    if applied_decision_id is not None:
        return
    await require_owned_active_claim(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
    )


async def save_review_draft(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    grade: Decimal | None,
    comment: str,
    criterion_scores: dict | None = None,
    allow_system_settings_read: bool = False,
) -> ReviewDraft:
    _submission, _attempt, assessment = await _locked_submission_context(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    if not allow_system_settings_read:
        require_review_required(assessment)
    await require_owned_active_claim(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
    )
    if grade is not None and (grade < 0 or grade > assessment.max_score):
        raise DomainError(422, "GRADE_OUT_OF_RANGE", "Grade is outside the assessment range")
    draft = await db.scalar(
        select(ReviewDraft).where(ReviewDraft.submission_id == submission_id).with_for_update()
    )
    if draft is None:
        draft = ReviewDraft(
            submission_id=submission_id,
            owner_id=teacher_id,
            grade=grade,
            comment=comment,
            criterion_scores=criterion_scores or {},
        )
        db.add(draft)
    else:
        # A draft is a transient aid, not a second lock. Once the previous claim is
        # released/expired, the current valid claim owner may replace it atomically.
        draft.owner_id = teacher_id
        draft.revision += 1
        draft.grade = grade
        draft.comment = comment
        draft.criterion_scores = criterion_scores or {}
    await db.flush()
    return draft


async def _lock_review_outbox_rows(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
) -> list[SyncOutbox]:
    """Use the same Outbox -> Submission lock order as the sync worker.

    This prevents a newly finalized decision from racing an already dispatched older
    grade. The teacher can retry once that bounded external delivery finishes.
    """

    return list(
        (
            await db.scalars(
                select(SyncOutbox)
                .join(
                    ReviewDecision,
                    ReviewDecision.id == SyncOutbox.aggregate_id,
                )
                .where(
                    SyncOutbox.aggregate_type == "ReviewDecision",
                    ReviewDecision.submission_id == submission_id,
                )
                .order_by(SyncOutbox.created_at, SyncOutbox.id)
                .with_for_update()
            )
        ).all()
    )


async def _validate_evidence_ids(
    db: AsyncSession,
    *,
    evidence_ids: list[uuid.UUID],
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> list[str]:
    """Accept only existing, official evidence bound to this exact submission.

    UUIDs are intentionally opaque in the API, so every supported evidence table is
    checked server-side. Teacher experiments are private hypotheses and are excluded.
    """

    if not evidence_ids:
        return []
    if len(evidence_ids) > 256:
        raise DomainError(422, "TOO_MANY_EVIDENCE_IDS", "At most 256 evidence items are allowed")
    if len(set(evidence_ids)) != len(evidence_ids):
        raise DomainError(422, "DUPLICATE_EVIDENCE_ID", "Evidence ids must be unique")

    requested = set(evidence_ids)
    valid: set[uuid.UUID] = set()
    official_snapshot = await db.get(Snapshot, submission.snapshot_id)
    if official_snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")

    valid.update(
        (
            await db.scalars(
                select(AuthorshipAnalysisJob.id).where(
                    AuthorshipAnalysisJob.id.in_(requested),
                    AuthorshipAnalysisJob.submission_id == submission.id,
                )
            )
        ).all()
    )
    if attempt.assigned_task_version_id is not None:
        assigned_version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        if assigned_version is None:
            raise DomainError(500, "TASK_VERSION_MISSING", "Assigned task version is missing")
        valid.update(
            (
                await db.scalars(
                    select(RunRequest.id)
                    .join(
                        EvidenceReport,
                        EvidenceReport.id == RunRequest.evidence_report_id,
                    )
                    .join(RunResult, RunResult.run_id == RunRequest.id)
                    .where(
                        RunRequest.id.in_(requested),
                        RunRequest.attempt_id == attempt.id,
                        RunRequest.submission_id == submission.id,
                        RunRequest.origin == RunOrigin.IMMUTABLE_SUBMISSION.value,
                        RunRequest.revision == official_snapshot.revision,
                        RunRequest.evidence_case_index.is_not(None),
                        RunRequest.status.in_(["COMPLETED", "FAILED", "CANCELLED"]),
                        EvidenceReport.submission_id == submission.id,
                        EvidenceReport.snapshot_id == official_snapshot.id,
                        EvidenceReport.task_version_id == attempt.assigned_task_version_id,
                        EvidenceReport.task_content_hash == assigned_version.content_hash,
                        EvidenceReport.snapshot_manifest_hash == official_snapshot.manifest_hash,
                        EvidenceReport.status == "COMPLETED",
                        RunRequest.evidence_case_index < EvidenceReport.total_cases,
                    )
                )
            ).all()
        )
    valid.update(
        (
            await db.scalars(
                select(AuthorshipAnalysisResult.id)
                .join(
                    AuthorshipAnalysisJob,
                    AuthorshipAnalysisJob.id == AuthorshipAnalysisResult.job_id,
                )
                .where(
                    AuthorshipAnalysisResult.id.in_(requested),
                    AuthorshipAnalysisJob.submission_id == submission.id,
                )
            )
        ).all()
    )
    valid.update(
        (
            await db.scalars(
                select(SimilarityMatch.id)
                .join(
                    SimilarityAnalysis,
                    SimilarityAnalysis.id == SimilarityMatch.analysis_id,
                )
                .where(
                    SimilarityMatch.id.in_(requested),
                    SimilarityAnalysis.assessment_id == assessment.id,
                    or_(
                        SimilarityMatch.submission_a_id == submission.id,
                        SimilarityMatch.submission_b_id == submission.id,
                    ),
                )
            )
        ).all()
    )
    valid.update(
        (
            await db.scalars(
                select(PlagiarismCase.id)
                .join(SimilarityMatch, SimilarityMatch.id == PlagiarismCase.match_id)
                .join(
                    SimilarityAnalysis,
                    SimilarityAnalysis.id == SimilarityMatch.analysis_id,
                )
                .where(
                    PlagiarismCase.id.in_(requested),
                    SimilarityAnalysis.assessment_id == assessment.id,
                    or_(
                        SimilarityMatch.submission_a_id == submission.id,
                        SimilarityMatch.submission_b_id == submission.id,
                    ),
                )
            )
        ).all()
    )
    valid.update(
        (
            await db.scalars(
                select(RunRequest.id).where(
                    RunRequest.id.in_(requested),
                    RunRequest.attempt_id == attempt.id,
                    RunRequest.requested_by_id == attempt.principal_id,
                    RunRequest.origin == RunOrigin.STUDENT_ATTEMPT.value,
                    RunRequest.revision <= official_snapshot.revision,
                )
            )
        ).all()
    )
    workspace_id = await db.scalar(select(Workspace.id).where(Workspace.attempt_id == attempt.id))
    if workspace_id is not None and official_snapshot.workspace_id == workspace_id:
        valid.update(
            (
                await db.scalars(
                    select(Snapshot.id).where(
                        Snapshot.id.in_(requested),
                        Snapshot.workspace_id == workspace_id,
                        Snapshot.revision <= official_snapshot.revision,
                    )
                )
            ).all()
        )
        valid.update(
            (
                await db.scalars(
                    select(EditEvent.id).where(
                        EditEvent.id.in_(requested),
                        EditEvent.workspace_id == workspace_id,
                        EditEvent.sequence <= official_snapshot.revision,
                    )
                )
            ).all()
        )

    missing = requested - valid
    if missing:
        raise DomainError(
            422,
            "INVALID_REVIEW_EVIDENCE",
            "Evidence does not exist or does not belong to this submission",
            {"invalid_ids": sorted(str(value) for value in missing)},
        )
    return [str(value) for value in evidence_ids]


async def finalize_review(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    grade: Decimal,
    comment: str,
    criterion_scores: dict | None = None,
    evidence_ids: list[uuid.UUID] | None = None,
    idempotency_key: str | None = None,
    allow_system_settings_read: bool = False,
) -> ReviewDecision:
    submission_hint, _attempt_hint, assessment_hint = await _submission_context(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
        lock=False,
        allow_system_settings_read=allow_system_settings_read,
    )
    if not allow_system_settings_read:
        require_review_required(assessment_hint)
    outbox_idempotency_key = ""
    if idempotency_key:
        outbox_idempotency_key = (
            f"review-request:{submission_id}:{sha256_text(idempotency_key)[:24]}"
        )
        prior_event = await db.scalar(
            select(SyncOutbox).where(SyncOutbox.idempotency_key == outbox_idempotency_key)
        )
        if prior_event is not None:
            prior_decision = await db.get(ReviewDecision, prior_event.aggregate_id)
            if prior_decision is None or prior_decision.reviewer_id != teacher_id:
                raise DomainError(409, "IDEMPOTENCY_CONFLICT", "Review request key is in use")
            return prior_decision

    outbox_rows = await _lock_review_outbox_rows(db, submission_id=submission_hint.id)
    if any(row.state == SyncOutboxState.PROCESSING.value for row in outbox_rows):
        raise DomainError(
            409,
            "GRADE_EXPORT_IN_PROGRESS",
            "A previous grade is currently being exported; retry after it finishes",
        )
    submission, attempt, assessment = await _locked_submission_context(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    if not await is_latest_completed_moodle_attempt(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    ):
        raise DomainError(
            409,
            "MOODLE_ATTEMPT_SUPERSEDED",
            "This Moodle attempt has been superseded by a newer attempt",
        )
    claim = await require_owned_active_claim(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
    )
    if grade < 0 or grade > assessment.max_score:
        raise DomainError(422, "GRADE_OUT_OF_RANGE", "Grade is outside the assessment range")
    validated_evidence = await _validate_evidence_ids(
        db,
        evidence_ids=evidence_ids or [],
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )
    latest = await db.scalar(
        select(ReviewDecision)
        .where(ReviewDecision.submission_id == submission_id)
        .order_by(ReviewDecision.revision.desc())
        .with_for_update()
    )
    revision = (latest.revision + 1) if latest else 1
    if latest is not None:
        latest.status = "SUPERSEDED"
        latest.lms_export_state = "SUPERSEDED"
        await db.execute(
            update(SyncOutbox)
            .where(
                SyncOutbox.aggregate_type == "ReviewDecision",
                SyncOutbox.aggregate_id == latest.id,
                SyncOutbox.state.in_(
                    [
                        SyncOutboxState.PENDING.value,
                        SyncOutboxState.RETRY.value,
                        SyncOutboxState.FAILED.value,
                    ]
                ),
            )
            .values(state=SyncOutboxState.BLOCKED.value, last_error="Superseded by newer review")
        )
    decision = ReviewDecision(
        submission_id=submission_id,
        reviewer_id=teacher_id,
        revision=revision,
        grade=grade,
        comment=comment,
        criterion_scores=criterion_scores or {},
        evidence_ids=validated_evidence,
        supersedes_id=latest.id if latest else None,
    )
    db.add(decision)
    await db.flush()
    course = await db.get(Course, assessment.course_id)
    if course is None:
        raise DomainError(500, "COURSE_MISSING", "Submission course is missing")
    event = SyncOutbox(
        connection_id=course.connection_id,
        course_id=assessment.course_id,
        event_type="review.decision",
        aggregate_type="ReviewDecision",
        aggregate_id=decision.id,
        idempotency_key=outbox_idempotency_key or f"review:{submission.id}:{revision}"[:100],
        payload={
            "submission_id": str(submission.id),
            "assessment_id": str(assessment.id),
            "review_revision": revision,
            "grade": str(grade),
            "comment": comment,
        },
    )
    db.add(event)
    submission.lms_export_state = "PENDING"
    # Historical Moodle submissions carry the only local copy of the exact
    # remote attempt/slot identity. Grade delivery validates that receipt
    # against its durable ExternalMapping before touching Moodle. Ordinary
    # locally-authored submissions still discard their previous export receipt
    # while a new grade is pending.
    if submission.source != "MOODLE_IMPORT":
        submission.external_receipt = {}
    claim.state = ReviewClaimState.RELEASED.value
    await db.flush()
    return decision


async def create_teacher_experiment(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    ttl_seconds: int = 86_400,
    allow_system_settings_read: bool = False,
) -> TeacherExperiment:
    submission, _attempt, _assessment = await _locked_submission_context(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    await require_teacher_experiment_access(
        db,
        submission_id=submission_id,
        teacher_id=teacher_id,
    )
    existing = await db.scalar(
        select(TeacherExperiment)
        .where(
            TeacherExperiment.submission_id == submission_id,
            TeacherExperiment.owner_id == teacher_id,
            TeacherExperiment.deleted_at.is_(None),
        )
        .order_by(TeacherExperiment.created_at.desc())
        .with_for_update()
    )
    if existing is not None and _as_utc(existing.expires_at) > utcnow():
        return existing
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")
    experiment = TeacherExperiment(
        submission_id=submission.id,
        owner_id=teacher_id,
        base_snapshot_hash=snapshot.manifest_hash,
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(experiment)
    await db.flush()
    for file in snapshot.files:
        content = str(file.get("content", ""))
        db.add(
            TeacherExperimentFile(
                experiment_id=experiment.id,
                path=str(file.get("path", "main.cpp")),
                content=content,
                content_hash=sha256_text(content),
            )
        )
    await db.flush()
    return experiment


async def update_experiment_file(
    db: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    file_id: uuid.UUID,
    content: str,
    expected_revision: int,
    allow_system_settings_read: bool = False,
) -> tuple[TeacherExperiment, TeacherExperimentFile]:
    experiment_hint = await db.get(TeacherExperiment, experiment_id)
    if experiment_hint is None:
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await _locked_submission_context(
        db,
        submission_id=experiment_hint.submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    experiment = await db.scalar(
        select(TeacherExperiment).where(TeacherExperiment.id == experiment_id).with_for_update()
    )
    if (
        experiment is None
        or experiment.owner_id != teacher_id
        or experiment.deleted_at is not None
        or _as_utc(experiment.expires_at) <= utcnow()
    ):
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await require_teacher_experiment_access(
        db,
        submission_id=experiment.submission_id,
        teacher_id=teacher_id,
    )
    if experiment.revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Experiment revision is stale",
            {"current_revision": experiment.revision},
        )
    file = await db.scalar(
        select(TeacherExperimentFile)
        .where(
            TeacherExperimentFile.id == file_id,
            TeacherExperimentFile.experiment_id == experiment.id,
        )
        .with_for_update()
    )
    if file is None:
        raise DomainError(404, "EXPERIMENT_FILE_NOT_FOUND", "Experiment file was not found")
    file.content = content
    file.content_hash = sha256_text(content)
    experiment.revision += 1
    await db.flush()
    return experiment, file


async def reset_teacher_experiment(
    db: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    expected_revision: int,
    allow_system_settings_read: bool = False,
) -> TeacherExperiment:
    experiment_hint = await db.get(TeacherExperiment, experiment_id)
    if experiment_hint is None:
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await _locked_submission_context(
        db,
        submission_id=experiment_hint.submission_id,
        teacher_id=teacher_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    experiment = await db.scalar(
        select(TeacherExperiment).where(TeacherExperiment.id == experiment_id).with_for_update()
    )
    if (
        experiment is None
        or experiment.owner_id != teacher_id
        or experiment.deleted_at is not None
        or _as_utc(experiment.expires_at) <= utcnow()
    ):
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await require_teacher_experiment_access(
        db,
        submission_id=experiment.submission_id,
        teacher_id=teacher_id,
    )
    if experiment.revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Experiment revision is stale",
            {"current_revision": experiment.revision},
        )
    submission = await db.get(Submission, experiment.submission_id)
    snapshot = await db.get(Snapshot, submission.snapshot_id) if submission else None
    if snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")
    files = list(
        (
            await db.scalars(
                select(TeacherExperimentFile).where(
                    TeacherExperimentFile.experiment_id == experiment.id
                )
            )
        ).all()
    )
    by_path = {row.path: row for row in files}
    for snapshot_file in snapshot.files:
        path = str(snapshot_file.get("path", "main.cpp"))
        content = str(snapshot_file.get("content", ""))
        row = by_path.get(path)
        if row is None:
            db.add(
                TeacherExperimentFile(
                    experiment_id=experiment.id,
                    path=path,
                    content=content,
                    content_hash=sha256_text(content),
                )
            )
        else:
            row.content = content
            row.content_hash = sha256_text(content)
    # Revisions are monotonic so an old If-Match value cannot become valid again
    # after a reset (the classic ABA race).
    experiment.revision += 1
    experiment.reset_at = utcnow()
    await db.flush()
    return experiment
