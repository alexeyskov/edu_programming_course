from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.attempts import (
    _edit_event_payload,
    _effective_flags,
    _enforce_run_budget,
    _expected_revision,
    _interactive_start_admission_lock,
    _mock_result,
    _record_interactive_response,
    _run_read,
    _store_run_result,
)
from app.auth.context import CurrentAuth
from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.errors import IntegrationError
from app.integrations.runner import RunnerAdapter, RunnerResult
from app.models.attempts import Attempt, EditEvent, RunRequest, Snapshot, Submission
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
)
from app.models.enums import AttemptState, CourseRole, ReviewClaimState, RunOrigin, RunStatus
from app.models.evidence import EvidenceReport
from app.models.identity import ExternalPrincipal
from app.models.review import (
    ReviewClaim,
    ReviewDecision,
    ReviewDraft,
    TeacherExperiment,
    TeacherExperimentFile,
)
from app.models.tasks import Assessment, TaskVersion
from app.schemas.attempts import AttemptHistoryEventRead, WorkspaceFileRead
from app.schemas.reviews import (
    ReviewClaimCreateRequest,
    ReviewClaimHeartbeatRequest,
    ReviewClaimRead,
    ReviewDecisionCreateRequest,
    ReviewDecisionRead,
    ReviewDraftRead,
    ReviewDraftUpsertRequest,
    SubmissionDecisionRead,
    SubmissionListItemRead,
    SubmissionReviewGroupItemRead,
    SubmissionReviewGroupRead,
    SubmissionTeacherRead,
    TeacherExperimentCreateRequest,
    TeacherExperimentFilePatchRead,
    TeacherExperimentFilePatchRequest,
    TeacherExperimentFileRead,
    TeacherExperimentRead,
    TeacherExperimentResetRequest,
)
from app.schemas.runs import (
    InteractiveRunCreateRequest,
    InteractiveRunInputRequest,
    InteractiveRunRead,
    RunCreateRequest,
    RunTeacherRead,
)
from app.services.build_profile import effective_attempt_build_profile
from app.services.client_context import normalize_client_context
from app.services.common import DomainError, language_for_path
from app.services.moodle_attempt_selection import (
    is_completed_moodle_submission,
    latest_reviewable_moodle_submission_ids,
)
from app.services.policy import (
    require_submission_review_access,
    submission_review_access,
    visible_submission_ids_for_review,
)
from app.services.review import (
    claim_submission,
    create_teacher_experiment,
    finalize_review,
    heartbeat_claim,
    release_claim,
    require_teacher_experiment_access,
    reset_teacher_experiment,
    save_review_draft,
    update_experiment_file,
)
from app.services.submission_origin import submission_origin_verification

router = APIRouter()
DB = Annotated[AsyncSession, Depends(get_db)]


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=UTC)


async def _submission_context(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    lock: bool = False,
    allow_system_settings_read: bool = False,
) -> tuple[Submission, Attempt, Assessment]:
    access = await require_submission_review_access(
        db,
        principal_id=teacher_id,
        submission_id=submission_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    statement = select(Submission).where(Submission.id == access.submission.id)
    if lock:
        statement = statement.with_for_update()
    submission = await db.scalar(statement)
    if submission is None:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
    attempt = access.attempt
    assessment = access.assessment
    if attempt is None or assessment is None:
        raise DomainError(500, "SUBMISSION_CONTEXT_MISSING", "Submission context is incomplete")
    return submission, attempt, assessment


async def _student_group(db: AsyncSession, *, principal_id: uuid.UUID, course_id: uuid.UUID) -> str:
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.principal_id == principal_id,
            CourseMembership.course_id == course_id,
            CourseMembership.role == CourseRole.STUDENT.value,
            CourseMembership.active.is_(True),
        )
    )
    if membership is None:
        return ""
    names = list(
        (
            await db.scalars(
                select(CourseGroup.name)
                .join(
                    CourseMembershipGroup,
                    CourseMembershipGroup.coursegroup_id == CourseGroup.id,
                )
                .where(
                    CourseMembershipGroup.coursemembership_id == membership.id,
                    CourseGroup.active.is_(True),
                )
                .order_by(CourseGroup.name)
            )
        ).all()
    )
    return ", ".join(names)


async def _current_claim(db: AsyncSession, submission_id: uuid.UUID) -> ReviewClaim | None:
    claim = await db.scalar(
        select(ReviewClaim)
        .where(
            ReviewClaim.submission_id == submission_id,
            ReviewClaim.state == ReviewClaimState.ACTIVE.value,
        )
        .order_by(ReviewClaim.created_at.desc())
    )
    expires_at = _aware(claim.lease_expires_at) if claim else None
    return claim if claim is not None and expires_at and expires_at > utcnow() else None


async def _claim_read(
    db: AsyncSession,
    claim: ReviewClaim,
    *,
    principal_id: uuid.UUID,
) -> ReviewClaimRead:
    owner = await db.get(ExternalPrincipal, claim.owner_id)
    return ReviewClaimRead(
        id=claim.id,
        submission_id=claim.submission_id,
        owner_id=claim.owner_id,
        owner_name=owner.display_name if owner else "Недоступный пользователь",
        lease_expires_at=claim.lease_expires_at,
        heartbeat_at=claim.heartbeat_at,
        state=claim.state,
        mine=claim.owner_id == principal_id,
        takeover_reason=claim.takeover_reason,
    )


async def _latest_decision(db: AsyncSession, submission_id: uuid.UUID) -> ReviewDecision | None:
    return await db.scalar(
        select(ReviewDecision)
        .where(
            ReviewDecision.submission_id == submission_id,
            ReviewDecision.status == "APPLIED",
        )
        .order_by(ReviewDecision.revision.desc())
    )


async def _decision_history(
    db: AsyncSession,
    submission_id: uuid.UUID,
) -> list[SubmissionDecisionRead]:
    decisions = list(
        (
            await db.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.submission_id == submission_id)
                .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
            )
        ).all()
    )
    reviewer_ids = {decision.reviewer_id for decision in decisions}
    reviewers = (
        list(
            (
                await db.scalars(
                    select(ExternalPrincipal).where(ExternalPrincipal.id.in_(reviewer_ids))
                )
            ).all()
        )
        if reviewer_ids
        else []
    )
    reviewer_names = {reviewer.id: reviewer.display_name for reviewer in reviewers}
    return [
        SubmissionDecisionRead(
            id=decision.id,
            submission_id=decision.submission_id,
            reviewer_id=decision.reviewer_id,
            reviewer_name=reviewer_names.get(decision.reviewer_id, "Недоступный пользователь"),
            revision=decision.revision,
            grade=decision.grade,
            comment=decision.comment,
            criterion_scores=decision.criterion_scores,
            evidence_ids=decision.evidence_ids,
            status=decision.status,
            supersedes_id=decision.supersedes_id,
            lms_export_state=decision.lms_export_state,
            created_at=decision.created_at,
            updated_at=decision.updated_at,
        )
        for decision in decisions
    ]


