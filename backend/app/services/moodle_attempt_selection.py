from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.attempts import Attempt, Submission
from app.models.courses import Course
from app.models.enums import AttemptState
from app.models.identity import ExternalPrincipal
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment

MoodleAttemptRow = tuple[Submission, Attempt, Assessment]

_ASSIGN_ATTEMPT_SUFFIX = re.compile(r"(?:^|-)attempt-([0-9]+)$")
_LEGACY_QUIZ_ATTEMPT = re.compile(r"^attempt:([0-9]+)$")
MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE = "moodle_attempt_observation"
_OBSERVATION_LOCAL_TYPE = "MoodleAttemptObservation"
_COMPLETED_ATTEMPT_STATES = {
    AttemptState.SUBMITTED.value,
    AttemptState.AUTO_SUBMITTED.value,
    AttemptState.LOCKED.value,
}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parent_assessment_id(assessment: Assessment) -> uuid.UUID:
    """Resolve the local activity that owns a split Quiz Essay question."""

    raw = dict(assessment.policy or {}).get("moodle_parent_assessment_id")
    try:
        return uuid.UUID(str(raw)) if raw else assessment.id
    except (TypeError, ValueError, AttributeError):
        # A malformed policy must not make unrelated assessments share an
        # attempt-selection scope.
        return assessment.id


