"""Reconcile exact, positively confirmed Moodle Quiz deletions.

A missing report row is never deletion evidence: group filters, pagination and
permission changes all legitimately hide attempts. Only the connector's exact
missing ``quiz_attempts`` record response for a requested, known id is authoritative.
Source snapshots stay intact; VOID is a recoverable local tombstone.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import String, case, cast, func, or_, select, union, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.integrations.errors import IntegrationProtocolError
from app.models.attempts import Attempt, Submission
from app.models.courses import Course
from app.models.enums import AttemptState, ReviewClaimState, SyncOutboxState
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ReviewClaim, ReviewDecision
from app.models.tasks import Assessment

DELETED_QUIZ_ATTEMPT_TYPE = "moodle_deleted_quiz_attempt"
_OBSERVATION_TYPE = "moodle_attempt_observation"
_ERROR = "MOODLE_ATTEMPT_DELETED: The exact Moodle Quiz attempt was deleted"


def _remote_id(value: Any) -> str:
    value = str(value or "").removeprefix("attempt:").strip()
    return value if value.isascii() and value.isdigit() and int(value) > 0 else ""


def _scope(course: Course, assessment: Assessment):
    return (
        Assessment.course_id == course.id,
        or_(
            Assessment.id == assessment.id,
            Assessment.policy["moodle_parent_assessment_id"].as_string() == str(assessment.id),
        ),
    )


def _attempt_remote_expression():
    raw = cast(
        func.coalesce(
            func.nullif(Submission.external_receipt["moodle_parent_attempt_id"].as_string(), ""),
            Attempt.integrity_policy["moodle_attempt_id"].as_string(),
        ),
        String,
    )
    return case((raw.startswith("attempt:"), func.substr(raw, 9)), else_=raw)


def _known_quiz_attempt_query(course: Course, assessment: Assessment, cmid: int):
    remote = _attempt_remote_expression().label("remote_id")
    local_ids = (
        select(remote)
        .select_from(Attempt)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .outerjoin(Submission, Submission.attempt_id == Attempt.id)
        .where(*_scope(course, assessment), Attempt.state != AttemptState.VOID.value)
    )
    marker_raw = cast(ExternalMapping.metadata_json["remote_attempt_id"].as_string(), String)
    marker_id = case(
        (marker_raw.startswith("attempt:"), func.substr(marker_raw, 9)), else_=marker_raw
    )
    marker_ids = select(marker_id.label("remote_id")).where(
        ExternalMapping.connection_id == course.connection_id,
        ExternalMapping.local_id == assessment.id,
        ExternalMapping.external_type == _OBSERVATION_TYPE,
        ExternalMapping.metadata_json["module"].as_string() == "quiz",
        ExternalMapping.metadata_json["cmid"].as_string() == str(cmid),
        or_(
            ExternalMapping.metadata_json["deleted_in_moodle"].as_boolean().is_(None),
            ExternalMapping.metadata_json["deleted_in_moodle"].as_boolean().is_(False),
        ),
    )
    candidates = union(local_ids, marker_ids).subquery()
    return select(candidates.c.remote_id).where(
        candidates.c.remote_id.is_not(None), candidates.c.remote_id != ""
    )


async def known_quiz_attempt_ids(
    db: AsyncSession, *, course: Course, assessment: Assessment, cmid: int
) -> list[str]:
    """Snapshot all known identities for one manual run's bounded probe jobs."""
    rows = await db.scalars(_known_quiz_attempt_query(course, assessment, cmid))
    return sorted({value for row in rows if (value := _remote_id(row))})


async def known_quiz_attempt_probe_batch(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
    mapping: ExternalMapping,
    cmid: int,
) -> list[str]:
    """Read one bounded legacy probe batch without files or answers."""
    query = _known_quiz_attempt_query(course, assessment, cmid)
    remote_id = query.selected_columns.remote_id
    metadata = dict(mapping.metadata_json or {})
    after = str(metadata.get("attempt_probe_after", ""))
    ids = list(
        (
            await db.scalars(
                query.where(remote_id > after)
                .order_by(remote_id)
                .limit(5)
            )
        ).all()
    )
    if len(ids) < 5 and after:
        ids.extend(
            (
                await db.scalars(
                    query.where(remote_id <= after)
                    .order_by(remote_id)
                    .limit(5 - len(ids))
                )
            ).all()
        )
    if ids:
        metadata["attempt_probe_after"] = ids[-1]
        mapping.metadata_json = metadata
    return [value for raw in ids if (value := _remote_id(raw))]


async def deleted_quiz_attempt_ids(
    db: AsyncSession, *, course: Course, assessment: Assessment
) -> set[str]:
    rows = await db.scalars(
        select(ExternalMapping.metadata_json).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.local_id == assessment.id,
            ExternalMapping.external_type == DELETED_QUIZ_ATTEMPT_TYPE,
        )
    )
    return {
        value
        for row in rows
        if isinstance(row, dict) and (value := _remote_id(row.get("remote_attempt_id")))
    }