async def _submission_list_item(
    db: AsyncSession,
    *,
    submission: Submission,
    teacher_id: uuid.UUID,
    can_review: bool = False,
    allow_review_without_requirement: bool = False,
) -> SubmissionListItemRead:
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    course = await db.get(Course, assessment.course_id) if assessment else None
    student = await db.get(ExternalPrincipal, attempt.principal_id) if attempt else None
    if attempt is None or assessment is None or course is None or student is None:
        raise DomainError(500, "SUBMISSION_CONTEXT_MISSING", "Submission context is incomplete")
    claim = await _current_claim(db, submission.id)
    decision = await _latest_decision(db, submission.id)
    evidence = await db.scalar(
        select(EvidenceReport)
        .where(
            EvidenceReport.submission_id == submission.id,
            EvidenceReport.status == "COMPLETED",
        )
        .order_by(EvidenceReport.created_at.desc(), EvidenceReport.id.desc())
    )
    review_status = "CLAIMED" if claim else "GRADED" if decision else "UNGRADED"
    return SubmissionListItemRead(
        id=submission.id,
        assessment_id=assessment.id,
        course_id=assessment.course_id,
        course_title=course.title,
        assessment_title=assessment.title,
        student_name=student.display_name,
        student_group=await _student_group(
            db,
            principal_id=student.id,
            course_id=assessment.course_id,
        ),
        submitted_at=submission.submitted_at,
        status=review_status,
        score=decision.grade if decision else None,
        max_score=assessment.max_score,
        claim=await _claim_read(db, claim, principal_id=teacher_id) if claim else None,
        risk="UNKNOWN",
        tests_passed=evidence.passed_cases if evidence else 0,
        tests_total=evidence.total_cases if evidence else 0,
        review_required=assessment.review_required,
        decision_support_enabled=assessment.decision_support_enabled,
        can_review=can_review and (assessment.review_required or allow_review_without_requirement),
    )


async def _submission_list_items(
    db: AsyncSession,
    *,
    rows: list[tuple[Submission, Attempt, Assessment, ExternalPrincipal]],
    teacher_id: uuid.UUID,
    mutable_submission_ids: set[uuid.UUID],
    allow_review_without_requirement: bool = False,
) -> list[SubmissionListItemRead]:
    """Render a bounded queue page with a fixed number of aggregate queries."""

    if not rows:
        return []
    now = utcnow()
    submission_ids = [submission.id for submission, *_rest in rows]
    claims = list(
        (
            await db.scalars(
                select(ReviewClaim)
                .where(
                    ReviewClaim.submission_id.in_(submission_ids),
                    ReviewClaim.state == ReviewClaimState.ACTIVE.value,
                )
                .order_by(ReviewClaim.created_at.desc())
            )
        ).all()
    )
    claims_by_submission: dict[uuid.UUID, ReviewClaim] = {}
    for claim in claims:
        expires_at = _aware(claim.lease_expires_at)
        if expires_at is not None and expires_at > now:
            claims_by_submission.setdefault(claim.submission_id, claim)

    decisions = list(
        (
            await db.scalars(
                select(ReviewDecision)
                .where(
                    ReviewDecision.submission_id.in_(submission_ids),
                    ReviewDecision.status == "APPLIED",
                )
                .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
            )
        ).all()
    )
    decisions_by_submission: dict[uuid.UUID, ReviewDecision] = {}
    for decision in decisions:
        decisions_by_submission.setdefault(decision.submission_id, decision)

    evidence_reports = list(
        (
            await db.scalars(
                select(EvidenceReport)
                .where(
                    EvidenceReport.submission_id.in_(submission_ids),
                    EvidenceReport.status == "COMPLETED",
                )
                .order_by(EvidenceReport.created_at.desc(), EvidenceReport.id.desc())
            )
        ).all()
    )
    evidence_by_submission: dict[uuid.UUID, EvidenceReport] = {}
    for report in evidence_reports:
        evidence_by_submission.setdefault(report.submission_id, report)

    owner_ids = {claim.owner_id for claim in claims_by_submission.values()}
    owners = (
        list(
            (
                await db.scalars(
                    select(ExternalPrincipal).where(ExternalPrincipal.id.in_(owner_ids))
                )
            ).all()
        )
        if owner_ids
        else []
    )
    owner_names = {owner.id: owner.display_name for owner in owners}

    principal_ids = {student.id for *_context, student in rows}
    course_ids = {assessment.course_id for _submission, _attempt, assessment, _student in rows}
    courses = list((await db.scalars(select(Course).where(Course.id.in_(course_ids)))).all())
    course_titles = {course.id: course.title for course in courses}
    group_rows = (
        await db.execute(
            select(
                CourseMembership.principal_id,
                CourseMembership.course_id,
                CourseGroup.name,
            )
            .join(
                CourseMembershipGroup,
                CourseMembershipGroup.coursemembership_id == CourseMembership.id,
            )
            .join(CourseGroup, CourseGroup.id == CourseMembershipGroup.coursegroup_id)
            .where(
                CourseMembership.principal_id.in_(principal_ids),
                CourseMembership.course_id.in_(course_ids),
                CourseMembership.role == CourseRole.STUDENT.value,
                CourseMembership.active.is_(True),
                or_(
                    CourseMembership.valid_until.is_(None),
                    CourseMembership.valid_until > now,
                ),
                CourseGroup.active.is_(True),
            )
            .order_by(CourseGroup.name)
        )
    ).all()
    group_names: dict[tuple[uuid.UUID, uuid.UUID], list[str]] = {}
    for principal_id, course_id, name in group_rows:
        group_names.setdefault((principal_id, course_id), []).append(name)

    result: list[SubmissionListItemRead] = []
    for submission, _attempt, assessment, student in rows:
        claim = claims_by_submission.get(submission.id)
        decision = decisions_by_submission.get(submission.id)
        evidence = evidence_by_submission.get(submission.id)
        review_status = "CLAIMED" if claim else "GRADED" if decision else "UNGRADED"
        claim_read = None
        if claim is not None:
            claim_read = ReviewClaimRead(
                id=claim.id,
                submission_id=claim.submission_id,
                owner_id=claim.owner_id,
                owner_name=owner_names.get(claim.owner_id, "Недоступный пользователь"),
                lease_expires_at=claim.lease_expires_at,
                heartbeat_at=claim.heartbeat_at,
                state=claim.state,
                mine=claim.owner_id == teacher_id,
                takeover_reason=claim.takeover_reason,
            )
        result.append(
            SubmissionListItemRead(
                id=submission.id,
                assessment_id=assessment.id,
                course_id=assessment.course_id,
                course_title=course_titles.get(assessment.course_id, "Недоступный курс"),
                assessment_title=assessment.title,
                student_name=student.display_name,
                student_group=", ".join(group_names.get((student.id, assessment.course_id), [])),
                submitted_at=submission.submitted_at,
                status=review_status,
                score=decision.grade if decision else None,
                max_score=assessment.max_score,
                claim=claim_read,
                risk="UNKNOWN",
                tests_passed=evidence.passed_cases if evidence else 0,
                tests_total=evidence.total_cases if evidence else 0,
                review_required=assessment.review_required,
                decision_support_enabled=assessment.decision_support_enabled,
                can_review=(
                    submission.id in mutable_submission_ids
                    and (assessment.review_required or allow_review_without_requirement)
                ),
            )
        )
    return result


ReviewQueueGroupKey = tuple[uuid.UUID, str, uuid.UUID, uuid.UUID]