def _completed_moodle_attempt_key(
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> tuple[uuid.UUID, uuid.UUID, str] | None:
    """Return (activity, student, remote attempt) for a completed LMS attempt."""

    if attempt.state not in _COMPLETED_ATTEMPT_STATES:
        return None
    receipt = dict(submission.external_receipt or {})
    policy = dict(attempt.integrity_policy or {})
    remote_attempt_id = str(receipt.get("moodle_parent_attempt_id", "")).strip()
    if not remote_attempt_id and submission.source != "MOODLE_IMPORT":
        remote_attempt_id = str(policy.get("moodle_attempt_id", "")).strip()
    if not remote_attempt_id:
        return None
    return _parent_assessment_id(assessment), attempt.principal_id, remote_attempt_id


def is_completed_moodle_import(
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> bool:
    return (
        submission.source == "MOODLE_IMPORT"
        and _completed_moodle_attempt_key(submission, attempt, assessment) is not None
    )


def is_completed_moodle_submission(
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> bool:
    """Return whether a completed submission is bound to a Moodle attempt."""

    return _completed_moodle_attempt_key(submission, attempt, assessment) is not None


def _remote_attempt_order(remote_attempt_id: str) -> tuple[int, int, str]:
    """Provide a deterministic tie-break without consulting local sequence.

    Quiz attempt ids are positive integers. Assignment imports carry
    ``user-<id>-attempt-<number>``. Moodle timestamps have second precision, so
    two completed attempts can legitimately have an equal ``submitted_at``.
    """

    if remote_attempt_id.isdigit():
        return 2, int(remote_attempt_id), remote_attempt_id
    legacy_quiz = _LEGACY_QUIZ_ATTEMPT.fullmatch(remote_attempt_id)
    if legacy_quiz is not None:
        return 2, int(legacy_quiz.group(1)), remote_attempt_id
    match = _ASSIGN_ATTEMPT_SUFFIX.search(remote_attempt_id)
    if match is not None:
        return 1, int(match.group(1)), remote_attempt_id
    return 0, 0, remote_attempt_id


def _remote_attempt_position(remote_attempt_id: str) -> tuple[int, int, str]:
    """Compare remote attempts while tolerating legacy Quiz id spelling.

    ``123`` and ``attempt:123`` identify the same Moodle Quiz attempt.  The
    raw text remains a tie-break only for opaque ids whose order cannot be
    parsed.
    """

    kind, number, raw = _remote_attempt_order(remote_attempt_id)
    return kind, number, "" if kind else raw


def _observation_external_id(
    *,
    assessment_id: uuid.UUID,
    principal_id: uuid.UUID,
    actor_external_subject: str,
) -> str:
    actor_marker = uuid.uuid5(
        assessment_id,
        f"{principal_id}:{actor_external_subject}",
    )
    return f"{assessment_id}:{principal_id}:{actor_marker}"


def _observation_metadata(mapping: ExternalMapping) -> dict[str, object] | None:
    metadata = mapping.metadata_json
    if not isinstance(metadata, dict):
        return None
    remote_attempt_id = str(metadata.get("remote_attempt_id", "")).strip()
    principal_id = str(metadata.get("principal_id", "")).strip()
    if not remote_attempt_id or not principal_id:
        return None
    return metadata


def _observation_reviewable(metadata: dict[str, object]) -> bool:
    # Old marker rows written during a rolling deploy may not have the explicit
    # flag.  The state is enough to retain the fail-closed active-attempt rule.
    value = metadata.get("reviewable")
    if isinstance(value, bool):
        return value
    return str(metadata.get("state", "")).upper() != "IN_PROGRESS"


def _observation_recency(metadata: dict[str, object]) -> tuple[int, int, str, int]:
    remote_attempt_id = str(metadata.get("remote_attempt_id", "")).strip()
    return (*_remote_attempt_position(remote_attempt_id), int(_observation_reviewable(metadata)))


async def observe_moodle_attempt(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
    principal: ExternalPrincipal,
    actor_external_subject: str,
    remote_attempt_id: str,
    state: str,
    module: str,
    cmid: str,
    submitted_at_epoch: int,
    external_revision: str,
) -> None:
    """Persist the latest remote attempt even while it is still a draft.

    Historical Moodle drafts intentionally do not become local Submissions,
    but their existence must immediately supersede an older completed attempt
    in the active review/grade path.  One marker per crawling actor avoids an
    absent-row uniqueness race between independent teacher browser sessions;
    readers conservatively merge all actor observations.
    """

    actor_external_subject = actor_external_subject.strip()[:255]
    remote_attempt_id = remote_attempt_id.strip()[:160]
    state = state.strip().upper()[:32]
    module = module.strip().removeprefix("mod_").lower()[:16]
    cmid = cmid.strip()[:64]
    if (
        not actor_external_subject
        or not remote_attempt_id
        or state not in {"IN_PROGRESS", "SUBMITTED", "GRADED", "UNKNOWN", "FINISHED"}
        or module not in {"assign", "quiz"}
        or not cmid.isdigit()
        or int(cmid) <= 0
    ):
        return
    if assessment.course_id != course.id or principal.connection_id != course.connection_id:
        raise ValueError("Moodle attempt observation ownership is invalid")

    parent_assessment_id = _parent_assessment_id(assessment)
    external_id = _observation_external_id(
        assessment_id=parent_assessment_id,
        principal_id=principal.id,
        actor_external_subject=actor_external_subject,
    )
    mapping = await db.scalar(
        select(ExternalMapping)
        .where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.external_type == MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE,
            ExternalMapping.external_id == external_id,
        )
        .with_for_update()
    )
    reviewable = state != "IN_PROGRESS"
    now = utcnow()
    incoming: dict[str, object] = {
        "assessment_id": str(parent_assessment_id),
        "course_id": str(course.id),
        "principal_id": str(principal.id),
        "actor_external_subject": actor_external_subject,
        "remote_attempt_id": remote_attempt_id,
        "state": state,
        "reviewable": reviewable,
        "module": module,
        "cmid": cmid,
        "submitted_at_epoch": max(0, submitted_at_epoch),
        "observed_at": now.isoformat(),
    }
    if mapping is None:
        db.add(
            ExternalMapping(
                connection_id=course.connection_id,
                local_type=_OBSERVATION_LOCAL_TYPE,
                local_id=parent_assessment_id,
                external_type=MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE,
                external_id=external_id,
                external_revision=external_revision.strip()[:255],
                metadata_json=incoming,
            )
        )
        return

    current = _observation_metadata(mapping)
    if current is not None:
        current_position = _remote_attempt_position(
            str(current.get("remote_attempt_id", "")).strip()
        )
        incoming_position = _remote_attempt_position(remote_attempt_id)
        if incoming_position < current_position:
            return
        if (
            incoming_position == current_position
            and _observation_reviewable(current)
            and not reviewable
        ):
            # A stale report page must not turn a known completed attempt back
            # into a draft.  The reverse transition is the normal lifecycle.
            return
    mapping.local_id = parent_assessment_id
    mapping.external_revision = external_revision.strip()[:255]
    mapping.metadata_json = incoming


def latest_completed_moodle_submission_ids(
    rows: Iterable[MoodleAttemptRow],
) -> set[uuid.UUID]:
    """Select every submission belonging to the latest completed LMS attempt.

    Split imported Essay questions remain separate submissions, while an
    app-authored retry is a separate local Attempt. Both forms are grouped by
    the authoritative remote Moodle attempt id. Older attempts remain history;
    local ``Attempt.sequence`` and import order never decide recency.
    """

    attempts: dict[
        tuple[uuid.UUID, uuid.UUID],
        dict[tuple[int, int, str], list[MoodleAttemptRow]],
    ] = {}
    for row in rows:
        key = _completed_moodle_attempt_key(*row)
        if key is None:
            continue
        activity_id, principal_id, remote_attempt_id = key
        remote_position = _remote_attempt_position(remote_attempt_id)
        attempts.setdefault((activity_id, principal_id), {}).setdefault(remote_position, []).append(
            row
        )

    selected: set[uuid.UUID] = set()
    for remote_attempts in attempts.values():
        _latest_position, latest_rows = max(
            remote_attempts.items(),
            key=lambda item: _attempt_recency(item[0], item[1]),
        )
        imported_rows = [row for row in latest_rows if row[0].source == "MOODLE_IMPORT"]
        if imported_rows:
            # Once the reverse synchronization has materialized the exact LMS
            # answer, it is the canonical review/grade representation: it has
            # the durable remote mapping and response observations needed for
            # tamper verification. The app-authored Submission remains audit
            # history but must not create a duplicate queue row.
            imported_slots = {
                str(dict(row[0].external_receipt or {}).get("moodle_response_id", ""))
                for row in imported_rows
            }
            # A crawl may have read only the first of several responses so far.
            # Keep native sibling answers until their own exact slot is imported.
            native_siblings = [
                row
                for row in latest_rows
                if row[0].source != "MOODLE_IMPORT"
                and dict(row[1].integrity_policy or {}).get("moodle_quiz_root_attempt_id")
                and str(dict(row[0].external_receipt or {}).get("moodle_response_id", ""))
                not in imported_slots
            ]
            latest_rows = [*imported_rows, *native_siblings]
        selected.update(row[0].id for row in latest_rows)
    return selected


async def latest_reviewable_moodle_submission_ids(
    db: AsyncSession,
    rows: Iterable[MoodleAttemptRow],
) -> set[uuid.UUID]:
    """Select completions only when no newer remote Moodle attempt is active.

    A Moodle report row for an in-progress retry has no local Submission.  Its
    durable observation therefore participates after the ordinary
    completed-vs-completed selection and suppresses the older completion until
    the retry itself becomes terminal and is imported.
    """

    materialized_rows = list(rows)
    selected = latest_completed_moodle_submission_ids(materialized_rows)
    if not selected:
        return selected

    selected_scopes: dict[uuid.UUID, tuple[uuid.UUID, uuid.UUID, str]] = {}
    parent_ids: set[uuid.UUID] = set()
    principal_ids: set[uuid.UUID] = set()
    course_ids: set[uuid.UUID] = set()
    completed_attempt_ids: set[uuid.UUID] = set()
    for submission, attempt, assessment in materialized_rows:
        key = _completed_moodle_attempt_key(submission, attempt, assessment)
        if key is None:
            continue
        completed_attempt_ids.add(attempt.id)
        if submission.id not in selected:
            continue
        selected_scopes[submission.id] = key
        parent_ids.add(key[0])
        principal_ids.add(key[1])
        course_ids.add(assessment.course_id)
    if not parent_ids:
        return selected

    markers = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.external_type == MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE,
                    ExternalMapping.local_type == _OBSERVATION_LOCAL_TYPE,
                    ExternalMapping.local_id.in_(parent_ids),
                )
            )
        ).all()
    )
    latest_observations: dict[tuple[uuid.UUID, uuid.UUID], dict[str, object]] = {}
    for marker in markers:
        metadata = _observation_metadata(marker)
        if metadata is None:
            continue
        try:
            parent_id = uuid.UUID(str(metadata.get("assessment_id", "")))
            principal_id = uuid.UUID(str(metadata.get("principal_id", "")))
        except (TypeError, ValueError, AttributeError):
            continue
        if marker.local_id != parent_id or parent_id not in parent_ids:
            continue
        scope = parent_id, principal_id
        previous = latest_observations.get(scope)
        if previous is None or _observation_recency(metadata) > _observation_recency(previous):
            latest_observations[scope] = metadata

    # An app-native retry is a durable local Attempt before it has a
    # Submission or appears in a later Moodle report crawl. Suppress the older
    # completion immediately while the newer remote attempt is still active.
    local_attempt_rows = list(
        (
            await db.execute(
                select(Attempt, Assessment)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
                .where(
                    Attempt.principal_id.in_(principal_ids),
                    Assessment.course_id.in_(course_ids),
                    Attempt.state != AttemptState.VOID.value,
                )
            )
        ).all()
    )
    selected_scope_pairs = {
        (parent_id, principal_id)
        for parent_id, principal_id, _remote_attempt_id in selected_scopes.values()
    }
    for local_attempt, local_assessment in local_attempt_rows:
        remote_attempt_id = str(
            dict(local_attempt.integrity_policy or {}).get("moodle_attempt_id", "")
        ).strip()
        if not remote_attempt_id:
            continue
        scope = _parent_assessment_id(local_assessment), local_attempt.principal_id
        if scope not in selected_scope_pairs:
            continue
        synthetic: dict[str, object] = {
            "assessment_id": str(scope[0]),
            "principal_id": str(scope[1]),
            "remote_attempt_id": remote_attempt_id,
            "state": local_attempt.state,
            # A terminal Attempt without a Submission (for example Moodle was
            # finalized in another tab) still suppresses the old completion
            # until its historical answer is materialized.
            "reviewable": local_attempt.id in completed_attempt_ids,
        }
        previous = latest_observations.get(scope)
        if previous is None or _observation_recency(synthetic) > _observation_recency(previous):
            latest_observations[scope] = synthetic

    for submission_id, (parent_id, principal_id, remote_attempt_id) in selected_scopes.items():
        observation = latest_observations.get((parent_id, principal_id))
        if observation is None:
            continue
        observed_id = str(observation.get("remote_attempt_id", "")).strip()
        observed_position = _remote_attempt_position(observed_id)
        submitted_position = _remote_attempt_position(remote_attempt_id)
        if observed_position > submitted_position or (
            observed_position == submitted_position and not _observation_reviewable(observation)
        ):
            selected.discard(submission_id)
    return selected