async def reconcile_deleted_quiz_attempts(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
    cmid: int,
    actor_external_subject: str,
    known_attempt_ids: list[str],
    deleted_attempt_ids: object,
) -> int:
    """Retire all native/imported siblings of explicitly deleted remote attempts."""

    if (
        not isinstance(deleted_attempt_ids, list)
        or len(deleted_attempt_ids) > 5
        or any(
            not isinstance(value, str) or _remote_id(value) != value
            for value in deleted_attempt_ids
        )
        or not set(deleted_attempt_ids).issubset(known_attempt_ids)
    ):
        raise IntegrationProtocolError("Moodle deleted attempt evidence is invalid")
    deleted = set(deleted_attempt_ids)
    if not deleted:
        return 0
    if assessment.course_id != course.id:
        raise IntegrationProtocolError("Moodle deleted attempt ownership is invalid")
    now = utcnow()
    # A report can discover deletion while a pinned answer job is queued or
    # already reading. Fence that job too, otherwise it retries an impossible
    # download forever or shows a false history error after a successful sync.
    await db.execute(
        update(SyncOutbox)
        .where(
            SyncOutbox.connection_id == course.connection_id,
            SyncOutbox.course_id == course.id,
            SyncOutbox.aggregate_id == assessment.id,
            SyncOutbox.event_type == "moodle.history.import",
            SyncOutbox.payload["module"].as_string() == "quiz",
            SyncOutbox.payload["cmid"].as_integer() == cmid,
            SyncOutbox.payload["detail_key"].as_string().in_(deleted),
            SyncOutbox.state != SyncOutboxState.DELIVERED.value,
        )
        .values(
            state=SyncOutboxState.DELIVERED.value,
            locked_at=None,
            last_error="",
            delivered_at=now,
            receipt={"status": "REMOTE_DELETED", "warning_count": 0},
        )
    )
    for remote_id in sorted(deleted):
        external_id = f"quiz:{cmid}:{remote_id}"
        tombstone = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.connection_id == course.connection_id,
                ExternalMapping.external_type == DELETED_QUIZ_ATTEMPT_TYPE,
                ExternalMapping.external_id == external_id,
            )
        )
        if tombstone is None:
            db.add(
                ExternalMapping(
                    connection_id=course.connection_id,
                    local_type="MoodleDeletedQuizAttempt",
                    local_id=assessment.id,
                    external_type=DELETED_QUIZ_ATTEMPT_TYPE,
                    external_id=external_id,
                    metadata_json={
                        "remote_attempt_id": remote_id,
                        "cmid": cmid,
                        "course_id": course.external_id,
                        "actor_external_subject": actor_external_subject,
                        "confirmed_at": now.isoformat(),
                        "proof": "MISSING_QUIZ_ATTEMPT_RECORD",
                    },
                )
            )
    remote = _attempt_remote_expression()
    matching_ids = (
        select(Attempt.id)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .outerjoin(Submission, Submission.attempt_id == Attempt.id)
        .where(*_scope(course, assessment), remote.in_(deleted))
    )
    attempts = list(
        (
            await db.scalars(
                select(Attempt)
                .where(Attempt.id.in_(matching_ids))
                .order_by(Attempt.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).all()
    )
    attempt_ids = [attempt.id for attempt in attempts]
    for attempt in attempts:
        policy = dict(attempt.integrity_policy or {})
        if attempt.state != AttemptState.VOID.value:
            policy["state_before_moodle_deletion"] = attempt.state
        policy.update({"closure_reason": "MOODLE_DELETED", "moodle_deleted_at": now.isoformat()})
        attempt.state = AttemptState.VOID.value
        attempt.submission_source = "MOODLE_DELETED"
        attempt.integrity_policy = policy
    submissions = (
        list(
            (
                await db.scalars(select(Submission).where(Submission.attempt_id.in_(attempt_ids)))
            ).all()
        )
        if attempt_ids
        else []
    )
    submission_ids = [submission.id for submission in submissions]
    decisions = (
        list(
            (
                await db.scalars(
                    select(ReviewDecision).where(ReviewDecision.submission_id.in_(submission_ids))
                )
            ).all()
        )
        if submission_ids
        else []
    )
    # Fence in-flight workers as well as queued retries. A late network response
    # cannot pass _owned_claim once this transaction retires its outbox row.
    if attempt_ids:
        await db.execute(
            update(SyncOutbox)
            .where(
                SyncOutbox.connection_id == course.connection_id,
                SyncOutbox.course_id == course.id,
                or_(
                    SyncOutbox.attempt_id.in_(attempt_ids),
                    SyncOutbox.aggregate_id.in_([decision.id for decision in decisions]),
                ),
                SyncOutbox.event_type.in_(["attempt.checkpoint", "review.decision"]),
                SyncOutbox.state != SyncOutboxState.DELIVERED.value,
            )
            .values(state=SyncOutboxState.BLOCKED.value, locked_at=None, last_error=_ERROR)
        )
    for submission in submissions:
        submission.external_receipt = {
            **dict(submission.external_receipt or {}),
            "deleted_in_moodle": True,
            "moodle_deleted_at": now.isoformat(),
        }
        if submission.lms_export_state != "DELIVERED":
            submission.lms_export_state = "BLOCKED"
    for decision in decisions:
        if decision.lms_export_state not in {"DELIVERED", "IMPORTED"}:
            decision.lms_export_state = "BLOCKED"
    if submission_ids:
        await db.execute(
            update(ReviewClaim)
            .where(
                ReviewClaim.submission_id.in_(submission_ids),
                ReviewClaim.state == ReviewClaimState.ACTIVE.value,
            )
            .values(state=ReviewClaimState.EXPIRED.value, lease_expires_at=now)
        )
    markers = await db.scalars(
        select(ExternalMapping)
        .where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.local_id == assessment.id,
            ExternalMapping.external_type == _OBSERVATION_TYPE,
        )
        .with_for_update()
    )
    for marker in markers:
        metadata = dict(marker.metadata_json or {})
        if (
            metadata.get("module") == "quiz"
            and str(metadata.get("cmid")) == str(cmid)
            and _remote_id(metadata.get("remote_attempt_id")) in deleted
        ):
            marker.metadata_json = {**metadata, "deleted_in_moodle": True}
    await db.flush()
    return len(attempts)