def _review_queue_group_key(
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> ReviewQueueGroupKey | None:
    """Identify one exact student's response to one Moodle Quiz attempt.

    Child Essay submissions are deliberately separate domain submissions.  A
    queue row, however, represents the surrounding Quiz attempt, just like the
    old ``work_uuid`` grouping did.  All identity components are mandatory so
    similarly numbered attempts, other quizzes and other students cannot mix.
    """

    if submission.source != "MOODLE_IMPORT" or attempt.state == AttemptState.VOID.value:
        return None
    receipt = dict(submission.external_receipt or {})
    parent_attempt_id = str(receipt.get("moodle_parent_attempt_id", "")).strip()
    raw_parent_id = dict(assessment.policy or {}).get("moodle_parent_assessment_id")
    try:
        parent_assessment_id = uuid.UUID(str(raw_parent_id))
    except (TypeError, ValueError, AttributeError):
        return None
    if not parent_attempt_id:
        return None
    return (
        parent_assessment_id,
        parent_attempt_id,
        attempt.principal_id,
        assessment.course_id,
    )


def _review_queue_group_position(submission: Submission) -> int | None:
    try:
        position = int(dict(submission.external_receipt or {}).get("moodle_response_position"))
    except (TypeError, ValueError, OverflowError):
        return None
    return position if 1 <= position <= 1_000 else None


def _quiz_response_duplicate_rank(
    submission: Submission,
    *,
    has_decision: bool = False,
    has_active_claim: bool = False,
) -> tuple[int, int, int, int, int, datetime, str]:
    """Prefer the current canonical import when old response ids collide.

    Early connector versions could identify the same Moodle question as
    ``position-1`` and later as its stable Quiz slot.  Both rows then have the
    same visual position.  Authoritative synchronization retires the old row;
    this rank also keeps the UI deterministic while that refresh is pending.
    """

    receipt = dict(submission.external_receipt or {})
    raw_version = receipt.get("historical_source_materialization_version")
    version = (
        int(raw_version)
        if isinstance(raw_version, int) and not isinstance(raw_version, bool)
        else 0
    )
    response_id = str(receipt.get("moodle_response_id", "")).strip().casefold()
    stable_identity = int(
        bool(response_id)
        and not response_id.startswith("position-")
        and response_id != "moodle-detail"
    )
    return (
        version,
        stable_identity,
        int(receipt.get("source_complete") is True),
        int(has_decision),
        int(has_active_claim),
        submission.created_at,
        str(submission.id),
    )


async def _review_queue_row_units(
    db: AsyncSession,
    rows: list[tuple[Submission, Attempt, Assessment, ExternalPrincipal]],
) -> list[list[tuple[Submission, Attempt, Assessment, ExternalPrincipal]]]:
    """Build ordered queue units without loading claims, decisions or evidence.

    The full lightweight identity projection is needed to paginate groups
    correctly, but expensive queue annotations only need to be materialized for
    units on the requested page.  This matters for historical courses with
    thousands of Moodle submissions on small deployment machines.
    """

    raw_keys = [
        _review_queue_group_key(submission, attempt, assessment)
        for submission, attempt, assessment, _student in rows
    ]
    parent_ids = {key[0] for key in raw_keys if key is not None}
    parents = (
        list((await db.scalars(select(Assessment).where(Assessment.id.in_(parent_ids)))).all())
        if parent_ids
        else []
    )
    parents_by_id = {parent.id: parent for parent in parents}
    keyed_indexes: dict[ReviewQueueGroupKey, list[int]] = {}
    normalized_keys: list[ReviewQueueGroupKey | None] = []
    for index, ((submission, _attempt, assessment, _student), key) in enumerate(
        zip(rows, raw_keys, strict=True)
    ):
        parent = parents_by_id.get(key[0]) if key is not None else None
        if (
            key is None
            or _review_queue_group_position(submission) is None
            or parent is None
            or parent.course_id != assessment.course_id
        ):
            normalized_keys.append(None)
            continue
        normalized_keys.append(key)
        keyed_indexes.setdefault(key, []).append(index)

    valid_keys = {
        key
        for key, indexes in keyed_indexes.items()
        if len({_review_queue_group_position(rows[index][0]) for index in indexes}) >= 2
    }
    emitted: set[ReviewQueueGroupKey] = set()
    units: list[list[tuple[Submission, Attempt, Assessment, ExternalPrincipal]]] = []
    for index, row in enumerate(rows):
        key = normalized_keys[index]
        if key is None or key not in valid_keys:
            units.append([row])
            continue
        if key in emitted:
            continue
        emitted.add(key)
        units.append([rows[member_index] for member_index in keyed_indexes[key]])
    return units


async def _collapse_submission_review_groups(
    db: AsyncSession,
    *,
    rows: list[tuple[Submission, Attempt, Assessment, ExternalPrincipal]],
    items: list[SubmissionListItemRead],
) -> list[SubmissionListItemRead]:
    """Collapse Quiz Essay children into queue units before API pagination."""

    if len(rows) != len(items):
        raise DomainError(500, "SUBMISSION_QUEUE_INVALID", "Submission queue is inconsistent")

    parent_ids = {
        key[0]
        for submission, attempt, assessment, _student in rows
        if (key := _review_queue_group_key(submission, attempt, assessment)) is not None
    }
    parents = (
        list((await db.scalars(select(Assessment).where(Assessment.id.in_(parent_ids)))).all())
        if parent_ids
        else []
    )
    parents_by_id = {parent.id: parent for parent in parents}

    grouped_rows: dict[
        ReviewQueueGroupKey,
        list[tuple[int, Submission, SubmissionListItemRead]],
    ] = {}
    row_keys: list[ReviewQueueGroupKey | None] = []
    for (submission, attempt, assessment, _student), item in zip(rows, items, strict=True):
        key = _review_queue_group_key(submission, attempt, assessment)
        position = _review_queue_group_position(submission)
        parent = parents_by_id.get(key[0]) if key is not None else None
        if (
            key is None
            or position is None
            or parent is None
            or parent.course_id != assessment.course_id
        ):
            row_keys.append(None)
            continue
        row_keys.append(key)
        grouped_rows.setdefault(key, []).append((position, submission, item))

    for key, members in list(grouped_rows.items()):
        by_position: dict[int, tuple[int, Submission, SubmissionListItemRead]] = {}
        for member in members:
            position, submission, item = member
            current = by_position.get(position)
            if current is None or _quiz_response_duplicate_rank(
                submission,
                has_decision=item.status == "GRADED",
                has_active_claim=item.status == "CLAIMED",
            ) > _quiz_response_duplicate_rank(
                current[1],
                has_decision=current[2].status == "GRADED",
                has_active_claim=current[2].status == "CLAIMED",
            ):
                by_position[position] = member
        grouped_rows[key] = list(by_position.values())

    # A single Essay question behaves exactly like a normal standalone
    # submission.  Only genuine multi-question attempts become queue groups.
    valid_group_keys = {key for key, members in grouped_rows.items() if len(members) >= 2}
    collapsed: list[SubmissionListItemRead] = []
    emitted: set[ReviewQueueGroupKey] = set()
    for row_index, ((_submission, _attempt, _assessment, _student), item) in enumerate(
        zip(rows, items, strict=True)
    ):
        key = row_keys[row_index]
        if key is None or key not in valid_group_keys:
            collapsed.append(item)
            continue
        if key in emitted:
            continue
        emitted.add(key)
        members = sorted(
            grouped_rows[key],
            key=lambda member: (member[0], str(member[1].id)),
        )
        member_items = [member[2] for member in members]
        relevant = [member for member in member_items if member.review_required]
        conflicts = [member for member in relevant if member.status == "CONFLICT"]
        pending = [member for member in relevant if member.status in {"UNGRADED", "CLAIMED"}]
        mine_claimed = [
            member
            for member in pending
            if member.status == "CLAIMED" and member.claim is not None and member.claim.mine
        ]
        ungraded_reviewable = [
            member for member in pending if member.status == "UNGRADED" and member.can_review
        ]
        claimed = [member for member in pending if member.status == "CLAIMED"]
        if conflicts:
            representative = conflicts[0]
            aggregate_status = "CONFLICT"
        elif pending:
            # Resume our own reservation first.  Otherwise, do not strand a
            # free question behind a sibling currently claimed by somebody
            # else: opening the group must land on work this teacher can take.
            representative = (mine_claimed or ungraded_reviewable or claimed or pending)[0]
            aggregate_status = representative.status
        else:
            reviewable = [member for member in member_items if member.can_review]
            representative = (reviewable or member_items)[0]
            aggregate_status = (
                "GRADED"
                if relevant and all(member.status == "GRADED" for member in relevant)
                else representative.status
            )

        parent = parents_by_id[key[0]]
        scores = [member.score for member in member_items if member.score is not None]
        risk_order = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}
        aggregate_risk = max(
            (member.risk for member in member_items),
            key=lambda risk: risk_order[risk],
        )
        review_group = SubmissionReviewGroupRead(
            id=uuid.uuid5(key[0], f"{key[2]}:{key[1]}"),
            title=parent.title,
            items=[
                SubmissionReviewGroupItemRead(
                    submission_id=member.id,
                    position=position,
                    title=member.assessment_title,
                    score=member.score,
                    max_score=member.max_score,
                    status=member.status,
                )
                for position, _submission, member in members
            ],
        )
        collapsed.append(
            representative.model_copy(
                update={
                    "assessment_title": parent.title,
                    "submitted_at": max(member.submitted_at for member in member_items),
                    "status": aggregate_status,
                    "score": sum(scores, Decimal("0")) if scores else None,
                    "max_score": sum(
                        (member.max_score for member in member_items),
                        Decimal("0"),
                    ),
                    "risk": aggregate_risk,
                    "tests_passed": sum(member.tests_passed for member in member_items),
                    "tests_total": sum(member.tests_total for member in member_items),
                    "review_required": any(member.review_required for member in member_items),
                    "decision_support_enabled": any(
                        member.decision_support_enabled for member in member_items
                    ),
                    "can_review": any(member.can_review for member in member_items),
                    "review_group": review_group,
                }
            )
        )
    return collapsed