def _attempt_recency(
    remote_position: tuple[int, int, str],
    rows: list[MoodleAttemptRow],
) -> tuple[int, int, datetime, str]:
    remote_kind, remote_number, remote_text = remote_position
    submitted_at = max(_aware(row[0].submitted_at) for row in rows)
    if remote_kind:
        # Moodle Quiz ids and Assignment attempt numbers are monotonic for one
        # activity/student. Prefer that authoritative identity because pages
        # without a parseable time receive a local import timestamp.
        return 1, remote_number, submitted_at, remote_text
    return 0, 0, submitted_at, remote_text


async def is_latest_completed_moodle_attempt(
    db: AsyncSession,
    *,
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> bool:
    """Return whether a Moodle-bound submission belongs to the current attempt.

    Genuinely local assessments remain outside this rule. The bounded query
    loads one student's completed submissions in one course, then applies the
    same remote grouping and active-retry observations used by the queue.
    """

    if _completed_moodle_attempt_key(submission, attempt, assessment) is None:
        return True
    rows = list(
        (
            await db.execute(
                select(Submission, Attempt, Assessment)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
                .where(
                    Attempt.principal_id == attempt.principal_id,
                    Attempt.state.in_(_COMPLETED_ATTEMPT_STATES),
                    Assessment.course_id == assessment.course_id,
                )
            )
        ).all()
    )
    return submission.id in await latest_reviewable_moodle_submission_ids(db, rows)


__all__ = [
    "MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE",
    "is_completed_moodle_import",
    "is_completed_moodle_submission",
    "is_latest_completed_moodle_attempt",
    "latest_completed_moodle_submission_ids",
    "latest_reviewable_moodle_submission_ids",
    "observe_moodle_attempt",
]