@router.get(
    "/assessments/{assessment_ref}/submissions",
    response_model=list[SubmissionListItemRead],
    tags=["reviews"],
)
async def list_submissions(
    assessment_ref: str,
    auth: CurrentAuth,
    db: DB,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[SubmissionListItemRead]:
    statement = (
        select(Submission, Attempt, Assessment, ExternalPrincipal)
        .join(Attempt, Attempt.id == Submission.attempt_id)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .join(ExternalPrincipal, ExternalPrincipal.id == Attempt.principal_id)
    )
    assessment_id: uuid.UUID | None = None
    if assessment_ref != "all":
        try:
            assessment_id = uuid.UUID(assessment_ref)
        except ValueError as exc:
            raise DomainError(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found") from exc
        assessment = await db.get(Assessment, assessment_id)
        if assessment is None:
            raise DomainError(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found")
        statement = statement.where(Attempt.assessment_id == assessment.id)
    visible_ids = await visible_submission_ids_for_review(
        db,
        principal_id=auth.principal_id,
        assessment_id=assessment_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    if not visible_ids:
        return []
    system_access = auth.has_capability("SYSTEM_SETTINGS")
    mutable_submission_ids = (
        visible_ids
        if system_access
        else await visible_submission_ids_for_review(
            db,
            principal_id=auth.principal_id,
            assessment_id=assessment_id,
        )
    )
    statement = statement.where(Submission.id.in_(visible_ids))
    rows = list(
        (await db.execute(statement.order_by(Submission.submitted_at.desc(), Submission.id))).all()
    )
    latest_moodle_ids = await latest_reviewable_moodle_submission_ids(
        db,
        ((submission, attempt, assessment) for submission, attempt, assessment, _student in rows),
    )
    rows = [
        row
        for row in rows
        if not is_completed_moodle_submission(row[0], row[1], row[2])
        or row[0].id in latest_moodle_ids
    ]
    queue_units = await _review_queue_row_units(db, rows)
    page_units = queue_units[offset : offset + limit]
    page_rows = [row for unit in page_units for row in unit]
    items = await _submission_list_items(
        db,
        rows=page_rows,
        teacher_id=auth.principal_id,
        mutable_submission_ids=mutable_submission_ids,
        allow_review_without_requirement=system_access,
    )
    # Offset and limit are queue-unit coordinates.  Applying SQL pagination
    # before this collapse would split one Quiz attempt across pages and make
    # counters depend on page boundaries.
    return await _collapse_submission_review_groups(db, rows=page_rows, items=items)


def _snapshot_file(snapshot: Snapshot, item: dict[str, Any]) -> WorkspaceFileRead:
    raw_id = item.get("id")
    try:
        file_id = uuid.UUID(str(raw_id))
    except (TypeError, ValueError):
        file_id = uuid.uuid5(snapshot.id, str(item.get("path", "main.cpp")))
    return WorkspaceFileRead(
        id=file_id,
        path=str(item.get("path", "main.cpp")),
        language=str(item.get("language", language_for_path(str(item.get("path", "main.cpp"))))),
        content=str(item.get("content", "")),
        read_only=True,
        created_revision=0,
    )


async def _submission_history(
    db: AsyncSession,
    *,
    submission: Submission,
    attempt: Attempt,
    snapshot: Snapshot,
) -> list[AttemptHistoryEventRead]:
    edit_events = list(
        (
            await db.scalars(
                select(EditEvent)
                .where(
                    EditEvent.workspace_id == snapshot.workspace_id,
                    EditEvent.sequence <= snapshot.revision,
                )
                .order_by(EditEvent.sequence)
            )
        ).all()
    )
    snapshots = list(
        (
            await db.scalars(
                select(Snapshot)
                .where(
                    Snapshot.workspace_id == snapshot.workspace_id,
                    Snapshot.revision <= snapshot.revision,
                )
                .order_by(Snapshot.created_at)
            )
        ).all()
    )
    runs = list(
        (
            await db.scalars(
                select(RunRequest)
                .where(
                    RunRequest.attempt_id == attempt.id,
                    RunRequest.requested_by_id == attempt.principal_id,
                    RunRequest.origin == RunOrigin.STUDENT_ATTEMPT.value,
                    RunRequest.revision <= snapshot.revision,
                )
                .order_by(RunRequest.created_at)
            )
        ).all()
    )
    history: list[AttemptHistoryEventRead] = []
    for event in edit_events:
        is_paste = event.source == "INTERNAL_PASTE"
        history.append(
            AttemptHistoryEventRead(
                id=event.id,
                type="internal_paste" if is_paste else "edit",
                label="Внутренняя вставка" if is_paste else "Изменение файла",
                detail=event.event_type,
                at=event.received_at,
                revision=event.sequence,
                event=_edit_event_payload(event),
                client=normalize_client_context(event.client_context) or None,
            )
        )
    for item in snapshots:
        is_submit = item.reason in {"SUBMISSION", "DEADLINE"}
        is_moodle_import = item.reason == "LMS_IMPORT"
        is_attempt_start = item.reason == "ATTEMPT_STARTED"
        history.append(
            AttemptHistoryEventRead(
                id=item.id,
                type="submit" if is_submit else "snapshot",
                label=(
                    "Сдача работы"
                    if is_submit
                    else "Импортировано из Moodle"
                    if is_moodle_import
                    else "Начало попытки"
                    if is_attempt_start
                    else "Контрольная точка"
                ),
                detail=(
                    "История редактирования в Moodle недоступна"
                    if is_moodle_import
                    else item.reason
                ),
                at=item.created_at,
                revision=item.revision,
                client=(
                    normalize_client_context(submission.client_context) or None
                    if is_submit and item.id == submission.snapshot_id
                    else normalize_client_context(attempt.client_context) or None
                    if is_attempt_start
                    else None
                ),
            )
        )
    for run in runs:
        history.append(
            AttemptHistoryEventRead(
                id=run.id,
                type="run",
                label="Компиляция и запуск",
                detail=run.status,
                at=run.created_at,
                revision=run.revision,
                client=normalize_client_context(run.client_context) or None,
            )
        )
    history.sort(key=lambda row: (_aware(row.at) or datetime.min.replace(tzinfo=UTC), row.revision))
    return history


async def _submission_review_group(
    db: AsyncSession,
    *,
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> SubmissionReviewGroupRead | None:
    """Return Essay siblings belonging to this exact Moodle Quiz attempt.

    A Moodle Quiz is one student work, while each Essay question is imported as
    an independently reviewable local submission.  The parent assessment id,
    parent attempt id *and* student id are all required so repeated attempts,
    similarly numbered quizzes and other students can never be mixed.
    """

    receipt = dict(submission.external_receipt or {})
    parent_attempt_id = str(receipt.get("moodle_parent_attempt_id", "")).strip()
    policy = dict(assessment.policy or {})
    raw_parent_id = policy.get("moodle_parent_assessment_id")
    try:
        parent_assessment_id = uuid.UUID(str(raw_parent_id))
    except (TypeError, ValueError, AttributeError):
        return None
    if not parent_attempt_id:
        return None

    parent = await db.get(Assessment, parent_assessment_id)
    if parent is None or parent.course_id != assessment.course_id:
        return None
    rows = list(
        (
            await db.execute(
                select(Submission, Attempt, Assessment)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
                .where(
                    Attempt.principal_id == attempt.principal_id,
                    Attempt.state != AttemptState.VOID.value,
                    Assessment.course_id == assessment.course_id,
                    Submission.source == "MOODLE_IMPORT",
                )
            )
        ).all()
    )
    grouped: list[tuple[int, Submission, Assessment]] = []
    for sibling, sibling_attempt, sibling_assessment in rows:
        sibling_receipt = dict(sibling.external_receipt or {})
        sibling_policy = dict(sibling_assessment.policy or {})
        if (
            sibling_attempt.principal_id != attempt.principal_id
            or str(sibling_receipt.get("moodle_parent_attempt_id", "")).strip() != parent_attempt_id
            or str(sibling_policy.get("moodle_parent_assessment_id", ""))
            != str(parent_assessment_id)
        ):
            continue
        raw_position = sibling_receipt.get("moodle_response_position")
        try:
            position = int(raw_position)
        except (TypeError, ValueError, OverflowError):
            continue
        if not 1 <= position <= 1_000:
            continue
        grouped.append((position, sibling, sibling_assessment))
    if len(grouped) < 2:
        return None

    sibling_ids = [sibling.id for _position, sibling, _assessment in grouped]
    now = utcnow()
    claims = list(
        (
            await db.scalars(
                select(ReviewClaim)
                .where(
                    ReviewClaim.submission_id.in_(sibling_ids),
                    ReviewClaim.state == ReviewClaimState.ACTIVE.value,
                )
                .order_by(ReviewClaim.submission_id, ReviewClaim.created_at.desc())
            )
        ).all()
    )
    claims_by_submission: dict[uuid.UUID, ReviewClaim] = {}
    for claim in claims:
        expires_at = _aware(claim.lease_expires_at)
        if (
            claim.submission_id not in claims_by_submission
            and expires_at is not None
            and expires_at > now
        ):
            claims_by_submission[claim.submission_id] = claim
    decisions = list(
        (
            await db.scalars(
                select(ReviewDecision)
                .where(
                    ReviewDecision.submission_id.in_(sibling_ids),
                    ReviewDecision.status == "APPLIED",
                )
                .order_by(ReviewDecision.submission_id, ReviewDecision.revision.desc())
            )
        ).all()
    )
    decisions_by_submission: dict[uuid.UUID, ReviewDecision] = {}
    for decision in decisions:
        decisions_by_submission.setdefault(decision.submission_id, decision)

    by_position: dict[int, tuple[int, Submission, Assessment]] = {}
    for row in grouped:
        position, sibling, _sibling_assessment = row
        current = by_position.get(position)
        if current is None or _quiz_response_duplicate_rank(
            sibling,
            has_decision=sibling.id in decisions_by_submission,
            has_active_claim=sibling.id in claims_by_submission,
        ) > _quiz_response_duplicate_rank(
            current[1],
            has_decision=current[1].id in decisions_by_submission,
            has_active_claim=current[1].id in claims_by_submission,
        ):
            by_position[position] = row
    grouped = list(by_position.values())

    items: list[SubmissionReviewGroupItemRead] = []
    for position, sibling, sibling_assessment in sorted(
        grouped,
        key=lambda row: (row[0], str(row[1].id)),
    ):
        claim = claims_by_submission.get(sibling.id)
        decision = decisions_by_submission.get(sibling.id)
        items.append(
            SubmissionReviewGroupItemRead(
                submission_id=sibling.id,
                position=position,
                title=sibling_assessment.title,
                score=decision.grade if decision is not None else None,
                max_score=sibling_assessment.max_score,
                status="CLAIMED" if claim is not None else "GRADED" if decision else "UNGRADED",
            )
        )
    return SubmissionReviewGroupRead(
        id=uuid.uuid5(
            parent_assessment_id,
            f"{attempt.principal_id}:{parent_attempt_id}",
        ),
        title=parent.title,
        items=items,
    )


@router.get(
    "/submissions/{submission_id}",
    response_model=SubmissionTeacherRead,
    tags=["reviews"],
)
async def get_submission(
    submission_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> SubmissionTeacherRead:
    system_access = auth.has_capability("SYSTEM_SETTINGS")
    submission, attempt, assessment = await _submission_context(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=system_access,
    )
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")
    base = await _submission_list_item(
        db,
        submission=submission,
        teacher_id=auth.principal_id,
        can_review=system_access
        or (
            await submission_review_access(
                db,
                principal_id=auth.principal_id,
                submission_id=submission.id,
            )
        )
        is not None,
        allow_review_without_requirement=system_access,
    )
    decision_history = await _decision_history(db, submission.id)
    return SubmissionTeacherRead(
        **base.model_dump(exclude={"review_group"}),
        attempt_id=attempt.id,
        assigned_task_version_id=attempt.assigned_task_version_id,
        snapshot_id=snapshot.id,
        revision=snapshot.revision,
        source=submission.source,
        late=submission.late,
        lms_export_state=submission.lms_export_state,
        files=[_snapshot_file(snapshot, item) for item in snapshot.files],
        history=await _submission_history(
            db,
            submission=submission,
            attempt=attempt,
            snapshot=snapshot,
        ),
        latest_decision=next(
            (decision for decision in decision_history if decision.status == "APPLIED"),
            None,
        ),
        decision_history=decision_history,
        origin_verification=await submission_origin_verification(
            db,
            submission=submission,
            attempt=attempt,
            assessment=assessment,
        ),
        review_group=await _submission_review_group(
            db,
            submission=submission,
            attempt=attempt,
            assessment=assessment,
        ),
        created_at=submission.created_at,
        updated_at=submission.updated_at,
    )


@router.post(
    "/submissions/{submission_id}/claims",
    response_model=ReviewClaimRead,
    status_code=status.HTTP_201_CREATED,
    tags=["reviews"],
)
async def create_review_claim(
    submission_id: uuid.UUID,
    payload: ReviewClaimCreateRequest,
    auth: CurrentAuth,
    db: DB,
) -> ReviewClaimRead:
    claim = await claim_submission(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    if payload.takeover_reason:
        claim.takeover_reason = payload.takeover_reason
    await db.commit()
    return await _claim_read(db, claim, principal_id=auth.principal_id)


@router.post(
    "/review-claims/{claim_id}/heartbeat",
    response_model=ReviewClaimRead,
    tags=["reviews"],
)
async def refresh_review_claim(
    claim_id: uuid.UUID,
    _payload: ReviewClaimHeartbeatRequest,
    auth: CurrentAuth,
    db: DB,
) -> ReviewClaimRead:
    claim = await heartbeat_claim(
        db,
        claim_id=claim_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return await _claim_read(db, claim, principal_id=auth.principal_id)


@router.delete(
    "/review-claims/{claim_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["reviews"],
)
async def delete_review_claim(
    claim_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> Response:
    await release_claim(
        db,
        claim_id=claim_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/submissions/{submission_id}/review-draft",
    response_model=ReviewDraftRead | None,
    tags=["reviews"],
)
async def get_review_draft(
    submission_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> ReviewDraftRead | None:
    await _submission_context(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    draft = await db.scalar(select(ReviewDraft).where(ReviewDraft.submission_id == submission_id))
    if draft is None or draft.owner_id != auth.principal_id:
        return None
    return ReviewDraftRead.model_validate(draft)


@router.put(
    "/submissions/{submission_id}/review-draft",
    response_model=ReviewDraftRead,
    tags=["reviews"],
)
async def put_review_draft(
    submission_id: uuid.UUID,
    payload: ReviewDraftUpsertRequest,
    auth: CurrentAuth,
    db: DB,
) -> ReviewDraftRead:
    draft = await save_review_draft(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        grade=payload.grade,
        comment=payload.comment,
        criterion_scores=payload.criterion_scores,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return ReviewDraftRead.model_validate(draft)


@router.post(
    "/submissions/{submission_id}/review-decisions",
    response_model=ReviewDecisionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["reviews"],
)
async def create_review_decision(
    submission_id: uuid.UUID,
    payload: ReviewDecisionCreateRequest,
    auth: CurrentAuth,
    db: DB,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> ReviewDecisionRead:
    decision = await finalize_review(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        grade=payload.grade,
        comment=payload.comment,
        criterion_scores=payload.criterion_scores,
        evidence_ids=payload.evidence_ids,
        idempotency_key=idempotency_key,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return ReviewDecisionRead.model_validate(decision)


async def _experiment_read(
    db: AsyncSession, experiment: TeacherExperiment
) -> TeacherExperimentRead:
    files = list(
        (
            await db.scalars(
                select(TeacherExperimentFile)
                .where(TeacherExperimentFile.experiment_id == experiment.id)
                .order_by(TeacherExperimentFile.path)
            )
        ).all()
    )
    submission = await db.get(Submission, experiment.submission_id)
    snapshot = await db.get(Snapshot, submission.snapshot_id) if submission else None
    if snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")
    original_hashes = {
        str(item.get("path", "main.cpp")): str(item.get("content_hash", ""))
        for item in snapshot.files
    }
    current_hashes = {file.path: file.content_hash for file in files}
    return TeacherExperimentRead(
        id=experiment.id,
        submission_id=experiment.submission_id,
        revision=experiment.revision,
        files=[
            TeacherExperimentFileRead(
                id=file.id,
                path=file.path,
                content=file.content,
                language=language_for_path(file.path),
            )
            for file in files
        ],
        changed=current_hashes != original_hashes,
        expires_at=experiment.expires_at,
        reset_at=experiment.reset_at,
        created_at=experiment.created_at,
        updated_at=experiment.updated_at,
    )


@router.post(
    "/submissions/{submission_id}/teacher-experiments",
    response_model=TeacherExperimentRead,
    status_code=status.HTTP_201_CREATED,
    tags=["teacher-sandbox"],
)
async def create_experiment(
    submission_id: uuid.UUID,
    _payload: TeacherExperimentCreateRequest,
    auth: CurrentAuth,
    db: DB,
) -> TeacherExperimentRead:
    experiment = await create_teacher_experiment(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return await _experiment_read(db, experiment)


@router.patch(
    "/teacher-experiments/{experiment_id}/files/{file_id}",
    response_model=TeacherExperimentFilePatchRead,
    tags=["teacher-sandbox"],
)
async def patch_experiment_file(
    experiment_id: uuid.UUID,
    file_id: uuid.UUID,
    payload: TeacherExperimentFilePatchRequest,
    auth: CurrentAuth,
    db: DB,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> TeacherExperimentFilePatchRead:
    experiment, file = await update_experiment_file(
        db,
        experiment_id=experiment_id,
        teacher_id=auth.principal_id,
        file_id=file_id,
        content=payload.content,
        expected_revision=_expected_revision(if_match),
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return TeacherExperimentFilePatchRead(
        file=TeacherExperimentFileRead(
            id=file.id,
            path=file.path,
            content=file.content,
            language=language_for_path(file.path),
        ),
        revision=experiment.revision,
    )


@router.post(
    "/teacher-experiments/{experiment_id}/reset",
    response_model=TeacherExperimentRead,
    tags=["teacher-sandbox"],
)
async def reset_experiment(
    experiment_id: uuid.UUID,
    _payload: TeacherExperimentResetRequest,
    auth: CurrentAuth,
    db: DB,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> TeacherExperimentRead:
    experiment = await reset_teacher_experiment(
        db,
        experiment_id=experiment_id,
        teacher_id=auth.principal_id,
        expected_revision=_expected_revision(if_match),
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    await db.commit()
    return await _experiment_read(db, experiment)


@router.delete(
    "/teacher-experiments/{experiment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["teacher-sandbox"],
)
async def delete_experiment(
    experiment_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> Response:
    experiment = await db.get(TeacherExperiment, experiment_id)
    if experiment is None or experiment.owner_id != auth.principal_id:
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await _submission_context(
        db,
        submission_id=experiment.submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    experiment.deleted_at = utcnow()
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/teacher-experiments/{experiment_id}/runs",
    response_model=RunTeacherRead,
    status_code=status.HTTP_201_CREATED,
    tags=["teacher-sandbox", "runs"],
)
async def run_experiment(
    experiment_id: uuid.UUID,
    payload: RunCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> RunTeacherRead:
    experiment_hint = await db.get(TeacherExperiment, experiment_id)
    if experiment_hint is None:
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    submission, attempt, _assessment = await _submission_context(
        db,
        submission_id=experiment_hint.submission_id,
        teacher_id=auth.principal_id,
        lock=True,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    experiment = await db.scalar(
        select(TeacherExperiment).where(TeacherExperiment.id == experiment_id).with_for_update()
    )
    expires_at = _aware(experiment.expires_at) if experiment else None
    if (
        experiment is None
        or experiment.owner_id != auth.principal_id
        or experiment.deleted_at is not None
        or not expires_at
        or expires_at <= utcnow()
    ):
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await require_teacher_experiment_access(
        db,
        submission_id=submission.id,
        teacher_id=auth.principal_id,
    )
    if experiment.revision != payload.revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Experiment revision is stale",
            {"current_revision": experiment.revision},
        )
    settings: Settings = request.app.state.settings
    flags = await _effective_flags(db, settings)
    if not flags["runner_enabled"]:
        raise DomainError(503, "RUNNER_DISABLED", "Compilation and execution are disabled")
    version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt.assigned_task_version_id
        else None
    )
    if version is None:
        raise DomainError(500, "TASK_VERSION_MISSING", "Assigned task version is missing")
    build_profile = await effective_attempt_build_profile(
        db,
        attempt=attempt,
        configured_profile=version.build_profile,
    )
    files = list(
        (
            await db.scalars(
                select(TeacherExperimentFile)
                .where(TeacherExperimentFile.experiment_id == experiment.id)
                .order_by(TeacherExperimentFile.path)
            )
        ).all()
    )
    source_manifest = [{"path": file.path, "content": file.content} for file in files]
    run = RunRequest(
        origin=RunOrigin.TEACHER_EXPERIMENT.value,
        attempt_id=attempt.id,
        submission_id=submission.id,
        teacher_experiment_id=experiment.id,
        requested_by_id=auth.principal_id,
        revision=experiment.revision,
        mode=payload.mode,
        build_profile=build_profile,
        filesystem_profile="UNRESTRICTED_CONTAINER",
        network_enabled=True,
        stdin=payload.stdin,
        status=RunStatus.RUNNING.value,
    )
    db.add(run)
    await db.commit()

    result: RunnerResult | None = None
    integration_error: IntegrationError | None = None
    try:
        if settings.runner_mock_enabled:
            result = _mock_result(run.id)
        else:
            async with httpx.AsyncClient() as client:
                result = await RunnerAdapter(settings, client).dispatch(
                    request_id=str(run.id),
                    profile_id=build_profile,
                    files=source_manifest,
                    stdin=payload.stdin,
                    mode=payload.mode,
                    limits={
                        "cpu_seconds": flags["runner_cpu_seconds"],
                        "memory_mb": flags["runner_memory_mb"],
                    },
                )
    except IntegrationError as exc:
        integration_error = exc
    run = await _store_run_result(db, run_id=run.id, result=result, error=integration_error)
    response = await _run_read(db, run, teacher=True)
    if not isinstance(response, RunTeacherRead):
        raise DomainError(500, "RUN_RESPONSE_INVALID", "Teacher run response is invalid")
    return response


async def _interactive_experiment(
    db: AsyncSession,
    *,
    experiment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    revision: int | None = None,
    allow_system_settings_read: bool = False,
    lock_start_parent: bool = False,
) -> tuple[TeacherExperiment, TaskVersion, list[TeacherExperimentFile]]:
    experiment_hint = await db.get(TeacherExperiment, experiment_id)
    if experiment_hint is None or experiment_hint.owner_id != teacher_id:
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    submission, attempt, _assessment = await _submission_context(
        db,
        submission_id=experiment_hint.submission_id,
        teacher_id=teacher_id,
        # Match the lock order used by experiment mutation endpoints:
        # submission first, then experiment.  Holding both rows through the
        # active-session check and RunRequest commit serializes concurrent
        # interactive starts without a schema-specific partial index.
        lock=lock_start_parent,
        allow_system_settings_read=allow_system_settings_read,
    )
    experiment = (
        await db.scalar(
            select(TeacherExperiment)
            .where(TeacherExperiment.id == experiment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if lock_start_parent
        else experiment_hint
    )
    if experiment is None or experiment.owner_id != teacher_id:
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    expires_at = _aware(experiment.expires_at)
    if experiment.deleted_at is not None or not expires_at or expires_at <= utcnow():
        raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
    await require_teacher_experiment_access(
        db,
        submission_id=submission.id,
        teacher_id=teacher_id,
    )
    if revision is not None and experiment.revision != revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Experiment revision is stale",
            {"current_revision": experiment.revision},
        )
    version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt.assigned_task_version_id
        else None
    )
    if version is None:
        raise DomainError(500, "TASK_VERSION_MISSING", "Assigned task version is missing")
    files = list(
        (
            await db.scalars(
                select(TeacherExperimentFile)
                .where(TeacherExperimentFile.experiment_id == experiment.id)
                .order_by(TeacherExperimentFile.path)
            )
        ).all()
    )
    return experiment, version, files


def _mock_interactive() -> InteractiveRunRead:
    return InteractiveRunRead(
        session_id=uuid.uuid4().hex,
        status="SUCCESS",
        terminal=True,
        exit_code=0,
        duration_ms=0,
        stdout="Runner mock: программа не запускалась.",
        stderr="",
        output_truncated=False,
        diagnostics=[],
    )


async def _teacher_interactive_run(
    db: AsyncSession,
    *,
    experiment: TeacherExperiment,
    teacher_id: uuid.UUID,
    session_id: str,
) -> RunRequest:
    run = await db.scalar(
        select(RunRequest).where(
            RunRequest.teacher_experiment_id == experiment.id,
            RunRequest.requested_by_id == teacher_id,
            RunRequest.origin == RunOrigin.TEACHER_EXPERIMENT.value,
            RunRequest.mode == "INTERACTIVE",
            RunRequest.external_job_id == session_id,
        )
    )
    if run is None:
        raise DomainError(404, "INTERACTIVE_SESSION_NOT_FOUND", "Interactive session was not found")
    return run


@router.post(
    "/teacher-experiments/{experiment_id}/interactive-sessions",
    response_model=InteractiveRunRead,
    status_code=status.HTTP_201_CREATED,
    tags=["teacher-sandbox", "interactive-runs"],
)
async def start_interactive_experiment(
    experiment_id: uuid.UUID,
    payload: InteractiveRunCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    settings: Settings = request.app.state.settings
    async with _interactive_start_admission_lock(request):
        experiment, version, files = await _interactive_experiment(
            db,
            experiment_id=experiment_id,
            teacher_id=auth.principal_id,
            revision=payload.revision,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
            lock_start_parent=True,
        )
        flags = await _effective_flags(db, settings)
        if not flags["runner_enabled"]:
            raise DomainError(503, "RUNNER_DISABLED", "Compilation and execution are disabled")
        await _enforce_run_budget(db, principal_id=auth.principal_id, settings=settings)
        existing = await db.scalar(
            select(RunRequest).where(
                RunRequest.teacher_experiment_id == experiment.id,
                RunRequest.requested_by_id == auth.principal_id,
                RunRequest.mode == "INTERACTIVE",
                RunRequest.status == RunStatus.RUNNING.value,
            )
        )
        if existing is not None:
            raise DomainError(
                409,
                "INTERACTIVE_SESSION_ACTIVE",
                "An interactive program is already running for this experiment",
            )
        submission = await db.get(Submission, experiment.submission_id)
        attempt = await db.get(Attempt, submission.attempt_id) if submission is not None else None
        if submission is None or attempt is None:
            raise DomainError(500, "SUBMISSION_CONTEXT_MISSING", "Submission context is incomplete")
        build_profile = await effective_attempt_build_profile(
            db,
            attempt=attempt,
            configured_profile=version.build_profile,
        )
        run = RunRequest(
            origin=RunOrigin.TEACHER_EXPERIMENT.value,
            attempt_id=attempt.id,
            submission_id=submission.id,
            teacher_experiment_id=experiment.id,
            requested_by_id=auth.principal_id,
            revision=experiment.revision,
            mode="INTERACTIVE",
            build_profile=build_profile,
            filesystem_profile="UNRESTRICTED_CONTAINER",
            network_enabled=True,
            stdin="",
            status=RunStatus.RUNNING.value,
        )
        db.add(run)
        # Publish the reservation before contacting the runner, while both
        # the worker guard and PostgreSQL parent-row locks are still held.
        await db.commit()
    if settings.runner_mock_enabled:
        response = _mock_interactive()
        await _record_interactive_response(db, run_id=run.id, raw=response)
        return response
    try:
        async with httpx.AsyncClient() as client:
            raw = await RunnerAdapter(settings, client).start_interactive(
                request_id=str(run.id),
                owner_key=str(experiment.id),
                profile_id=build_profile,
                files=[{"path": file.path, "content": file.content} for file in files],
                limits={
                    "cpu_seconds": flags["runner_cpu_seconds"],
                    "memory_mb": flags["runner_memory_mb"],
                },
            )
    except IntegrationError as exc:
        await _store_run_result(db, run_id=run.id, result=None, error=exc)
        raise DomainError(
            503,
            "RUNNER_UNAVAILABLE",
            "Сервис компиляции временно недоступен",
        ) from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response


@router.post(
    "/teacher-experiments/{experiment_id}/interactive-sessions/{session_id}/state",
    response_model=InteractiveRunRead,
    tags=["teacher-sandbox", "interactive-runs"],
)
async def interactive_experiment_state(
    experiment_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    experiment, _version, _files = await _interactive_experiment(
        db,
        experiment_id=experiment_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    run = await _teacher_interactive_run(
        db, experiment=experiment, teacher_id=auth.principal_id, session_id=session_id
    )
    settings: Settings = request.app.state.settings
    try:
        async with httpx.AsyncClient() as client:
            raw = await RunnerAdapter(settings, client).interactive_state(
                session_id=session_id, owner_key=str(experiment.id)
            )
    except IntegrationError as exc:
        raise DomainError(503, "RUNNER_UNAVAILABLE", "Не удалось получить вывод программы") from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response


@router.post(
    "/teacher-experiments/{experiment_id}/interactive-sessions/{session_id}/input",
    response_model=InteractiveRunRead,
    tags=["teacher-sandbox", "interactive-runs"],
)
async def interactive_experiment_input(
    experiment_id: uuid.UUID,
    session_id: str,
    payload: InteractiveRunInputRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    experiment, _version, _files = await _interactive_experiment(
        db,
        experiment_id=experiment_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    run = await _teacher_interactive_run(
        db, experiment=experiment, teacher_id=auth.principal_id, session_id=session_id
    )
    if run.status != RunStatus.RUNNING.value:
        raise DomainError(409, "INTERACTIVE_SESSION_FINISHED", "Interactive program is not running")
    settings: Settings = request.app.state.settings
    try:
        async with httpx.AsyncClient() as client:
            raw = await RunnerAdapter(settings, client).interactive_input(
                session_id=session_id,
                owner_key=str(experiment.id),
                text=payload.text,
            )
    except IntegrationError as exc:
        raise DomainError(503, "RUNNER_UNAVAILABLE", "Не удалось передать ввод программе") from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response


@router.post(
    "/teacher-experiments/{experiment_id}/interactive-sessions/{session_id}/eof",
    response_model=InteractiveRunRead,
    tags=["teacher-sandbox", "interactive-runs"],
)
async def interactive_experiment_eof(
    experiment_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    experiment, _version, _files = await _interactive_experiment(
        db,
        experiment_id=experiment_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    run = await _teacher_interactive_run(
        db, experiment=experiment, teacher_id=auth.principal_id, session_id=session_id
    )
    if run.status != RunStatus.RUNNING.value:
        raise DomainError(409, "INTERACTIVE_SESSION_FINISHED", "Interactive program is not running")
    settings: Settings = request.app.state.settings
    try:
        async with httpx.AsyncClient() as client:
            raw = await RunnerAdapter(settings, client).interactive_eof(
                session_id=session_id, owner_key=str(experiment.id)
            )
    except IntegrationError as exc:
        raise DomainError(503, "RUNNER_UNAVAILABLE", "Не удалось завершить ввод программы") from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response


@router.post(
    "/teacher-experiments/{experiment_id}/interactive-sessions/{session_id}/stop",
    response_model=InteractiveRunRead,
    tags=["teacher-sandbox", "interactive-runs"],
)
async def stop_interactive_experiment(
    experiment_id: uuid.UUID,
    session_id: str,
    request: Request,
    auth: CurrentAuth,
    db: DB,
) -> InteractiveRunRead:
    experiment, _version, _files = await _interactive_experiment(
        db,
        experiment_id=experiment_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    run = await _teacher_interactive_run(
        db, experiment=experiment, teacher_id=auth.principal_id, session_id=session_id
    )
    settings: Settings = request.app.state.settings
    try:
        async with httpx.AsyncClient() as client:
            raw = await RunnerAdapter(settings, client).interactive_stop(
                session_id=session_id, owner_key=str(experiment.id)
            )
    except IntegrationError as exc:
        raise DomainError(503, "RUNNER_UNAVAILABLE", "Не удалось остановить программу") from exc
    response = InteractiveRunRead.model_validate(raw)
    await _record_interactive_response(db, run_id=run.id, raw=response)
    return response
