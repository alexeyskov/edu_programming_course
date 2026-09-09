from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import math
import os
import re
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from typing import Any, Protocol

import httpx
from sqlalchemy import and_, case, delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    CredentialDecryptionError,
    decrypt_moodle_browser_state,
    decrypt_moodle_credential,
    encrypt_moodle_browser_state,
)
from app.db.base import utcnow
from app.integrations.errors import (
    IntegrationAttemptFinalized,
    IntegrationBusy,
    IntegrationError,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from app.integrations.moodle import CourseDiscovery, MoodleBridge
from app.integrations.moodle_artifact import (
    build_moodle_online_text_artifact,
    build_moodle_submission_artifact,
)
from app.integrations.moodle_modes import moodle_auth_mode, moodle_pluginless_transport
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.integrations.moodle_transport import (
    confirmed_moodle_activity_answer_transport,
    moodle_file_type_allowed,
    normalize_moodle_essay_answer_transport,
)
from app.models.attempts import Attempt, Snapshot, Submission, Workspace
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
    CourseSection,
)
from app.models.enums import AttemptState, AvailabilityTarget, CourseRole, SyncOutboxState
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    MoodleCredential,
)
from app.models.integration import ExternalMapping, LMSSubmissionFingerprint, SyncOutbox
from app.models.review import ReviewDecision
from app.models.tasks import (
    Assessment,
    AssessmentItem,
    AvailabilityRule,
    TaskBankItem,
    TaskVersion,
)
from app.services.common import (
    DomainError,
    canonical_hash,
    positive_decimal,
    sha256_text,
    validate_source_path,
)
from app.services.course_sync_state import course_sync_stale_before
from app.services.delivery_profile import resolve_assessment_workspace_delivery_profile
from app.services.moodle_attempt_selection import is_latest_completed_moodle_attempt
from app.services.moodle_history import (
    enqueue_historical_submission_imports,
    materialize_historical_submissions,
)
from app.services.moodle_materialization import materialize_moodle_activity_drafts
from app.services.moodle_quiz_runtime import (
    pinned_moodle_quiz_binding,
    resolve_moodle_assignment_context,
    resolve_moodle_quiz_context,
)
from app.services.moodle_source import (
    moodle_quiz_uses_latest_attempt_grade,
    moodle_source_confirmation_from_activity,
    moodle_source_is_confirmed,
)
from app.services.submission_origin import comparison_content, content_digests
from app.services.teacher_tokens import (
    teacher_membership_is_authorized,
    teacher_membership_revision,
    teacher_token_for_principal,
)
from app.services.workspace import (
    create_snapshot,
    enqueue_checkpoint,
    submit_attempt,
)

type SessionFactory = async_sessionmaker[AsyncSession]
type ClientFactory = Callable[[], httpx.AsyncClient]

_MAX_LMS_SECTIONS = 2_000
_MAX_LMS_ACTIVITIES = 5_000
_MAX_LMS_ACTIVITIES_BYTES = 128 * 1024
_TASK_MIRROR_BYTES = 4 * 1024 * 1024
_MANAGED_SUBMISSION_FILENAMES = frozenset(
    {"solution.c", "solution.cpp", "main.c", "main.cpp", "submission.zip"}
)


def _previous_managed_artifact_from_receipts(
    receipts: list[object],
    *,
    module: str,
    answer_transport: str,
    course_id: str,
    cmid: int,
) -> tuple[str | None, str | None]:
    """Select only a fully scoped durable browser delivery receipt."""

    for raw in receipts:
        if not isinstance(raw, dict):
            continue
        if raw.get("module") != module or raw.get("answer_transport") != answer_transport:
            continue
        remote = raw.get("receipt")
        if not isinstance(remote, dict):
            continue
        filename = remote.get("filename")
        digest = remote.get("sha256")
        if (
            remote.get("course_id") == course_id
            and remote.get("cmid") == cmid
            and filename in _MANAGED_SUBMISSION_FILENAMES
            and isinstance(digest, str)
            and re.fullmatch(r"[a-f0-9]{64}", digest)
            and raw.get("artifact_sha256") == digest
        ):
            return str(filename), digest
    return None, None


class MoodleAdapter(Protocol):
    async def store_checkpoint(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    async def push_grade(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

    async def upsert_task_definition(
        self, payload: dict[str, Any], idempotency_key: str
    ) -> dict[str, Any]: ...

    async def get_latest_checkpoint(
        self, *, course_id: str, user_id: str, attempt_ref: str
    ) -> dict[str, Any] | None: ...

    async def discover_course(
        self, external_id: str, actor_external_subject: str
    ) -> CourseDiscovery: ...

    async def discover_historical_submissions(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]: ...


class BridgeFactory(Protocol):
    def __call__(
        self,
        settings: Settings,
        connection: ConnectionTarget,
        client: httpx.AsyncClient,
    ) -> MoodleAdapter: ...


@dataclass(frozen=True, slots=True)
class ConnectionTarget:
    id: uuid.UUID
    base_url: str
    service_token: str | None
    mode: str = "BRIDGE"
    transport: str = "BRIDGE"
    principal_id: uuid.UUID | None = None
    browser_state: dict[str, Any] | None = None
    browser_credential_id: uuid.UUID | None = None
    browser_credential_revision: int | None = None
    browser_lease_owner: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimedOutboxEvent:
    id: uuid.UUID
    event_type: str
    aggregate_type: str
    aggregate_id: uuid.UUID
    connection_id: uuid.UUID
    course_id: uuid.UUID | None
    attempt_id: uuid.UUID | None
    idempotency_key: str
    payload: dict[str, Any]
    attempts: int
    locked_at: datetime


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    checkpoints_enqueued: int = 0
    attempts_submitted: int = 0
    courses_enqueued: int = 0


@dataclass(frozen=True, slots=True)
class RecoveredCheckpoint:
    attempt_id: uuid.UUID
    course_external_id: str
    user_external_id: str
    snapshot_ref: str
    snapshot_sha256: str
    reason: str
    event_chain_head: str
    epoch: int
    workspace_revision: int
    files: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _CheckpointDelivery:
    connection: ConnectionTarget
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _GradeDelivery:
    connection: ConnectionTarget
    payload: dict[str, Any]
    decision_id: uuid.UUID
    submission_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _CourseDelivery:
    connection: ConnectionTarget
    course_id: uuid.UUID
    external_course_id: str
    actor_external_subject: str


@dataclass(frozen=True, slots=True)
class _TaskVersionDelivery:
    connection: ConnectionTarget
    payload: dict[str, Any]
    course_id: uuid.UUID
    task_item_id: uuid.UUID
    task_version_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class _HistoryImportDelivery:
    connection: ConnectionTarget
    course_id: uuid.UUID
    assessment_id: uuid.UUID
    actor_external_subject: str
    payload: dict[str, Any]


type DeliveryContext = (
    _CheckpointDelivery
    | _GradeDelivery
    | _CourseDelivery
    | _TaskVersionDelivery
    | _HistoryImportDelivery
)


@dataclass(frozen=True, slots=True)
class _BrowserDeliveryResult:
    value: dict[str, Any] | CourseDiscovery
    storage_state: dict[str, Any]


class _BlockedDelivery(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def checkpoint_interval_seconds(
    attempt: Attempt,
    assessment: Assessment,
    *,
    now: datetime | None = None,
) -> int:
    """Return D/10, switching to D/20 for the final fifth, with 30s/15m clamps."""

    now = _as_utc(now or utcnow())
    duration = assessment.duration_seconds
    if not duration:
        return 300
    elapsed = max(0.0, (now - _as_utc(attempt.started_at)).total_seconds())
    divisor = 20 if elapsed >= duration * 0.8 else 10
    return max(30, min(900, int(duration / divisor)))


def retry_delay_seconds(attempt_number: int, settings: Settings) -> int:
    """Deterministic bounded exponential delay; idempotency makes random jitter optional."""

    base = max(1, int(getattr(settings, "sync_retry_base_seconds", 5)))
    maximum = max(base, int(getattr(settings, "sync_retry_max_seconds", 3600)))
    exponent = max(0, min(20, attempt_number - 1))
    return min(maximum, base * (2**exponent))


def validate_checkpoint_manifest(
    manifest_json: str,
    expected_sha256: str,
    *,
    max_bytes: int,
    max_files: int = 128,
) -> list[dict[str, Any]]:
    """Validate the complete checkpoint, including envelope and every file content hash."""

    if not isinstance(manifest_json, str):
        raise IntegrationProtocolError("Checkpoint manifest must be JSON text")
    try:
        encoded = manifest_json.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise IntegrationProtocolError("Checkpoint manifest is not valid UTF-8") from exc
    if not encoded or len(encoded) > max_bytes:
        raise IntegrationProtocolError("Checkpoint manifest exceeds the configured size limit")
    if (
        not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in expected_sha256)
    ):
        raise IntegrationProtocolError("Checkpoint SHA-256 is invalid")
    actual_hash = hashlib.sha256(encoded).hexdigest()
    if not hmac.compare_digest(actual_hash, expected_sha256.lower()):
        raise IntegrationProtocolError("Checkpoint manifest SHA-256 does not match")
    try:
        manifest = json.loads(
            manifest_json,
            parse_constant=lambda value: (_raise_invalid_json_constant(value)),
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise IntegrationProtocolError("Checkpoint manifest is invalid JSON") from exc
    if not isinstance(manifest, list) or len(manifest) > max_files:
        raise IntegrationProtocolError("Checkpoint manifest has an invalid file list")

    seen_paths: set[str] = set()
    seen_ids: set[str] = set()
    validated: list[dict[str, Any]] = []
    source_bytes = 0
    for raw_file in manifest:
        if not isinstance(raw_file, dict):
            raise IntegrationProtocolError("Checkpoint file entry has an invalid shape")
        path_value = raw_file.get("path")
        content = raw_file.get("content")
        content_hash = raw_file.get("content_hash")
        if not isinstance(path_value, str) or not isinstance(content, str):
            raise IntegrationProtocolError("Checkpoint file path/content is invalid")
        try:
            path = validate_source_path(path_value)
        except Exception as exc:
            raise IntegrationProtocolError("Checkpoint contains an unsafe source path") from exc
        if path in seen_paths:
            raise IntegrationProtocolError("Checkpoint contains duplicate source paths")
        seen_paths.add(path)
        file_id = raw_file.get("id")
        if file_id is not None:
            try:
                normalized_id = str(uuid.UUID(str(file_id)))
            except (TypeError, ValueError) as exc:
                raise IntegrationProtocolError("Checkpoint file id is invalid") from exc
            if normalized_id in seen_ids:
                raise IntegrationProtocolError("Checkpoint contains duplicate file ids")
            seen_ids.add(normalized_id)
        try:
            source_bytes += len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise IntegrationProtocolError("Checkpoint source is not valid UTF-8") from exc
        if source_bytes > max_bytes:
            raise IntegrationProtocolError("Checkpoint source exceeds the configured size limit")
        if not isinstance(content_hash, str) or not hmac.compare_digest(
            sha256_text(content), content_hash.lower()
        ):
            raise IntegrationProtocolError("Checkpoint file content hash does not match")
        validated.append(dict(raw_file, path=path, content_hash=content_hash.lower()))
    return validated


def _raise_invalid_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


async def maintain_attempts(
    db: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> SchedulerResult:
    """Create due checkpoints and submit the authoritative last revision at deadline.

    Baseline policy intentionally auto-submits every still-active attempt at its server deadline,
    even when an imported activity did not expose an autosubmit flag. This prevents the scheduler
    from discarding acknowledged work; a later policy can only replace this with an equally durable
    terminal-state rule.
    """

    now = _as_utc(now or utcnow())
    attempt_ids = list(
        (
            await db.scalars(
                select(Attempt.id)
                .where(Attempt.state == AttemptState.ACTIVE.value)
                .order_by(Attempt.deadline_at.asc().nulls_last(), Attempt.started_at, Attempt.id)
            )
        ).all()
    )
    checkpoints = 0
    submitted = 0
    for attempt_id in attempt_ids:
        attempt = await db.scalar(
            select(Attempt).where(Attempt.id == attempt_id).with_for_update(skip_locked=True)
        )
        if attempt is None or attempt.state != AttemptState.ACTIVE.value:
            continue
        workspace = await db.scalar(
            select(Workspace).where(Workspace.attempt_id == attempt.id).with_for_update()
        )
        assessment = await db.get(Assessment, attempt.assessment_id)
        if workspace is None or assessment is None:
            continue

        deadline = _as_utc(attempt.deadline_at) if attempt.deadline_at is not None else None
        if deadline is not None and now >= deadline:
            # Read revision only after Attempt -> Workspace locks; it is the last accepted revision.
            # SQLite drops timezone metadata; normalize the in-memory value for workspace service.
            attempt.deadline_at = deadline
            await submit_attempt(
                db,
                attempt_id=attempt.id,
                principal_id=attempt.principal_id,
                expected_revision=workspace.current_revision,
                source="DEADLINE",
            )
            submitted += 1
            checkpoints += 1
            continue

        final_minute = bool(
            deadline is not None and timedelta(0) < deadline - now <= timedelta(seconds=60)
        )
        reason = "FINAL_MINUTE" if final_minute else "PERIODIC"
        last_event = await db.scalar(
            select(SyncOutbox)
            .where(
                SyncOutbox.attempt_id == attempt.id,
                SyncOutbox.event_type == "attempt.checkpoint",
            )
            .order_by(SyncOutbox.created_at.desc())
        )
        interval = checkpoint_interval_seconds(attempt, assessment, now=now)
        is_due = (
            last_event is None or (now - _as_utc(last_event.created_at)).total_seconds() >= interval
        )
        checkpoint_payloads = list(
            (
                await db.scalars(
                    select(SyncOutbox.payload).where(
                        SyncOutbox.attempt_id == attempt.id,
                        SyncOutbox.event_type == "attempt.checkpoint",
                    )
                )
            ).all()
        )
        already_final = any(
            isinstance(payload, dict) and payload.get("reason") == "FINAL_MINUTE"
            for payload in checkpoint_payloads
        )
        if not is_due and not (final_minute and not already_final):
            continue
        snapshot = await create_snapshot(db, workspace, reason)
        heartbeat_generation = (
            None if reason == "FINAL_MINUTE" else (int(now.timestamp()) // interval) * interval
        )
        generation = reason if reason == "FINAL_MINUTE" else f"P{heartbeat_generation:x}"
        idempotency_key = f"checkpoint:{attempt.id}:{snapshot.id}:{generation}"[:100]
        before = await db.scalar(
            select(SyncOutbox.id).where(SyncOutbox.idempotency_key == idempotency_key)
        )
        await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason=reason,
            heartbeat_generation=heartbeat_generation,
        )
        if before is None:
            checkpoints += 1
    return SchedulerResult(checkpoints_enqueued=checkpoints, attempts_submitted=submitted)


async def enqueue_course_synchronizations(
    db: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> int:
    now = now or utcnow()
    interval = max(60, int(getattr(settings, "sync_course_interval_seconds", 900)))
    bucket = int(now.timestamp()) // interval
    stale_before = course_sync_stale_before(settings, now)
    courses = list(
        (
            await db.scalars(
                select(Course)
                .join(LMSConnection, LMSConnection.id == Course.connection_id)
                .where(
                    Course.archived_at.is_(None),
                    Course.catalog_enabled.is_(True),
                    LMSConnection.enabled.is_(True),
                )
                .order_by(Course.id)
            )
        ).all()
    )
    enqueued = 0
    for course in courses:
        outstanding = await db.scalar(
            select(SyncOutbox.id).where(
                SyncOutbox.course_id == course.id,
                SyncOutbox.event_type == "course.sync",
                SyncOutbox.state.in_(
                    [
                        SyncOutboxState.PENDING.value,
                        SyncOutboxState.PROCESSING.value,
                        SyncOutboxState.RETRY.value,
                    ]
                ),
            )
        )
        if outstanding is not None:
            continue
        # ``POST /courses/{id}/sync`` performs discovery in the request so it
        # has no worker-owned outbox lease.  Its durable course marker is the
        # coordination point: do not overwrite a live foreground refresh with
        # PENDING.  A marker older than the external-I/O bound is deliberately
        # recoverable below, so a cancelled API request cannot wedge a course.
        if course.sync_status == "SYNCING" and _as_utc(course.updated_at) > _as_utc(stale_before):
            continue
        recent_row = (
            await db.execute(
                select(SyncOutbox.delivered_at, SyncOutbox.created_at)
                .where(
                    SyncOutbox.course_id == course.id,
                    SyncOutbox.event_type == "course.sync",
                    SyncOutbox.state == SyncOutboxState.DELIVERED.value,
                )
                .order_by(SyncOutbox.delivered_at.desc(), SyncOutbox.created_at.desc())
                .limit(1)
            )
        ).first()
        recent = (recent_row[0] or recent_row[1]) if recent_row is not None else None
        if (
            course.sync_status == "CURRENT"
            and recent is not None
            and _as_utc(recent) > now - timedelta(seconds=interval)
        ):
            continue
        recovering_stale_marker = course.sync_status in {"PENDING", "SYNCING"} and _as_utc(
            course.updated_at
        ) <= _as_utc(stale_before)
        key_kind = "course-sync-recovery" if recovering_stale_marker else "course-sync"
        key = f"{key_kind}:{course.id}:{bucket}"[:100]
        if await db.scalar(select(SyncOutbox.id).where(SyncOutbox.idempotency_key == key)):
            continue
        # Serialize concurrent scheduler iterations without preventing stale
        # PENDING/SYNCING recovery.  A second scheduler observing the fresh
        # PENDING written here must not create another bucket row.
        claimed = await db.execute(
            update(Course)
            .where(
                Course.id == course.id,
                or_(
                    Course.sync_status.in_(["CURRENT", "FAILED"]),
                    and_(
                        Course.sync_status.in_(["PENDING", "SYNCING"]),
                        Course.updated_at <= stale_before,
                    ),
                ),
            )
            .values(sync_status="PENDING", updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if claimed.rowcount != 1:  # type: ignore[attr-defined]
            continue
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                event_type="course.sync",
                aggregate_type="Course",
                aggregate_id=course.id,
                idempotency_key=key,
                payload={"course_id": course.external_id, "schedule_bucket": bucket},
            )
        )
        enqueued += 1
    await db.flush()
    return enqueued


async def run_scheduler_iteration(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
    include_course_sync: bool = True,
) -> SchedulerResult:
    """Run one idempotent scheduler transaction; it never contacts an external service."""

    now = now or utcnow()
    async with session_factory() as db, db.begin():
        attempt_result = await maintain_attempts(db, settings, now=now)
        courses = (
            await enqueue_course_synchronizations(db, settings, now=now)
            if include_course_sync
            else 0
        )
        return SchedulerResult(
            checkpoints_enqueued=attempt_result.checkpoints_enqueued,
            attempts_submitted=attempt_result.attempts_submitted,
            courses_enqueued=courses,
        )


async def claim_next_outbox_event(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    now: datetime | None = None,
    terminal_checkpoints_only: bool = False,
) -> ClaimedOutboxEvent | None:
    """Atomically claim one due row using PostgreSQL ``FOR UPDATE SKIP LOCKED``."""

    now = now or utcnow()
    lease_seconds = max(5, int(getattr(settings, "sync_lease_seconds", 120)))
    stale_before = now - timedelta(seconds=lease_seconds)
    async with session_factory() as db, db.begin():
        due = or_(
            and_(
                SyncOutbox.state.in_(
                    [SyncOutboxState.PENDING.value, SyncOutboxState.RETRY.value]
                ),
                SyncOutbox.next_attempt_at <= now,
            ),
            and_(
                SyncOutbox.state == SyncOutboxState.PROCESSING.value,
                or_(
                    SyncOutbox.locked_at.is_(None),
                    SyncOutbox.locked_at <= stale_before,
                ),
            ),
        )
        filters = [due]
        if terminal_checkpoints_only:
            filters.extend(
                [
                    SyncOutbox.event_type == "attempt.checkpoint",
                    SyncOutbox.payload["reason"].as_string().in_(["SUBMISSION", "DEADLINE"]),
                ]
            )
        row = await db.scalar(
            select(SyncOutbox)
            .where(*filters)
            # Student answer checkpoints are latency-sensitive.  Course and
            # history crawls can take minutes and must not sit ahead of a
            # submission merely because they were enqueued first.  The final
            # checkpoint also jumps ahead of older periodic saves: those are
            # immediately superseded once the final snapshot exists.
            .order_by(
                case(
                    (
                        and_(
                            SyncOutbox.event_type == "attempt.checkpoint",
                            SyncOutbox.payload["reason"]
                            .as_string()
                            .in_(["SUBMISSION", "DEADLINE"]),
                        ),
                        0,
                    ),
                    (SyncOutbox.event_type == "attempt.checkpoint", 1),
                    (SyncOutbox.event_type == "review.decision", 2),
                    (SyncOutbox.event_type == "task.version", 3),
                    (SyncOutbox.event_type == "course.sync", 4),
                    (
                        and_(
                            SyncOutbox.event_type == "moodle.history.import",
                            SyncOutbox.payload["priority_only"].as_boolean().is_(True),
                        ),
                        5,
                    ),
                    # Exhaustive history is deliberately last.  Its priority
                    # pass has already imported attempts awaiting review.
                    else_=6,
                ),
                SyncOutbox.next_attempt_at,
                SyncOutbox.created_at,
                SyncOutbox.id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if row is None:
            return None
        if row.event_type == "review.decision" and row.aggregate_type == "ReviewDecision":
            decision = await db.get(ReviewDecision, row.aggregate_id)
            submission = (
                await db.scalar(
                    select(Submission)
                    .where(Submission.id == decision.submission_id)
                    .with_for_update()
                )
                if decision is not None
                else None
            )
            if submission is not None:
                latest = await db.scalar(
                    select(ReviewDecision)
                    .where(ReviewDecision.submission_id == submission.id)
                    .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
                )
                if latest is not None and latest.id == decision.id and decision.status == "APPLIED":
                    submission.lms_export_state = "PROCESSING"
        row.state = SyncOutboxState.PROCESSING.value
        row.attempts += 1
        row.last_attempt_at = now
        row.locked_at = now
        row.last_error = ""
        if row.event_type == "course.sync" and row.course_id is not None:
            course = await db.get(Course, row.course_id)
            if course is not None:
                course.sync_status = "SYNCING"
        await db.flush()
        return ClaimedOutboxEvent(
            id=row.id,
            event_type=row.event_type,
            aggregate_type=row.aggregate_type,
            aggregate_id=row.aggregate_id,
            connection_id=row.connection_id,
            course_id=row.course_id,
            attempt_id=row.attempt_id,
            idempotency_key=row.idempotency_key,
            payload=dict(row.payload or {}),
            attempts=row.attempts,
            locked_at=row.locked_at,
        )


async def process_outbox_once(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    bridge_factory: BridgeFactory | None = None,
    client: httpx.AsyncClient | None = None,
    client_factory: ClientFactory | None = None,
    now: datetime | None = None,
    terminal_checkpoints_only: bool = False,
) -> bool:
    """Claim, commit, call Moodle, then persist the result in a fresh transaction."""

    claim = await claim_next_outbox_event(
        session_factory,
        settings,
        now=now,
        terminal_checkpoints_only=terminal_checkpoints_only,
    )
    if claim is None:
        return False
    try:
        context = await _prepare_delivery(session_factory, settings, claim)
    except _BlockedDelivery as exc:
        await _finish_blocked(session_factory, claim, f"{exc.code}: {exc}")
        return True
    except IntegrationError as exc:
        await _finish_failure(session_factory, settings, claim, exc, now=now)
        return True
    except Exception as exc:  # malformed local rows must not crash the durable worker loop
        await _finish_failure(session_factory, settings, claim, exc, now=now)
        return True

    owns_client = client is None
    active_client = client
    factory = bridge_factory or default_bridge_factory
    try:
        try:
            if active_client is None:
                active_client = (
                    client_factory()
                    if client_factory
                    else httpx.AsyncClient(follow_redirects=False, trust_env=False)
                )
            bridge = factory(settings, context.connection, active_client)
            result = await _deliver(bridge, context, claim.idempotency_key)
        finally:
            if owns_client and active_client is not None:
                await _close_client_best_effort(active_client)
    except IntegrationAttemptFinalized:
        await _release_browser_context_best_effort(session_factory, context)
        await _finish_moodle_attempt_finalized(session_factory, claim, now=now)
        return True
    except IntegrationError as exc:
        if isinstance(exc, MoodleAuthenticationError):
            await _invalidate_browser_context_best_effort(session_factory, context)
        else:
            await _release_browser_context_best_effort(session_factory, context)
        await _finish_failure(session_factory, settings, claim, exc, now=now)
        return True
    except Exception as exc:  # only class name is persisted for unexpected failures
        await _release_browser_context_best_effort(session_factory, context)
        await _finish_failure(session_factory, settings, claim, exc, now=now)
        return True
    except BaseException:
        await _release_browser_context_best_effort(session_factory, context)
        raise

    try:
        finished = await _finish_delivered(session_factory, settings, claim, context, result)
        if not finished:
            await _release_browser_context_best_effort(session_factory, context)
    except IntegrationError as exc:
        await _release_browser_context_best_effort(session_factory, context)
        await _finish_failure(session_factory, settings, claim, exc, now=now)
    except Exception as exc:
        await _release_browser_context_best_effort(session_factory, context)
        await _finish_failure(session_factory, settings, claim, exc, now=now)
    except BaseException:
        await _release_browser_context_best_effort(session_factory, context)
        raise
    return True


async def recover_attempt_checkpoint(
    session_factory: SessionFactory,
    settings: Settings,
    *,
    attempt_id: uuid.UUID,
    bridge_factory: BridgeFactory | None = None,
    client: httpx.AsyncClient | None = None,
    client_factory: ClientFactory | None = None,
) -> RecoveredCheckpoint | None:
    """Fetch a recovery candidate without mutating the local authoritative workspace."""

    async with session_factory() as db:
        attempt = await db.get(Attempt, attempt_id)
        if attempt is None:
            raise IntegrationProtocolError("Recovery attempt does not exist")
        assessment = await db.get(Assessment, attempt.assessment_id)
        principal = await db.get(ExternalPrincipal, attempt.principal_id)
        if assessment is None or principal is None:
            raise IntegrationProtocolError("Recovery context is incomplete")
        course = await db.get(Course, assessment.course_id)
        connection = await db.get(LMSConnection, course.connection_id) if course else None
        if course is None or connection is None:
            raise IntegrationProtocolError("Recovery course connection is missing")
        target = _connection_target(settings, connection)
        if target.mode == "PLUGINLESS":
            if target.transport == "PLAYWRIGHT":
                raise IntegrationProtocolError(
                    "Moodle Quiz Essay is an export target; recovery uses local snapshots"
                )
            try:
                target = await _principal_credential_target(db, settings, target, principal.id)
            except _BlockedDelivery as exc:
                raise IntegrationProtocolError("Moodle reauthentication is required") from exc
        course_external_id = course.external_id
        user_external_id = principal.external_subject

    owns_client = client is None
    active_client = client or (
        client_factory()
        if client_factory
        else httpx.AsyncClient(follow_redirects=False, trust_env=False)
    )
    try:
        bridge = (bridge_factory or default_bridge_factory)(settings, target, active_client)
        checkpoint = await bridge.get_latest_checkpoint(
            course_id=course_external_id,
            user_id=user_external_id,
            attempt_ref=str(attempt_id),
        )
    finally:
        if owns_client:
            await active_client.aclose()
    if checkpoint is None:
        return None

    _require_equal(checkpoint, ("courseid", "course_id"), course_external_id, "course")
    _require_equal(checkpoint, ("userid", "user_id"), user_external_id, "user")
    _require_equal(checkpoint, ("attemptref", "attempt_ref"), str(attempt_id), "attempt")
    manifest_json = _required_string(checkpoint, "manifestjson", "manifest_json")
    snapshot_hash = _required_string(checkpoint, "snapshotsha256", "snapshot_sha256")
    max_bytes = int(getattr(settings, "sync_checkpoint_max_bytes", 16 * 1024 * 1024))
    max_files = int(getattr(settings, "sync_checkpoint_max_files", 128))
    files = validate_checkpoint_manifest(
        manifest_json,
        snapshot_hash,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    revision = _required_nonnegative_integer(
        checkpoint,
        ("workspacerevision", "workspace_revision"),
        "workspace revision",
    )
    epoch = _required_nonnegative_integer(checkpoint, ("epoch",), "attempt epoch")
    if epoch < 1:
        raise IntegrationProtocolError("Checkpoint attempt epoch is invalid")
    event_chain_head = _required_hash(
        checkpoint,
        ("eventchainhead", "event_chain_head"),
        "event chain head",
        allow_empty=True,
    )
    snapshot_ref = _required_string(checkpoint, "snapshotref", "snapshot_ref")
    try:
        snapshot_id = uuid.UUID(snapshot_ref)
    except ValueError as exc:
        raise IntegrationProtocolError("Checkpoint snapshot reference is invalid") from exc
    async with session_factory() as db:
        current_attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        snapshot = await db.get(Snapshot, snapshot_id)
        if (
            current_attempt is None
            or workspace is None
            or snapshot is None
            or snapshot.workspace_id != workspace.id
        ):
            raise IntegrationProtocolError("Checkpoint snapshot ownership does not match")
        if current_attempt.epoch != epoch:
            raise IntegrationProtocolError("Checkpoint attempt epoch does not match")
        if snapshot.revision != revision:
            raise IntegrationProtocolError("Checkpoint workspace revision does not match")
        if not hmac.compare_digest(snapshot.manifest_hash.lower(), snapshot_hash.lower()):
            raise IntegrationProtocolError("Checkpoint snapshot hash does not match")
        if not hmac.compare_digest(snapshot.event_chain_head.lower(), event_chain_head):
            raise IntegrationProtocolError("Checkpoint event chain head does not match")
        if (
            canonical_hash(files) != snapshot.manifest_hash.lower()
            or canonical_hash(snapshot.files) != snapshot.manifest_hash.lower()
        ):
            raise IntegrationProtocolError("Checkpoint snapshot manifest does not match")
    return RecoveredCheckpoint(
        attempt_id=attempt_id,
        course_external_id=course_external_id,
        user_external_id=user_external_id,
        snapshot_ref=snapshot_ref,
        snapshot_sha256=snapshot_hash.lower(),
        reason=str(checkpoint.get("reason", "RECOVERY")),
        event_chain_head=event_chain_head,
        epoch=epoch,
        workspace_revision=revision,
        files=tuple(files),
    )


# The longer name is useful at call sites and preserves a connector-neutral service surface.
recover_latest_checkpoint = recover_attempt_checkpoint


class _MoodleBrowserAdapter:
    """Adapt typed browser operations to the durable outbox delivery surface."""

    def __init__(
        self,
        settings: Settings,
        connection: ConnectionTarget,
        client: httpx.AsyncClient,
    ) -> None:
        if connection.browser_state is None:
            raise IntegrationProtocolError("Moodle browser session state is missing")
        from app.integrations.moodle_browser import MoodleBrowserClient

        self.browser = MoodleBrowserClient(
            settings,
            client,
            base_url=connection.base_url,
            storage_state=connection.browser_state,
        )

    async def store_checkpoint(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> _BrowserDeliveryResult:
        from app.integrations.moodle_browser import MoodleBrowserQuizEssayArtifact

        filename = payload.get("artifact_filename")
        content = payload.get("artifact_bytes")
        digest = payload.get("artifact_sha256")
        size = payload.get("artifact_size")
        if (
            not isinstance(filename, str)
            or not isinstance(content, bytes)
            or not isinstance(digest, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or len(content) != size
            or not hmac.compare_digest(hashlib.sha256(content).hexdigest(), digest.lower())
        ):
            raise IntegrationProtocolError("Moodle Quiz Essay artifact is inconsistent")
        module = payload.get("module")
        artifact = MoodleBrowserQuizEssayArtifact(filename=filename, content=content)
        if module == "assign":
            result = await self.browser.sync_assignment_submission(
                str(payload.get("course_id", "")),
                payload.get("cmid"),
                artifact,
                answer_transport=payload.get("answer_transport"),
                finalize=payload.get("finalize"),
                requires_submission_statement=payload.get("requires_submission_statement", False),
                submission_drafts=payload.get("submission_drafts"),
                max_submission_bytes_inherited=payload.get("max_submission_bytes_inherited", False),
                previous_managed_filename=payload.get("previous_managed_filename"),
                previous_managed_sha256=payload.get("previous_managed_sha256"),
                idempotency_key=idempotency_key,
            )
        elif module == "quiz":
            result = await self.browser.sync_quiz_essay(
                str(payload.get("course_id", "")),
                payload.get("cmid"),
                artifact,
                answer_transport=payload.get("answer_transport"),
                finalize=payload.get("finalize"),
                expected_attempt_id=payload.get("expected_attempt_id"),
                expected_question_slot=payload.get("expected_question_slot"),
                previous_managed_filename=payload.get("previous_managed_filename"),
                previous_managed_sha256=payload.get("previous_managed_sha256"),
                idempotency_key=idempotency_key,
            )
        else:
            raise IntegrationProtocolError("Moodle checkpoint module is invalid")
        return _BrowserDeliveryResult(
            value={
                "status": result.status,
                "module": module,
                "answer_transport": payload.get("answer_transport"),
                "artifact_sha256": digest.lower(),
                "receipt": asdict(result.receipt),
            },
            storage_state=result.storage_state,
        )

    async def push_grade(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> _BrowserDeliveryResult:
        result = await self.browser.push_grade(payload, idempotency_key)
        return _BrowserDeliveryResult(
            value={"status": result.status, "receipt": result.receipt},
            storage_state=result.storage_state,
        )

    async def discover_course(
        self,
        external_id: str,
        actor_external_subject: str,
    ) -> _BrowserDeliveryResult:
        result = await self.browser.discover_course(external_id, actor_external_subject)
        return _BrowserDeliveryResult(
            value=result.discovery,
            storage_state=result.storage_state,
        )

    async def discover_historical_submissions(
        self,
        payload: dict[str, Any],
    ) -> _BrowserDeliveryResult:
        result = await self.browser.discover_historical_submissions(
            course_id=str(payload.get("course_id", "")),
            actor_external_subject=str(payload.get("actor_external_subject", "")),
            module=str(payload.get("module", "")),
            cmid=payload.get("cmid"),
            cursor=str(payload.get("cursor", "0:0")),
            limit=payload.get("limit", 10),
            priority_only=payload.get("priority_only", False),
        )
        return _BrowserDeliveryResult(
            value={
                "course_id": result.course_id,
                "activity": {"module": result.module, "cmid": result.cmid},
                "items": list(result.items),
                "next_cursor": result.next_cursor,
                "complete": result.complete,
                "warnings": list(result.warnings),
            },
            storage_state=result.storage_state,
        )

    async def upsert_task_definition(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        raise IntegrationProtocolError("Moodle browser task mirroring is not supported")

    async def get_latest_checkpoint(
        self,
        *,
        course_id: str,
        user_id: str,
        attempt_ref: str,
    ) -> dict[str, Any] | None:
        raise IntegrationProtocolError("Moodle browser checkpoint recovery is not supported")


def default_bridge_factory(
    settings: Settings,
    connection: ConnectionTarget,
    client: httpx.AsyncClient,
) -> MoodleAdapter:
    if connection.mode == "PLUGINLESS":
        if connection.transport == "PLAYWRIGHT":
            return _MoodleBrowserAdapter(settings, connection, client)  # type: ignore[return-value]
        from app.integrations.moodle_standard import MoodleStandardClient

        return MoodleStandardClient(
            settings,
            client,
            base_url=connection.base_url,
            service_token=connection.service_token,
        )
    return MoodleBridge(
        settings,
        client,
        base_url=connection.base_url,
        service_token=connection.service_token,
    )


async def _prepare_delivery(
    session_factory: SessionFactory,
    settings: Settings,
    claim: ClaimedOutboxEvent,
) -> DeliveryContext:
    async with session_factory() as db:
        connection = await db.get(LMSConnection, claim.connection_id)
        if connection is None or not connection.enabled:
            raise _BlockedDelivery("CONNECTION_DISABLED", "LMS connection is unavailable")
        target = _connection_target(settings, connection)
        if claim.event_type == "attempt.checkpoint":
            return await _prepare_checkpoint(db, settings, claim, target)
        if claim.event_type == "review.decision":
            return await _prepare_grade(db, settings, claim, target)
        if claim.event_type == "task.version":
            return await _prepare_task_version(db, claim, target)
        if claim.event_type == "course.sync":
            return await _prepare_course_sync(db, settings, claim, target)
        if claim.event_type == "moodle.history.import":
            return await _prepare_history_import(db, settings, claim, target)
        raise _BlockedDelivery("UNSUPPORTED_EVENT", "Outbox event type is not supported")


async def _prepare_checkpoint(
    db: AsyncSession,
    settings: Settings,
    claim: ClaimedOutboxEvent,
    connection: ConnectionTarget,
) -> _CheckpointDelivery:
    if claim.attempt_id is None or claim.aggregate_type != "Snapshot":
        raise _BlockedDelivery("INVALID_CHECKPOINT", "Checkpoint references are incomplete")
    attempt = await db.get(Attempt, claim.attempt_id)
    snapshot = await db.get(Snapshot, claim.aggregate_id)
    if attempt is None or snapshot is None:
        raise _BlockedDelivery("MISSING_CHECKPOINT", "Checkpoint source no longer exists")
    if attempt.submission_source == "MOODLE_FINALIZED":
        raise _BlockedDelivery(
            "MOODLE_ATTEMPT_FINALIZED",
            "Moodle attempt was already finalized outside the application",
        )
    assessment = await db.get(Assessment, attempt.assessment_id)
    principal = await db.get(ExternalPrincipal, attempt.principal_id)
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    course = await db.get(Course, assessment.course_id) if assessment else None
    if (
        assessment is None
        or principal is None
        or workspace is None
        or course is None
        or snapshot.workspace_id != workspace.id
        or course.connection_id != claim.connection_id
        or claim.course_id != course.id
    ):
        raise _BlockedDelivery("CHECKPOINT_CONTEXT_MISMATCH", "Checkpoint ownership is invalid")
    payload = dict(claim.payload)
    expected_values = {
        "course_id": course.external_id,
        "user_id": principal.external_subject,
        "attempt_ref": str(attempt.id),
        "snapshot_ref": str(snapshot.id),
    }
    for key, expected in expected_values.items():
        if str(payload.get(key, "")) != str(expected):
            raise _BlockedDelivery("CHECKPOINT_CONTEXT_MISMATCH", f"Checkpoint {key} is invalid")
    revision = payload.get("workspace_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision != snapshot.revision:
        raise _BlockedDelivery(
            "CHECKPOINT_CONTEXT_MISMATCH", "Checkpoint workspace revision is invalid"
        )
    epoch = payload.get("epoch")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch != attempt.epoch or epoch < 1:
        raise _BlockedDelivery("CHECKPOINT_CONTEXT_MISMATCH", "Checkpoint attempt epoch is invalid")
    chain_head = payload.get("event_chain_head")
    if not _is_sha256(chain_head, allow_empty=True) or not hmac.compare_digest(
        str(chain_head).lower(), snapshot.event_chain_head.lower()
    ):
        raise _BlockedDelivery(
            "CHECKPOINT_CONTEXT_MISMATCH", "Checkpoint event chain head is invalid"
        )
    reason = payload.get("reason")
    heartbeat_generation = payload.get("heartbeat_generation")
    if reason not in {
        "ATTEMPT_STARTED",
        "PERIODIC",
        "FINAL_MINUTE",
        "SUBMISSION",
        "DEADLINE",
    }:
        raise _BlockedDelivery("INVALID_CHECKPOINT", "Checkpoint reason is invalid")
    manifest_json = payload.get("manifest_json")
    manifest_hash = payload.get("snapshot_sha256")
    max_bytes = int(getattr(settings, "sync_checkpoint_max_bytes", 16 * 1024 * 1024))
    max_files = int(getattr(settings, "sync_checkpoint_max_files", 128))
    files = validate_checkpoint_manifest(
        manifest_json,
        manifest_hash,
        max_bytes=max_bytes,
        max_files=max_files,
    )
    if (
        not hmac.compare_digest(str(manifest_hash).lower(), snapshot.manifest_hash.lower())
        or canonical_hash(files) != snapshot.manifest_hash
        or canonical_hash(snapshot.files) != snapshot.manifest_hash
    ):
        raise _BlockedDelivery("CHECKPOINT_SNAPSHOT_MISMATCH", "Local snapshot hash is invalid")
    if connection.mode == "PLUGINLESS":
        if connection.transport != "PLAYWRIGHT":
            raise _BlockedDelivery(
                "PLUGINLESS_CHECKPOINT_UNSUPPORTED",
                "Moodle checkpoint upload requires the Playwright transport",
            )
        if reason not in {"SUBMISSION", "DEADLINE"}:
            sibling_payloads = list(
                (
                    await db.scalars(
                        select(SyncOutbox.payload).where(
                            SyncOutbox.attempt_id == attempt.id,
                            SyncOutbox.event_type == "attempt.checkpoint",
                            SyncOutbox.id != claim.id,
                        )
                    )
                ).all()
            )
            superseded = False
            for sibling in sibling_payloads:
                if not isinstance(sibling, dict):
                    continue
                sibling_revision = sibling.get("workspace_revision")
                sibling_reason = sibling.get("reason")
                sibling_generation = sibling.get("heartbeat_generation")
                if isinstance(sibling_revision, bool) or not isinstance(sibling_revision, int):
                    continue
                if (
                    sibling_revision > revision
                    or (
                        sibling_revision >= revision
                        and sibling_reason in {"SUBMISSION", "DEADLINE"}
                    )
                    or (
                        reason == "PERIODIC"
                        and sibling_reason == "PERIODIC"
                        and sibling_revision == revision
                        and not isinstance(sibling_generation, bool)
                        and isinstance(sibling_generation, int)
                        and (
                            isinstance(heartbeat_generation, bool)
                            or not isinstance(heartbeat_generation, int)
                            or sibling_generation > heartbeat_generation
                        )
                    )
                ):
                    superseded = True
                    break
            if superseded:
                raise _BlockedDelivery(
                    "SUPERSEDED",
                    "A newer Moodle Quiz Essay checkpoint already exists",
                )
        delivery_resolution = await resolve_assessment_workspace_delivery_profile(
            db,
            assessment.id,
        )
        mapping = delivery_resolution.mapping
        runtime_context = None
        if mapping is None:
            try:
                runtime_context = await resolve_moodle_quiz_context(db, assessment.id)
                if runtime_context is None:
                    runtime_context = await resolve_moodle_assignment_context(db, assessment.id)
            except DomainError as exc:
                raise _BlockedDelivery(exc.code, exc.message) from exc
            if runtime_context is not None:
                mapping = runtime_context.mapping
        if mapping is None:
            raise _BlockedDelivery(
                "MOODLE_SUBMISSION_MAPPING_REQUIRED",
                "Exactly one current, confirmed Moodle Quiz Essay or Assignment mapping "
                "on the course connection is required",
            )
        metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
        module = _moodle_activity_mapping_module(mapping)
        activity = metadata.get("activity")
        activity = activity if isinstance(activity, dict) else {}
        external_course_id = str(_positive_integer(course.external_id, "Moodle course id"))
        external_cmid = _positive_integer(
            metadata.get("cmid", mapping.external_id), "Moodle activity cmid"
        )
        answer_transport = _moodle_activity_answer_transport(mapping)
        pinned_quiz_binding: tuple[str, str, str] | None = None
        if module == "quiz":
            pinned_quiz_binding = pinned_moodle_quiz_binding(
                attempt.integrity_policy,
                course_external_id=external_course_id,
                cmid=external_cmid,
            )
            if pinned_quiz_binding is not None:
                answer_transport = pinned_quiz_binding[2]
        elif module == "assign":
            integrity_policy = (
                attempt.integrity_policy if isinstance(attempt.integrity_policy, dict) else {}
            )
            prepared_transport = normalize_moodle_essay_answer_transport(
                integrity_policy.get("moodle_answer_transport")
            )
            if prepared_transport in {"ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"}:
                answer_transport = prepared_transport
        if answer_transport is None:
            raise _BlockedDelivery(
                "MOODLE_ANSWER_TRANSPORT_REQUIRED",
                "A proven Moodle answer transport is required for code synchronization",
            )
        force_archive = False
        if module == "assign" and answer_transport == "ASSIGN_FILE":
            max_submission_files = activity.get("max_submission_files")
            if (
                not isinstance(max_submission_files, int)
                or isinstance(max_submission_files, bool)
                or max_submission_files < 1
                or activity.get("file_types_confirmed") is not True
            ):
                raise _BlockedDelivery(
                    "MOODLE_ASSIGNMENT_FILE_CONSTRAINTS_UNCONFIRMED",
                    "Moodle Assignment file limits and accepted types must be confirmed",
                )
            single_suffix = (
                PurePosixPath(str(files[0].get("path", ""))).suffix.lower()
                if len(files) == 1
                else ""
            )
            natural_suffix = (
                ".c"
                if single_suffix == ".c"
                else ".cpp"
                if single_suffix in {".cc", ".cpp", ".cxx"}
                else ".zip"
            )
            accepted_file_types = activity.get("accepted_file_types", "")
            if not moodle_file_type_allowed(
                accepted_file_types,
                natural_suffix,
            ):
                raise _BlockedDelivery(
                    "MOODLE_ASSIGNMENT_FILE_TYPE_FORBIDDEN",
                    "Moodle Assignment does not accept the generated artifact type",
                )
        try:
            artifact = (
                build_moodle_online_text_artifact(files)
                if answer_transport in {"ESSAY_ONLINE_TEXT", "ASSIGN_ONLINE_TEXT"}
                else build_moodle_submission_artifact(files, force_archive=force_archive)
            )
        except IntegrationProtocolError as exc:
            if answer_transport in {"ESSAY_ONLINE_TEXT", "ASSIGN_ONLINE_TEXT"}:
                raise _BlockedDelivery(
                    "MOODLE_ONLINE_TEXT_SINGLE_TRANSLATION_UNIT_REQUIRED",
                    str(exc),
                ) from exc
            raise
        max_submission_bytes = activity.get("max_submission_bytes")
        if (
            module == "assign"
            and isinstance(max_submission_bytes, int)
            and not isinstance(max_submission_bytes, bool)
            and artifact.size > max_submission_bytes
        ):
            raise _BlockedDelivery(
                "MOODLE_ASSIGNMENT_FILE_TOO_LARGE",
                "Moodle Assignment artifact exceeds the configured file size limit",
            )
        previous_events = list(
            (
                await db.scalars(
                    select(SyncOutbox)
                    .where(
                        SyncOutbox.attempt_id == attempt.id,
                        SyncOutbox.course_id == course.id,
                        SyncOutbox.connection_id == connection.id,
                        SyncOutbox.event_type == "attempt.checkpoint",
                        SyncOutbox.state == SyncOutboxState.DELIVERED.value,
                        SyncOutbox.id != claim.id,
                    )
                    .order_by(
                        SyncOutbox.delivered_at.desc(),
                        SyncOutbox.created_at.desc(),
                    )
                    .limit(32)
                )
            ).all()
        )
        previous_managed_filename, previous_managed_sha256 = (
            _previous_managed_artifact_from_receipts(
                [event.receipt for event in previous_events],
                module=module,
                answer_transport=answer_transport,
                course_id=external_course_id,
                cmid=external_cmid,
            )
        )
        expected_attempt_id = pinned_quiz_binding[0] if pinned_quiz_binding is not None else None
        expected_question_slot = pinned_quiz_binding[1] if pinned_quiz_binding is not None else None
        if module == "quiz":
            last_fingerprint = await db.scalar(
                select(LMSSubmissionFingerprint)
                .where(
                    LMSSubmissionFingerprint.connection_id == connection.id,
                    LMSSubmissionFingerprint.course_id == course.id,
                    LMSSubmissionFingerprint.attempt_id == attempt.id,
                    LMSSubmissionFingerprint.module == "quiz",
                    LMSSubmissionFingerprint.external_activity_id == str(external_cmid),
                    LMSSubmissionFingerprint.external_attempt_id != "",
                    LMSSubmissionFingerprint.external_question_slot != "",
                )
                .order_by(
                    LMSSubmissionFingerprint.delivered_at.desc(),
                    LMSSubmissionFingerprint.id.desc(),
                )
            )
            if last_fingerprint is not None:
                fingerprint_identity = (
                    last_fingerprint.external_attempt_id,
                    last_fingerprint.external_question_slot,
                )
                if (
                    expected_attempt_id is not None
                    and expected_question_slot is not None
                    and fingerprint_identity != (expected_attempt_id, expected_question_slot)
                ):
                    raise _BlockedDelivery(
                        "MOODLE_ATTEMPT_BINDING_CONFLICT",
                        "A prior checkpoint belongs to another Moodle attempt",
                    )
                expected_attempt_id, expected_question_slot = fingerprint_identity
        browser_payload: dict[str, Any] = {
            "module": module,
            "course_id": int(external_course_id),
            "cmid": external_cmid,
            "answer_transport": answer_transport,
            "artifact_filename": artifact.filename,
            "artifact_bytes": artifact.raw_bytes,
            "artifact_sha256": artifact.sha256,
            "artifact_size": artifact.size,
            "finalize": reason in {"SUBMISSION", "DEADLINE"},
            "requires_submission_statement": (
                activity.get("requires_submission_statement") is True
            ),
            "submission_drafts": (
                activity.get("submission_drafts")
                if isinstance(activity.get("submission_drafts"), bool)
                else None
            ),
            "max_submission_bytes_inherited": (
                activity.get("max_submission_bytes_inherited") is True
            ),
            "previous_managed_filename": previous_managed_filename,
            "previous_managed_sha256": previous_managed_sha256,
            "expected_attempt_id": expected_attempt_id,
            "expected_question_slot": expected_question_slot,
            "snapshot_ref": str(snapshot.id),
            "snapshot_sha256": str(manifest_hash).lower(),
            "reason": reason,
        }
        connection = await _principal_credential_target(
            db,
            settings,
            connection,
            principal.id,
        )
        return _CheckpointDelivery(connection=connection, payload=browser_payload)
    return _CheckpointDelivery(connection=connection, payload=payload)


async def _prepare_task_version(
    db: AsyncSession,
    claim: ClaimedOutboxEvent,
    connection: ConnectionTarget,
) -> _TaskVersionDelivery:
    if connection.mode == "PLUGINLESS":
        raise _BlockedDelivery(
            "PLUGINLESS_TASK_MIRROR_UNSUPPORTED",
            "Immutable Moodle task mirroring requires the optional bridge plugin",
        )
    if claim.course_id is None or claim.aggregate_type != "TaskVersion":
        raise _BlockedDelivery("INVALID_TASK_VERSION", "Task mirror references are incomplete")
    version = await db.get(TaskVersion, claim.aggregate_id)
    item = await db.get(TaskBankItem, version.item_id) if version is not None else None
    course = await db.get(Course, claim.course_id)
    if version is None or item is None or course is None:
        raise _BlockedDelivery("MISSING_TASK_VERSION", "Task mirror source no longer exists")
    if course.connection_id != claim.connection_id or (
        item.course_id is not None and item.course_id != course.id
    ):
        raise _BlockedDelivery("TASK_VERSION_CONTEXT_MISMATCH", "Task mirror ownership is invalid")
    if item.course_id is None:
        course_use = await db.scalar(
            select(AssessmentItem.id)
            .join(Assessment, Assessment.id == AssessmentItem.assessment_id)
            .where(
                AssessmentItem.task_version_id == version.id,
                Assessment.course_id == course.id,
            )
            .limit(1)
        )
        if course_use is None:
            raise _BlockedDelivery(
                "TASK_VERSION_CONTEXT_MISMATCH",
                "Shared task mirror is not attached to the target course",
            )
    if version.number < 1 or version.status not in {"PUBLISHED", "ARCHIVED"}:
        raise _BlockedDelivery("INVALID_TASK_VERSION", "Task mirror version is not publishable")
    _positive_integer(course.external_id, "Moodle course id")

    semantic_content = _task_version_content(version)
    semantic_hash = canonical_hash(semantic_content)
    if not _is_sha256(version.content_hash) or not hmac.compare_digest(
        semantic_hash, version.content_hash.lower()
    ):
        raise _BlockedDelivery("TASK_CONTENT_MISMATCH", "Task semantic content hash is invalid")
    try:
        definition_json = _bounded_canonical_json(
            _task_mirror_definition(item, version),
            maximum=_TASK_MIRROR_BYTES,
            label="Task mirror definition",
        )
    except IntegrationProtocolError as exc:
        raise _BlockedDelivery("INVALID_TASK_DEFINITION", str(exc)) from exc
    payload = {
        "course_id": course.external_id,
        "task_ref": str(item.id),
        "version": version.number,
        "content_hash": version.content_hash.lower(),
        "definition_sha256": sha256_text(definition_json),
        "definition_json": definition_json,
        "status": version.status,
    }
    if not _same_task_payload(claim.payload, payload):
        raise _BlockedDelivery(
            "TASK_VERSION_CONTEXT_MISMATCH", "Task mirror outbox payload is stale or invalid"
        )
    return _TaskVersionDelivery(
        connection=connection,
        payload=payload,
        course_id=course.id,
        task_item_id=item.id,
        task_version_id=version.id,
    )


async def _prepare_grade(
    db: AsyncSession,
    settings: Settings,
    claim: ClaimedOutboxEvent,
    connection: ConnectionTarget,
) -> _GradeDelivery:
    if claim.aggregate_type != "ReviewDecision":
        raise _BlockedDelivery("INVALID_GRADE_EVENT", "Grade event aggregate is invalid")
    decision = await db.get(ReviewDecision, claim.aggregate_id)
    if decision is None:
        raise _BlockedDelivery("MISSING_DECISION", "Review decision no longer exists")
    latest = await db.scalar(
        select(ReviewDecision)
        .where(ReviewDecision.submission_id == decision.submission_id)
        .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
    )
    if latest is None or latest.id != decision.id or decision.status != "APPLIED":
        raise _BlockedDelivery("SUPERSEDED", "A newer review decision exists")
    submission = await db.get(Submission, decision.submission_id)
    attempt = await db.get(Attempt, submission.attempt_id) if submission else None
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    course = await db.get(Course, assessment.course_id) if assessment else None
    principal = await db.get(ExternalPrincipal, attempt.principal_id) if attempt else None
    reviewer = await db.get(ExternalPrincipal, decision.reviewer_id)
    if (
        submission is None
        or attempt is None
        or assessment is None
        or course is None
        or principal is None
        or reviewer is None
        or course.connection_id != claim.connection_id
        or claim.course_id != course.id
    ):
        raise _BlockedDelivery("GRADE_CONTEXT_MISMATCH", "Grade ownership is invalid")
    if not await is_latest_completed_moodle_attempt(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    ):
        raise _BlockedDelivery(
            "SUPERSEDED",
            "This Moodle attempt has been superseded by a newer attempt",
        )
    signed_comment = _comment_with_reviewer_signature(
        decision.comment,
        _reviewer_signature(reviewer.display_name),
    )
    historical_mapping = await db.scalar(
        select(ExternalMapping).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.local_type.in_(["Submission", "core.Submission"]),
            ExternalMapping.local_id == submission.id,
            ExternalMapping.external_type == "moodle_historical_submission",
        )
    )
    receipt = submission.external_receipt if isinstance(submission.external_receipt, dict) else {}
    historical_metadata = (
        historical_mapping.metadata_json
        if historical_mapping is not None and isinstance(historical_mapping.metadata_json, dict)
        else {}
    )
    historical_module = str(historical_metadata.get("module", "")).lower()
    historical_receipt = (
        submission.source == "MOODLE_IMPORT"
        or str(receipt.get("source", "")).upper() == "MOODLE_HISTORY"
        or bool(receipt.get("moodle_parent_attempt_id"))
    )
    if historical_receipt and historical_mapping is None:
        raise _BlockedDelivery(
            "MAPPING_REQUIRED",
            "Historical Moodle submission mapping is required",
        )
    if historical_mapping is not None and historical_module not in {"assign", "quiz"}:
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Historical Moodle submission module mapping is invalid",
        )

    # A submission completed through the IDE is reviewable before the reverse
    # Moodle history crawl materializes its canonical imported twin.  Unlike a
    # historical submission it deliberately has no remote receipt while a
    # grade is pending, so derive the Quiz target only from the immutable
    # runtime binding and the terminal delivery attestation.  Never infer a
    # Quiz target from the assessment mapping alone: that could grade a Moodle
    # attempt which did not receive this exact submitted snapshot.
    runtime_policy = dict(attempt.integrity_policy or {})
    if (
        historical_mapping is None
        and not historical_receipt
        and runtime_policy.get("moodle_runtime_prepared") is True
    ):
        if submission.source not in {"MANUAL", "DEADLINE"}:
            raise _BlockedDelivery(
                "MOODLE_QUIZ_SUBMISSION_NOT_CONFIRMED",
                "The local Moodle Quiz submission source is not confirmed",
            )
        if connection.mode != "PLUGINLESS" or connection.transport != "PLAYWRIGHT":
            raise _BlockedDelivery(
                "QUIZ_GRADE_EXPORT_UNSUPPORTED",
                "Moodle Quiz Essay grades require the Playwright connector",
            )
        exact_activity_mappings = [
            row
            for row in (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.connection_id == course.connection_id,
                        ExternalMapping.local_id == assessment.id,
                        ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                    )
                )
            ).all()
            if _moodle_activity_mapping_module(row) in {"assign", "quiz"}
        ]
        if (
            len(exact_activity_mappings) != 1
            or _moodle_activity_mapping_module(exact_activity_mappings[0]) != "quiz"
        ):
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "Exactly one Moodle Quiz activity mapping must match the local assessment",
            )
        quiz_mapping = exact_activity_mappings[0]
        quiz_metadata = (
            quiz_mapping.metadata_json if isinstance(quiz_mapping.metadata_json, dict) else {}
        )
        mapped_external_cmid = _positive_integer(
            quiz_mapping.external_id,
            "Moodle quiz mapping cmid",
        )
        cmid = _positive_integer(
            quiz_metadata.get("cmid", mapped_external_cmid),
            "Moodle quiz cmid",
        )
        if cmid != mapped_external_cmid:
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "Moodle Quiz activity identifiers are inconsistent",
            )
        pinned_binding = pinned_moodle_quiz_binding(
            runtime_policy,
            course_external_id=course.external_id,
            cmid=cmid,
        )
        if pinned_binding is None:
            raise _BlockedDelivery(
                "MOODLE_ATTEMPT_BINDING_CONFLICT",
                "The submitted Moodle Quiz attempt does not match its activity mapping",
            )
        external_attempt_id, response_id, answer_transport = pinned_binding
        expected_terminal_reason = "DEADLINE" if submission.source == "DEADLINE" else "SUBMISSION"
        terminal_fingerprint = await db.scalar(
            select(LMSSubmissionFingerprint.id).where(
                LMSSubmissionFingerprint.connection_id == course.connection_id,
                LMSSubmissionFingerprint.course_id == course.id,
                LMSSubmissionFingerprint.assessment_id == assessment.id,
                LMSSubmissionFingerprint.principal_id == principal.id,
                LMSSubmissionFingerprint.attempt_id == attempt.id,
                LMSSubmissionFingerprint.submission_id == submission.id,
                LMSSubmissionFingerprint.snapshot_id == submission.snapshot_id,
                LMSSubmissionFingerprint.module == "quiz",
                LMSSubmissionFingerprint.external_activity_id == str(cmid),
                LMSSubmissionFingerprint.external_attempt_id == external_attempt_id,
                LMSSubmissionFingerprint.external_question_slot == response_id,
                LMSSubmissionFingerprint.answer_transport == answer_transport,
                LMSSubmissionFingerprint.checkpoint_reason == expected_terminal_reason,
                LMSSubmissionFingerprint.terminal.is_(True),
            )
        )
        if terminal_fingerprint is None:
            raise _BlockedDelivery(
                "MOODLE_QUIZ_TERMINAL_DELIVERY_REQUIRED",
                (
                    "The exact submitted snapshot must be finalized in Moodle before "
                    "its grade can be exported"
                ),
            )
        grade_scale_max = positive_decimal(assessment.max_score)
        if grade_scale_max is None or decision.grade > grade_scale_max:
            raise _BlockedDelivery(
                "QUIZ_GRADE_SCALE_UNCONFIRMED",
                "The local Quiz grade scale is missing or smaller than the decision grade",
            )
        quiz_activity = quiz_metadata.get("activity")
        if (
            not isinstance(quiz_activity, dict)
            or str(quiz_activity.get("cmid", "")) != str(cmid)
            or str(quiz_activity.get("module", "")).lower().removeprefix("mod_") != "quiz"
        ):
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "The confirmed Moodle Quiz activity does not match the submitted attempt",
            )
        quiz_overall_grade_max = (
            positive_decimal(quiz_activity.get("grade_max"))
            if quiz_activity.get("grade_confirmed") is True
            else None
        )
        if quiz_overall_grade_max is None:
            raise _BlockedDelivery(
                "QUIZ_GRADE_SCALE_UNCONFIRMED",
                "The Moodle Quiz overall grade scale is not confirmed",
            )
        if not moodle_quiz_uses_latest_attempt_grade(quiz_activity):
            raise _BlockedDelivery(
                "MOODLE_LAST_ATTEMPT_GRADING_REQUIRED",
                (
                    "Moodle Quiz must use the 'Last attempt' grading method before "
                    "the latest attempt grade can be exported"
                ),
            )
        payload = {
            "module": "quiz",
            "courseid": _positive_integer(course.external_id, "Moodle course id"),
            "cmid": cmid,
            "userid": _positive_integer(principal.external_subject, "Moodle user id"),
            "attempt_id": _positive_integer(external_attempt_id, "Moodle quiz attempt id"),
            "question_slot": _positive_integer(response_id, "Moodle quiz response slot"),
            "grade": str(decision.grade),
            "grade_scale_max": str(grade_scale_max),
            "quiz_overall_grade_max": str(quiz_overall_grade_max),
            "comment": signed_comment,
        }
        connection = await _principal_credential_target(
            db, settings, connection, decision.reviewer_id
        )
        return _GradeDelivery(
            connection=connection,
            decision_id=decision.id,
            submission_id=submission.id,
            payload=payload,
        )
    historical_assignment_target: tuple[int, int] | None = None
    if historical_module == "assign" and historical_mapping is not None:
        historical_assignment_target = _confirmed_historical_assignment_attempt_number(
            mapping=historical_mapping,
            metadata=historical_metadata,
            receipt=receipt,
            course=course,
            assessment=assessment,
            principal=principal,
        )
    if historical_module == "quiz" and historical_mapping is not None:
        if connection.mode != "PLUGINLESS" or connection.transport != "PLAYWRIGHT":
            raise _BlockedDelivery(
                "QUIZ_GRADE_EXPORT_UNSUPPORTED",
                "Historical Moodle Quiz Essay grades require the Playwright connector",
            )
        if receipt.get("external_id") != historical_mapping.external_id:
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "Historical Moodle Quiz submission mapping is inconsistent",
            )
        if str(historical_metadata.get("course_id", "")) != str(course.external_id):
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "Historical Moodle Quiz course mapping is inconsistent",
            )
        attempt_id = _matching_positive_external_value(
            receipt,
            historical_metadata,
            "moodle_parent_attempt_id",
            "Moodle quiz attempt id",
        )
        response_id = _matching_positive_external_value(
            receipt,
            historical_metadata,
            "moodle_response_id",
            "Moodle quiz response slot",
            fallback_key="moodle_response_position",
        )
        cmid = _positive_integer(historical_metadata.get("cmid"), "Moodle quiz cmid")
        grade_scale_max = positive_decimal(assessment.max_score)
        if grade_scale_max is None or decision.grade > grade_scale_max:
            raise _BlockedDelivery(
                "QUIZ_GRADE_SCALE_UNCONFIRMED",
                "The local Quiz grade scale is missing or smaller than the decision grade",
            )
        mapping_assessment_ids = {assessment.id}
        raw_parent_id = dict(assessment.policy or {}).get("moodle_parent_assessment_id")
        if raw_parent_id:
            try:
                parent_id = uuid.UUID(str(raw_parent_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise _BlockedDelivery(
                    "MAPPING_NOT_CONFIRMED",
                    "Moodle Quiz parent assessment mapping is invalid",
                ) from exc
            parent = await db.get(Assessment, parent_id)
            if parent is None or parent.course_id != assessment.course_id:
                raise _BlockedDelivery(
                    "MAPPING_NOT_CONFIRMED",
                    "Moodle Quiz parent assessment belongs to another context",
                )
            mapping_assessment_ids.add(parent.id)
        quiz_mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.connection_id == course.connection_id,
                        ExternalMapping.local_id.in_(mapping_assessment_ids),
                        ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                    )
                )
            ).all()
        )
        quiz_mappings = [
            row
            for row in quiz_mappings
            if _moodle_activity_mapping_module(row) == "quiz"
            and str(dict(row.metadata_json or {}).get("cmid", row.external_id)) == str(cmid)
        ]
        quiz_mapping = quiz_mappings[0] if len(quiz_mappings) == 1 else None
        quiz_metadata = (
            quiz_mapping.metadata_json
            if quiz_mapping is not None and isinstance(quiz_mapping.metadata_json, dict)
            else {}
        )
        quiz_activity = quiz_metadata.get("activity")
        quiz_overall_grade_max = (
            positive_decimal(quiz_activity.get("grade_max"))
            if isinstance(quiz_activity, dict) and quiz_activity.get("grade_confirmed") is True
            else None
        )
        if quiz_overall_grade_max is None:
            raise _BlockedDelivery(
                "QUIZ_GRADE_SCALE_UNCONFIRMED",
                "The Moodle Quiz overall grade scale is not confirmed",
            )
        mapped_cmid = str(
            quiz_metadata.get("cmid", quiz_mapping.external_id if quiz_mapping else "")
        )
        if mapped_cmid != str(cmid):
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "Moodle Quiz activity mapping does not match the graded attempt",
            )
        if not moodle_quiz_uses_latest_attempt_grade(quiz_metadata.get("activity")):
            raise _BlockedDelivery(
                "MOODLE_LAST_ATTEMPT_GRADING_REQUIRED",
                (
                    "Moodle Quiz must use the 'Last attempt' grading method before "
                    "the latest attempt grade can be exported"
                ),
            )
        payload = {
            "module": "quiz",
            "courseid": _positive_integer(course.external_id, "Moodle course id"),
            "cmid": cmid,
            "userid": _positive_integer(principal.external_subject, "Moodle user id"),
            "attempt_id": attempt_id,
            "question_slot": response_id,
            "grade": str(decision.grade),
            "grade_scale_max": str(grade_scale_max),
            "quiz_overall_grade_max": str(quiz_overall_grade_max),
            "comment": signed_comment,
        }
        connection = await _principal_credential_target(
            db, settings, connection, decision.reviewer_id
        )
        return _GradeDelivery(
            connection=connection,
            decision_id=decision.id,
            submission_id=submission.id,
            payload=payload,
        )

    mapping_rows = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.local_id == assessment.id,
                )
            )
        ).all()
    )
    mapping = next((item for item in mapping_rows if _is_mod_assign_mapping(item)), None)
    if mapping is None:
        raise _BlockedDelivery(
            "MAPPING_REQUIRED", "Explicit Moodle mod_assign cmid mapping is required"
        )
    mapping_metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    activity = mapping_metadata.get("activity")
    if not isinstance(activity, dict):
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Moodle Assignment mapping has not been confirmed by course synchronization",
        )
    grade_max = positive_decimal(activity.get("grade_max"))
    if grade_max is None:
        raise _BlockedDelivery(
            "UNSUPPORTED_MOODLE_GRADING",
            "Mapped Moodle Assignment must use a positive numeric grade",
        )
    if grade_max != assessment.max_score:
        raise _BlockedDelivery(
            "GRADE_RANGE_MISMATCH",
            "Assessment and Moodle Assignment maximum scores differ",
        )
    cmid_raw = mapping_metadata.get("cmid", mapping.external_id)
    cmid = _positive_integer(cmid_raw, "Moodle assignment cmid")
    user_id = _positive_integer(principal.external_subject, "Moodle user id")
    attempt_number: int | None = None
    if historical_assignment_target is not None:
        historical_cmid, attempt_number = historical_assignment_target
        if historical_cmid != cmid:
            raise _BlockedDelivery(
                "MAPPING_NOT_CONFIRMED",
                "Historical Moodle Assignment activity mapping is inconsistent",
            )
    payload = {
        "courseid": _positive_integer(course.external_id, "Moodle course id"),
        "cmid": cmid,
        "userid": user_id,
        "grade": str(decision.grade),
        "comment": signed_comment,
    }
    if attempt_number is not None:
        payload["attempt_number"] = attempt_number
    if connection.mode == "PLUGINLESS":
        if connection.transport == "PLAYWRIGHT":
            payload["module"] = "assign"
        else:
            payload["assignmentid"] = _positive_integer(
                activity.get("instance_id"), "Moodle assignment instance id"
            )
        connection = await _principal_credential_target(
            db, settings, connection, decision.reviewer_id
        )
    return _GradeDelivery(
        connection=connection,
        decision_id=decision.id,
        submission_id=submission.id,
        payload=payload,
    )


async def _prepare_course_sync(
    db: AsyncSession,
    settings: Settings,
    claim: ClaimedOutboxEvent,
    connection: ConnectionTarget,
) -> _CourseDelivery:
    if claim.course_id is None or claim.aggregate_type != "Course":
        raise _BlockedDelivery("INVALID_COURSE_SYNC", "Course sync aggregate is invalid")
    course = await db.get(Course, claim.course_id)
    if (
        course is None
        or course.id != claim.aggregate_id
        or course.connection_id != claim.connection_id
    ):
        raise _BlockedDelivery("COURSE_CONTEXT_MISMATCH", "Course sync ownership is invalid")
    course_id = course.id
    external_course_id = course.external_id
    actors = [
        (principal_id, external_subject)
        for principal_id, external_subject in (
            await db.execute(
                select(ExternalPrincipal.id, ExternalPrincipal.external_subject)
                .join(
                    CourseMembership,
                    CourseMembership.principal_id == ExternalPrincipal.id,
                )
                .where(
                    CourseMembership.course_id == course.id,
                    CourseMembership.role == CourseRole.TEACHER.value,
                    CourseMembership.active.is_(True),
                    ExternalPrincipal.active.is_(True),
                )
                .order_by(ExternalPrincipal.id)
            )
        ).all()
    ]
    actors = [actor for actor in actors if await teacher_membership_is_authorized(db, actor[0])]
    if not actors:
        raise _BlockedDelivery(
            "TEACHER_CONTEXT_REQUIRED", "Course sync requires an active Moodle teacher"
        )
    _, actor_external_subject = actors[0]
    if connection.mode == "PLUGINLESS":
        connection_with_credential = None
        for candidate_id, candidate_external_subject in actors:
            try:
                connection_with_credential = await _principal_credential_target(
                    db, settings, connection, candidate_id
                )
            except _BlockedDelivery:
                continue
            actor_external_subject = candidate_external_subject
            break
        if connection_with_credential is None:
            raise _BlockedDelivery(
                "LMS_REAUTH_REQUIRED",
                "Course synchronization requires an active teacher Moodle session",
            )
        connection = connection_with_credential
    return _CourseDelivery(
        connection=connection,
        course_id=course_id,
        external_course_id=external_course_id,
        actor_external_subject=actor_external_subject,
    )


async def _prepare_history_import(
    db: AsyncSession,
    settings: Settings,
    claim: ClaimedOutboxEvent,
    connection: ConnectionTarget,
) -> _HistoryImportDelivery:
    """Validate one bounded historical-import page and lease a teacher session."""

    if claim.course_id is None or claim.aggregate_type != "Assessment":
        raise _BlockedDelivery(
            "INVALID_HISTORY_IMPORT",
            "Historical Moodle import references are incomplete",
        )
    assessment = await db.get(Assessment, claim.aggregate_id)
    course = await db.get(Course, claim.course_id)
    if (
        assessment is None
        or course is None
        or assessment.course_id != course.id
        or course.connection_id != claim.connection_id
    ):
        raise _BlockedDelivery(
            "HISTORY_IMPORT_CONTEXT_MISMATCH",
            "Historical Moodle import ownership is invalid",
        )
    if connection.mode != "PLUGINLESS" or connection.transport != "PLAYWRIGHT":
        raise _BlockedDelivery(
            "HISTORY_IMPORT_UNSUPPORTED",
            "Historical Moodle import requires the Playwright transport",
        )

    mappings = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.local_id == assessment.id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                )
            )
        ).all()
    )
    mapping = next(
        (item for item in mappings if _moodle_activity_mapping_module(item) in {"quiz", "assign"}),
        None,
    )
    if mapping is None:
        raise _BlockedDelivery(
            "HISTORY_IMPORT_MAPPING_REQUIRED",
            "Historical Moodle import requires a mapped Quiz or Assignment",
        )
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    module = _moodle_activity_mapping_module(mapping)
    if module is None:
        raise _BlockedDelivery(
            "HISTORY_IMPORT_MAPPING_REQUIRED",
            "Historical Moodle activity type is unavailable",
        )
    cmid = _positive_integer(metadata.get("cmid", mapping.external_id), "Moodle activity cmid")

    payload = dict(claim.payload or {})
    if str(payload.get("course_id", "")) != course.external_id:
        raise _BlockedDelivery(
            "HISTORY_IMPORT_CONTEXT_MISMATCH",
            "Historical Moodle course identifier is invalid",
        )
    if str(payload.get("module", "")).lower() != module:
        raise _BlockedDelivery(
            "HISTORY_IMPORT_CONTEXT_MISMATCH",
            "Historical Moodle activity type is invalid",
        )
    if _positive_integer(payload.get("cmid"), "Moodle activity cmid") != cmid:
        raise _BlockedDelivery(
            "HISTORY_IMPORT_CONTEXT_MISMATCH",
            "Historical Moodle activity identifier is invalid",
        )
    cursor = payload.get("cursor", "0:0")
    cursor_parts = str(cursor).split(":")
    if (
        not isinstance(cursor, str)
        or len(cursor_parts) != 2
        or any(not part.isdigit() or len(part) > 6 for part in cursor_parts)
    ):
        raise _BlockedDelivery(
            "INVALID_HISTORY_IMPORT",
            "Historical Moodle import cursor is invalid",
        )
    limit = payload.get("limit", 10)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
        raise _BlockedDelivery(
            "INVALID_HISTORY_IMPORT",
            "Historical Moodle import page size is invalid",
        )
    priority_only = payload.get("priority_only", False)
    if not isinstance(priority_only, bool):
        raise _BlockedDelivery(
            "INVALID_HISTORY_IMPORT",
            "Historical Moodle import priority flag is invalid",
        )

    actor_external_subject = str(payload.get("actor_external_subject", "")).strip()
    if not actor_external_subject:
        raise _BlockedDelivery(
            "INVALID_HISTORY_IMPORT",
            "Historical Moodle import actor is missing",
        )
    actor = await db.scalar(
        select(ExternalPrincipal)
        .join(
            CourseMembership,
            CourseMembership.principal_id == ExternalPrincipal.id,
        )
        .where(
            ExternalPrincipal.connection_id == course.connection_id,
            ExternalPrincipal.external_subject == actor_external_subject,
            ExternalPrincipal.active.is_(True),
            CourseMembership.course_id == course.id,
            CourseMembership.role == CourseRole.TEACHER.value,
            CourseMembership.active.is_(True),
            or_(
                CourseMembership.valid_until.is_(None),
                CourseMembership.valid_until > utcnow(),
            ),
        )
    )
    if actor is None or not await teacher_membership_is_authorized(db, actor.id):
        raise _BlockedDelivery(
            "HISTORY_IMPORT_ACTOR_FORBIDDEN",
            "Historical Moodle import actor is no longer an authorized course teacher",
        )
    # Never fall back to another teacher here: every subsequent page belongs
    # to the exact Moodle visibility scope captured by the initial event.
    leased_connection = await _principal_credential_target(
        db,
        settings,
        connection,
        actor.id,
    )
    return _HistoryImportDelivery(
        connection=leased_connection,
        course_id=course.id,
        assessment_id=assessment.id,
        actor_external_subject=actor_external_subject,
        payload={
            "course_id": course.external_id,
            "actor_external_subject": actor_external_subject,
            "module": module,
            "cmid": cmid,
            "cursor": cursor,
            "limit": limit,
            "priority_only": priority_only,
        },
    )


async def _deliver(
    bridge: MoodleAdapter,
    context: DeliveryContext,
    idempotency_key: str,
) -> dict[str, Any] | CourseDiscovery | _BrowserDeliveryResult:
    if isinstance(context, _CheckpointDelivery):
        return await bridge.store_checkpoint(context.payload, idempotency_key)
    if isinstance(context, _GradeDelivery):
        return await bridge.push_grade(context.payload, idempotency_key)
    if isinstance(context, _TaskVersionDelivery):
        return await bridge.upsert_task_definition(context.payload, idempotency_key)
    if isinstance(context, _HistoryImportDelivery):
        return await bridge.discover_historical_submissions(context.payload)
    return await bridge.discover_course(
        context.external_course_id,
        context.actor_external_subject,
    )


_MOODLE_FINALIZED_ERROR = (
    "MOODLE_ATTEMPT_FINALIZED: Moodle attempt was already finalized outside the application"
)


def _nonfinal_assignment_became_finalized(
    context: DeliveryContext,
    result: dict[str, Any] | CourseDiscovery | _BrowserDeliveryResult,
) -> bool:
    if not isinstance(context, _CheckpointDelivery):
        return False
    payload = context.payload
    if payload.get("module") != "assign" or payload.get("finalize") is not False:
        return False
    value = result.value if isinstance(result, _BrowserDeliveryResult) else result
    return isinstance(value, dict) and value.get("status") == "FINALIZED"


async def _mark_moodle_attempt_finalized(
    db: AsyncSession,
    *,
    attempt: Attempt,
    finalized_at: datetime,
    delivered_event_id: uuid.UUID | None = None,
) -> None:
    """Make an externally terminal Moodle attempt locally read-only.

    The Attempt lock must be acquired before this helper is called.  Blocking
    every undelivered checkpoint in the same transaction prevents an older
    periodic revision from being retried after the terminal state is known.
    """

    attempt.state = AttemptState.LOCKED.value
    attempt.submission_source = "MOODLE_FINALIZED"
    if attempt.submitted_at is None:
        attempt.submitted_at = finalized_at
    attempt.integrity_policy = {
        **dict(attempt.integrity_policy or {}),
        "closure_reason": "MOODLE_FINALIZED",
    }
    pending = list(
        (
            await db.scalars(
                select(SyncOutbox)
                .where(
                    SyncOutbox.attempt_id == attempt.id,
                    SyncOutbox.event_type == "attempt.checkpoint",
                    SyncOutbox.state != SyncOutboxState.DELIVERED.value,
                )
                .with_for_update()
            )
        ).all()
    )
    for event in pending:
        if delivered_event_id is not None and event.id == delivered_event_id:
            continue
        event.state = SyncOutboxState.BLOCKED.value
        event.last_error = _MOODLE_FINALIZED_ERROR
        event.locked_at = None
    submissions = list(
        (
            await db.scalars(
                select(Submission)
                .where(
                    Submission.attempt_id == attempt.id,
                    Submission.lms_export_state != "DELIVERED",
                )
                .with_for_update()
            )
        ).all()
    )
    for submission in submissions:
        submission.lms_export_state = "BLOCKED"


async def _finish_moodle_attempt_finalized(
    session_factory: SessionFactory,
    claim: ClaimedOutboxEvent,
    *,
    now: datetime | None,
) -> bool:
    if claim.attempt_id is None or claim.event_type != "attempt.checkpoint":
        return await _finish_blocked(
            session_factory,
            claim,
            _MOODLE_FINALIZED_ERROR,
        )
    async with session_factory() as db, db.begin():
        # Keep the same lock order as workspace mutations: Attempt, then
        # checkpoint rows.  This avoids a deadlock with a concurrent save.
        attempt = await db.scalar(
            select(Attempt).where(Attempt.id == claim.attempt_id).with_for_update()
        )
        row = await _owned_claim(db, claim)
        if row is None:
            return False
        if attempt is None:
            row.state = SyncOutboxState.BLOCKED.value
            row.last_error = "MISSING_CHECKPOINT: Checkpoint attempt no longer exists"
            row.locked_at = None
            return True
        await _mark_moodle_attempt_finalized(
            db,
            attempt=attempt,
            finalized_at=now or utcnow(),
        )
        return True


async def _finish_delivered(
    session_factory: SessionFactory,
    settings: Settings,
    claim: ClaimedOutboxEvent,
    context: DeliveryContext,
    result: dict[str, Any] | CourseDiscovery | _BrowserDeliveryResult,
) -> bool:
    closes_after_assignment_save = _nonfinal_assignment_became_finalized(context, result)
    async with session_factory() as db, db.begin():
        locked_attempt: Attempt | None = None
        if isinstance(context, _CheckpointDelivery) and claim.attempt_id is not None:
            locked_attempt = await db.scalar(
                select(Attempt).where(Attempt.id == claim.attempt_id).with_for_update()
            )
        row = await _owned_claim(db, claim)
        if row is None:
            return False
        if locked_attempt is not None and locked_attempt.submission_source == "MOODLE_FINALIZED":
            # Another delivery already observed the terminal Moodle state.
            # This response must not create a second fingerprint or be treated
            # as proof that its artifact was accepted.
            await _mark_moodle_attempt_finalized(
                db,
                attempt=locked_attempt,
                finalized_at=locked_attempt.submitted_at or utcnow(),
            )
            return False
        browser_state: dict[str, Any] | None = None
        if isinstance(result, _BrowserDeliveryResult):
            if context.connection.transport != "PLAYWRIGHT":
                raise IntegrationProtocolError("Unexpected Moodle browser delivery result")
            browser_state = result.storage_state
            result = result.value
        elif context.connection.transport == "PLAYWRIGHT":
            raise IntegrationProtocolError("Moodle browser session state is missing")
        if isinstance(context, _GradeDelivery):
            decision = await db.get(ReviewDecision, context.decision_id)
            submission = await db.scalar(
                select(Submission).where(Submission.id == context.submission_id).with_for_update()
            )
            latest = (
                await db.scalar(
                    select(ReviewDecision)
                    .where(ReviewDecision.submission_id == context.submission_id)
                    .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
                )
                if submission is not None
                else None
            )
            if (
                decision is None
                or submission is None
                or latest is None
                or latest.id != decision.id
                or decision.status != "APPLIED"
            ):
                row.state = SyncOutboxState.BLOCKED.value
                row.last_error = "SUPERSEDED: A newer review decision exists"
                row.locked_at = None
                if decision is not None:
                    decision.lms_export_state = "SUPERSEDED"
                return True
        if isinstance(context, _CourseDelivery):
            if not isinstance(result, CourseDiscovery):
                raise IntegrationProtocolError("Course discovery result has an invalid shape")
            await _apply_course_discovery(
                db,
                context.course_id,
                result,
                actor_external_subject=context.actor_external_subject,
            )
            receipt: dict[str, Any] = {
                "status": "DELIVERED",
                "external_revision": str(result.preview.get("external_revision", "")),
                "membership_revision": str(result.preview.get("membership_revision", "")),
                "section_count": len(result.preview.get("sections", [])),
                "member_count": len(
                    result.preview.get("membership_snapshot", {}).get("members", [])
                ),
            }
        elif isinstance(context, _HistoryImportDelivery):
            if not isinstance(result, dict):
                raise IntegrationProtocolError(
                    "Historical Moodle import result has an invalid shape"
                )
            course = await db.get(Course, context.course_id)
            assessment = await db.get(Assessment, context.assessment_id)
            if (
                course is None
                or assessment is None
                or assessment.course_id != course.id
                or course.connection_id != claim.connection_id
            ):
                raise IntegrationProtocolError(
                    "Historical Moodle import target changed during delivery"
                )
            items = result.get("items")
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise IntegrationProtocolError(
                    "Historical Moodle submissions have an invalid shape"
                )
            stats = await materialize_historical_submissions(
                db,
                course=course,
                assessment=assessment,
                actor_external_subject=context.actor_external_subject,
                items=items,
            )
            next_cursor = result.get("next_cursor")
            complete = result.get("complete")
            if (
                not isinstance(complete, bool)
                or (complete and next_cursor is not None)
                or (not complete and not isinstance(next_cursor, str))
            ):
                raise IntegrationProtocolError("Historical Moodle pagination is inconsistent")
            next_page_queued = False
            full_scan_queued = False
            if isinstance(next_cursor, str):
                cursor_hash = hashlib.sha256(next_cursor.encode("utf-8")).hexdigest()[:12]
                next_key = f"history:{claim.id.hex[:20]}:{cursor_hash}"
                if not await db.scalar(
                    select(SyncOutbox.id).where(SyncOutbox.idempotency_key == next_key)
                ):
                    db.add(
                        SyncOutbox(
                            connection_id=course.connection_id,
                            course_id=course.id,
                            event_type="moodle.history.import",
                            aggregate_type="Assessment",
                            aggregate_id=assessment.id,
                            idempotency_key=next_key,
                            payload={
                                "course_id": course.external_id,
                                "actor_external_subject": context.actor_external_subject,
                                "module": context.payload["module"],
                                "cmid": context.payload["cmid"],
                                "cursor": next_cursor,
                                "limit": context.payload["limit"],
                                "priority_only": context.payload.get("priority_only", False),
                            },
                        )
                    )
                    next_page_queued = True
            elif context.payload.get("priority_only") is True:
                active_history_rows = list(
                    (
                        await db.scalars(
                            select(SyncOutbox).where(
                                SyncOutbox.course_id == course.id,
                                SyncOutbox.aggregate_id == assessment.id,
                                SyncOutbox.event_type == "moodle.history.import",
                                SyncOutbox.state.in_(
                                    [
                                        SyncOutboxState.PENDING.value,
                                        SyncOutboxState.PROCESSING.value,
                                        SyncOutboxState.RETRY.value,
                                    ]
                                ),
                            )
                        )
                    ).all()
                )
                full_scan_active = any(
                    str((candidate.payload or {}).get("actor_external_subject", ""))
                    == context.actor_external_subject
                    and (candidate.payload or {}).get("priority_only") is not True
                    for candidate in active_history_rows
                )
                if not full_scan_active:
                    db.add(
                        SyncOutbox(
                            connection_id=course.connection_id,
                            course_id=course.id,
                            event_type="moodle.history.import",
                            aggregate_type="Assessment",
                            aggregate_id=assessment.id,
                            idempotency_key=f"history-full:{claim.id.hex[:20]}",
                            payload={
                                "course_id": course.external_id,
                                "actor_external_subject": context.actor_external_subject,
                                "module": context.payload["module"],
                                "cmid": context.payload["cmid"],
                                "cursor": "0:0",
                                "limit": context.payload["limit"],
                                "priority_only": False,
                            },
                        )
                    )
                    full_scan_queued = True
            warnings = result.get("warnings", [])
            receipt = {
                "status": "DELIVERED",
                "created": stats.created,
                "updated": stats.updated,
                "unchanged": stats.unchanged,
                "skipped": stats.skipped,
                "warning_count": len(warnings) if isinstance(warnings, list) else 0,
                "complete": complete,
                "next_page_queued": next_page_queued,
                "full_scan_queued": full_scan_queued,
                "priority_only": context.payload.get("priority_only", False),
                "actor_external_subject": context.actor_external_subject,
            }
        else:
            if not isinstance(result, dict):
                raise IntegrationProtocolError("Moodle delivery receipt has an invalid shape")
            receipt = _bounded_receipt(result, settings)
            if isinstance(context, _CheckpointDelivery):
                await _record_lms_submission_fingerprint(
                    db,
                    row=row,
                    context=context,
                    result=result,
                )
                if closes_after_assignment_save:
                    if locked_attempt is None:
                        raise IntegrationProtocolError(
                            "Finalized Moodle Assignment attempt is missing"
                        )
                    await _mark_moodle_attempt_finalized(
                        db,
                        attempt=locked_attempt,
                        finalized_at=utcnow(),
                        delivered_event_id=row.id,
                    )
            if isinstance(context, _TaskVersionDelivery):
                await _apply_task_mirror_receipt(db, context, receipt)
        if browser_state is not None:
            await _persist_browser_context(
                db,
                settings,
                context.connection,
                browser_state,
            )
        row.state = SyncOutboxState.DELIVERED.value
        row.receipt = receipt
        row.last_error = ""
        row.delivered_at = utcnow()
        row.locked_at = None
        if isinstance(context, _GradeDelivery):
            if decision is not None:
                decision.lms_export_state = "DELIVERED"
            if submission is not None:
                submission.lms_export_state = "DELIVERED"
        return True


async def _record_lms_submission_fingerprint(
    db: AsyncSession,
    *,
    row: SyncOutbox,
    context: _CheckpointDelivery,
    result: dict[str, Any],
) -> None:
    """Persist an immutable attestation after every successful LMS delivery.

    Draft and periodic deliveries matter as much as the terminal delivery: if
    a learner later replaces the managed answer directly in Moodle, the
    historical read-back must still be able to prove that the LMS response is
    different from every answer this application successfully delivered.
    """

    payload = context.payload
    reason = str(payload.get("reason", ""))
    if reason not in {
        "ATTEMPT_STARTED",
        "PERIODIC",
        "FINAL_MINUTE",
        "SUBMISSION",
        "DEADLINE",
    }:
        return
    artifact = payload.get("artifact_bytes")
    if not isinstance(artifact, bytes) or row.attempt_id is None or row.course_id is None:
        return
    existing = await db.scalar(
        select(LMSSubmissionFingerprint.id).where(LMSSubmissionFingerprint.outbox_id == row.id)
    )
    if existing is not None:
        return
    attempt = await db.get(Attempt, row.attempt_id)
    if attempt is None:
        raise IntegrationProtocolError("Delivered answer attempt is missing")
    snapshot_id = payload.get("snapshot_ref")
    try:
        snapshot_uuid = uuid.UUID(str(snapshot_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise IntegrationProtocolError("Delivered answer snapshot reference is invalid") from exc
    submission = await db.scalar(
        select(Submission)
        .where(
            Submission.attempt_id == attempt.id,
            Submission.snapshot_id == snapshot_uuid,
        )
        .order_by(Submission.revision.desc())
    )
    answer_transport = str(payload.get("answer_transport", ""))
    compared, canonicalization = comparison_content(artifact, answer_transport)
    artifact_md5, artifact_sha256 = content_digests(artifact)
    comparison_md5, comparison_sha256 = content_digests(compared)
    remote_receipt = result.get("receipt")
    remote_receipt = remote_receipt if isinstance(remote_receipt, dict) else {}
    db.add(
        LMSSubmissionFingerprint(
            outbox_id=row.id,
            connection_id=row.connection_id,
            course_id=row.course_id,
            assessment_id=attempt.assessment_id,
            principal_id=attempt.principal_id,
            attempt_id=attempt.id,
            submission_id=submission.id if submission is not None else None,
            snapshot_id=snapshot_uuid,
            module=str(payload.get("module", ""))[:16],
            external_activity_id=str(payload.get("cmid", ""))[:64],
            external_attempt_id=str(remote_receipt.get("attempt_id", ""))[:160],
            external_question_slot=str(remote_receipt.get("question_slot", ""))[:64],
            answer_transport=answer_transport[:32],
            artifact_filename=str(payload.get("artifact_filename", ""))[:255],
            artifact_size=len(artifact),
            artifact_md5=artifact_md5,
            artifact_sha256=artifact_sha256,
            comparison_md5=comparison_md5,
            comparison_sha256=comparison_sha256,
            canonicalization=canonicalization,
            checkpoint_reason=reason,
            terminal=reason in {"SUBMISSION", "DEADLINE"},
            delivered_at=utcnow(),
        )
    )


async def _finish_blocked(
    session_factory: SessionFactory,
    claim: ClaimedOutboxEvent,
    reason: str,
) -> bool:
    async with session_factory() as db, db.begin():
        row = await _owned_claim(db, claim)
        if row is None:
            return False
        row.state = SyncOutboxState.BLOCKED.value
        row.last_error = reason[:4000]
        row.locked_at = None
        if row.event_type == "review.decision":
            decision = await db.get(ReviewDecision, row.aggregate_id)
            if decision is not None:
                decision.lms_export_state = (
                    "SUPERSEDED" if reason.startswith("SUPERSEDED:") else "BLOCKED"
                )
                submission = await db.scalar(
                    select(Submission)
                    .where(Submission.id == decision.submission_id)
                    .with_for_update()
                )
                latest = await db.scalar(
                    select(ReviewDecision)
                    .where(ReviewDecision.submission_id == decision.submission_id)
                    .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
                )
                if submission is not None and latest is not None and latest.id == decision.id:
                    submission.lms_export_state = decision.lms_export_state
        if row.event_type == "course.sync" and row.course_id is not None:
            course = await db.get(Course, row.course_id)
            if course is not None:
                course.sync_status = "FAILED"
                code, separator, message = reason.partition(":")
                _record_course_sync_error(
                    course,
                    code=code if separator else "SYNC_BLOCKED",
                    message=message.strip() if separator else reason,
                    retryable=False,
                )
        return True


async def _finish_failure(
    session_factory: SessionFactory,
    settings: Settings,
    claim: ClaimedOutboxEvent,
    error: Exception,
    *,
    now: datetime | None,
) -> bool:
    now = now or utcnow()
    # Browser-state credentials are deliberately leased exclusively.  With more
    # than one sync-worker lane, a second history/course event can therefore be
    # claimed while another event is using the same teacher session.  That is
    # local scheduling contention, not a failed Moodle delivery: charging it to
    # the retry budget made untouched history pages reach FAILED while the first
    # page was still being read.
    browser_busy = isinstance(error, IntegrationBusy)
    retryable = browser_busy or not isinstance(error, IntegrationError) or error.retryable
    max_attempts = max(1, int(getattr(settings, "sync_max_attempts", 8)))
    state = (
        SyncOutboxState.RETRY.value
        if browser_busy or (retryable and claim.attempts < max_attempts)
        else SyncOutboxState.FAILED.value
    )
    if isinstance(error, IntegrationError):
        detail = f"{error.code}: {error}"
    else:
        detail = f"UNEXPECTED_{error.__class__.__name__.upper()}"
    async with session_factory() as db, db.begin():
        row = await _owned_claim(db, claim)
        if row is None:
            return False
        row.state = state
        row.last_error = detail[:4000]
        row.locked_at = None
        if browser_busy:
            # ``claim_next_outbox_event`` increments attempts before preparing
            # the delivery.  Undo that increment so repeated contention can
            # never exhaust the external-delivery retry budget.
            row.attempts = max(0, row.attempts - 1)
        if state == SyncOutboxState.RETRY.value:
            delay_seconds = (
                min(5, max(1, int(getattr(settings, "sync_retry_base_seconds", 5))))
                if browser_busy
                else retry_delay_seconds(claim.attempts, settings)
            )
            if (
                claim.event_type == "attempt.checkpoint"
                and isinstance(claim.payload, dict)
                and claim.payload.get("reason") in {"SUBMISSION", "DEADLINE"}
            ):
                # The student is waiting behind the final submission screen.
                # Terminal delivery is idempotent and has only a handful of
                # retries, so keep this recovery window short instead of using
                # the hour-scale backoff intended for background imports.
                delay_seconds = min(delay_seconds, 5)
            row.next_attempt_at = now + timedelta(
                seconds=delay_seconds
            )
        if row.event_type == "review.decision":
            decision = await db.get(ReviewDecision, row.aggregate_id)
            if decision is not None:
                decision.lms_export_state = (
                    "RETRY" if state == SyncOutboxState.RETRY.value else "FAILED"
                )
                submission = await db.scalar(
                    select(Submission)
                    .where(Submission.id == decision.submission_id)
                    .with_for_update()
                )
                latest = await db.scalar(
                    select(ReviewDecision)
                    .where(ReviewDecision.submission_id == decision.submission_id)
                    .order_by(ReviewDecision.revision.desc(), ReviewDecision.created_at.desc())
                )
                if submission is not None and latest is not None and latest.id == decision.id:
                    submission.lms_export_state = decision.lms_export_state
        if row.event_type == "course.sync" and row.course_id is not None:
            course = await db.get(Course, row.course_id)
            if course is not None:
                _record_course_sync_error(
                    course,
                    code=(error.code if isinstance(error, IntegrationError) else detail),
                    message=(str(error) if str(error).strip() else detail),
                    retryable=retryable,
                )
                if state == SyncOutboxState.RETRY.value:
                    # A retryable connector/credential conflict is queue state,
                    # not a terminal course failure.  In particular,
                    # IntegrationBusy is expected when a history page owns the
                    # same teacher browser session for a bounded interval.
                    if isinstance(error, IntegrationBusy):
                        course.sync_status = "SYNCING"
                        _clear_course_sync_error(course)
                    else:
                        course.sync_status = "PENDING"
        return True


async def _owned_claim(
    db: AsyncSession,
    claim: ClaimedOutboxEvent,
) -> SyncOutbox | None:
    row = await db.scalar(select(SyncOutbox).where(SyncOutbox.id == claim.id).with_for_update())
    if (
        row is None
        or row.state != SyncOutboxState.PROCESSING.value
        or row.locked_at is None
        or not _same_instant(row.locked_at, claim.locked_at)
    ):
        return None
    return row


async def _apply_course_discovery(
    db: AsyncSession,
    course_id: uuid.UUID,
    discovery: CourseDiscovery,
    *,
    actor_external_subject: str,
) -> None:
    course = await db.scalar(select(Course).where(Course.id == course_id).with_for_update())
    if course is None or course.external_id != discovery.external_id:
        raise IntegrationProtocolError("Course discovery target changed during delivery")
    preview = discovery.preview
    course.title = _bounded_text(preview.get("title") or course.title, 255)
    course.short_name = _bounded_text(preview.get("short_name"), 120)
    course.external_revision = _bounded_text(preview.get("external_revision"), 255)
    course.starts_at = _epoch_datetime(preview.get("starts_at_epoch"))
    course.ends_at = _epoch_datetime(preview.get("ends_at_epoch"))
    course.sync_status = "CURRENT"
    _clear_course_sync_error(course)

    sections = preview.get("sections", [])
    if not isinstance(sections, list) or len(sections) > _MAX_LMS_SECTIONS:
        raise IntegrationProtocolError("Course section snapshot has an invalid shape")
    existing_sections = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseSection).where(CourseSection.course_id == course.id))
        ).all()
    }
    seen_sections: set[str] = set()
    activities: list[dict[str, Any]] = []
    for position, item in enumerate(sections):
        if not isinstance(item, dict) or not str(item.get("external_id", "")):
            raise IntegrationProtocolError("Course section snapshot has an invalid entry")
        external_id = _bounded_text(item["external_id"], 255)
        if not external_id or external_id in seen_sections:
            raise IntegrationProtocolError("Course section snapshot has duplicate identifiers")
        seen_sections.add(external_id)
        section = existing_sections.get(external_id)
        if section is None:
            section = CourseSection(course_id=course.id, external_id=external_id, title="")
            db.add(section)
        section.title = _bounded_text(item.get("title", ""), 255)
        raw_position = item.get("position")
        section.position = (
            raw_position
            if isinstance(raw_position, int) and not isinstance(raw_position, bool)
            else position
        )
        section.visible = bool(item.get("visible", True))
        section.external_revision = course.external_revision
        raw_activities = item.get("activities", [])
        if not isinstance(raw_activities, list):
            raise IntegrationProtocolError("Course activity snapshot has an invalid shape")
        for raw_activity in raw_activities:
            activity = _project_lms_activity(raw_activity, external_id)
            if activity is not None:
                activities.append(activity)
                if len(activities) > _MAX_LMS_ACTIVITIES:
                    raise IntegrationProtocolError("Course activity snapshot is too large")
    for external_id, section in existing_sections.items():
        if external_id not in seen_sections:
            section.visible = False

    policy_activities = [_activity_policy_projection(row) for row in activities]
    _bounded_canonical_json(
        policy_activities,
        maximum=_MAX_LMS_ACTIVITIES_BYTES,
        label="Course activity projection",
    )
    policies = dict(course.policies) if isinstance(course.policies, dict) else {}
    policies["lms_activities"] = policy_activities
    policies["lms_activity_revision"] = course.external_revision
    course.policies = policies
    await db.flush()
    actor = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == course.connection_id,
            ExternalPrincipal.external_subject == actor_external_subject,
        )
    )
    if actor is None:
        raise IntegrationProtocolError("Course synchronization actor is missing")
    await materialize_moodle_activity_drafts(
        db,
        course=course,
        activities=activities,
        created_by_id=actor.id,
    )
    await _apply_activity_deadlines(db, course, activities)

    snapshot = preview.get("membership_snapshot", {})
    members = snapshot.get("members", []) if isinstance(snapshot, dict) else None
    if not isinstance(members, list):
        raise IntegrationProtocolError("Course membership snapshot has an invalid shape")
    await _apply_memberships(
        db,
        course,
        members,
        str(preview.get("membership_revision", "")),
        complete=bool(snapshot.get("complete", True)),
    )
    # The current roster determines the authorized actor set for independent
    # history crawl chains.
    await enqueue_historical_submission_imports(
        db,
        course=course,
        actor_external_subject=actor_external_subject,
    )
    await db.flush()


def _record_course_sync_error(
    course: Course,
    *,
    code: object,
    message: object,
    retryable: bool,
) -> None:
    course.sync_error_code = _bounded_text(code, 64) or "SYNC_FAILED"
    course.sync_error_message = _bounded_text(message, 4_000)
    course.sync_error_at = utcnow()
    course.sync_error_retryable = bool(retryable)


def _clear_course_sync_error(course: Course) -> None:
    course.sync_error_code = ""
    course.sync_error_message = ""
    course.sync_error_at = None
    course.sync_error_retryable = False


def _project_lms_activity(
    raw: object,
    section_external_id: str,
) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    cmid = raw.get("cmid")
    if isinstance(cmid, bool) or not str(cmid).isdigit() or int(cmid) <= 0:
        return None
    module = _bounded_text(raw.get("module"), 32).lower()
    if not module or not all(char.isalnum() or char == "_" for char in module):
        return None
    instance_id = raw.get("instance_id")
    if isinstance(instance_id, bool) or not str(instance_id or 0).isdigit():
        instance_id = 0
    projected: dict[str, Any] = {
        "cmid": int(cmid),
        "instance_id": int(instance_id or 0),
        "module": module,
        "name": _bounded_text(raw.get("name"), 255),
        "visible": bool(raw.get("visible", True)),
        "user_visible": bool(raw.get("uservisible", True)),
        "section_external_id": section_external_id,
        "url": _bounded_text(raw.get("url"), 2_000),
    }
    for source, target in (
        ("opens_at", "opens_at_epoch"),
        ("due_at", "due_at_epoch"),
        ("cutoff_at", "cutoff_at_epoch"),
    ):
        value = raw.get(source)
        projected[target] = int(value) if isinstance(value, int) and value > 0 else 0
    grade = raw.get("grade_max")
    if (
        isinstance(grade, int | float)
        and not isinstance(grade, bool)
        and math.isfinite(float(grade))
    ):
        projected["grade_max"] = grade
    description = raw.get("description")
    if isinstance(description, str):
        projected["description"] = description.strip()[:50_000]
    for field, maximum in (
        ("duration_seconds", 31_536_000),
        ("attempt_limit", 100),
        ("question_count", 10_000),
        ("essay_question_count", 10_000),
        ("random_question_count", 10_000),
    ):
        value = raw.get(field)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and 0 <= value <= maximum
            and (field not in {"duration_seconds", "attempt_limit"} or value > 0)
        ):
            projected[field] = value
    projected["import_supported"] = bool(raw.get("import_supported", False))
    projected["random_essay_confirmed"] = raw.get("random_essay_confirmed") is True
    projected["statement_deferred"] = raw.get("statement_deferred") is True
    projected["attempt_limit_unlimited"] = raw.get("attempt_limit_unlimited") is True
    grading_method = str(raw.get("quiz_grading_method", "")).upper()
    grading_method_confirmed = raw.get(
        "quiz_grading_method_confirmed"
    ) is True and grading_method in {"HIGHEST", "AVERAGE", "FIRST", "LAST"}
    projected["quiz_grading_method_confirmed"] = grading_method_confirmed
    if grading_method_confirmed:
        projected["quiz_grading_method"] = grading_method
    projected["user_overrides_confirmed"] = raw.get("user_overrides_confirmed") is True
    raw_overrides = raw.get("user_overrides")
    normalized_overrides: list[dict[str, Any]] = []
    overrides_valid = isinstance(raw_overrides, list) and len(raw_overrides) <= 256
    if isinstance(raw_overrides, list):
        for raw_override in raw_overrides[:256]:
            if not isinstance(raw_override, dict):
                overrides_valid = False
                break
            user_id = str(raw_override.get("user_id", ""))
            override_id = raw_override.get("override_id")
            display_name = _bounded_text(raw_override.get("display_name"), 255)
            if (
                not user_id.isdigit()
                or int(user_id) <= 0
                or isinstance(override_id, bool)
                or not isinstance(override_id, int)
                or override_id <= 0
                or not display_name
                or raw_override.get("confirmed") is not True
            ):
                overrides_valid = False
                break
            normalized_override: dict[str, Any] = {
                "override_id": override_id,
                "user_id": user_id,
                "display_name": display_name,
                "confirmed": True,
            }
            for source, target in (
                ("opens_at", "opens_at_epoch"),
                ("due_at", "due_at_epoch"),
                ("cutoff_at", "cutoff_at_epoch"),
            ):
                value = raw_override.get(source)
                normalized_override[target] = (
                    int(value)
                    if isinstance(value, int) and not isinstance(value, bool) and value > 0
                    else 0
                )
            for field in (
                "opens_at_overridden",
                "due_at_overridden",
                "cutoff_at_overridden",
                "duration_overridden",
                "attempt_limit_overridden",
                "attempt_limit_unlimited",
            ):
                normalized_override[field] = raw_override.get(field) is True
            attempts = raw_override.get("attempt_limit")
            if isinstance(attempts, int) and not isinstance(attempts, bool) and 0 < attempts <= 100:
                normalized_override["attempt_limit"] = attempts
            duration = raw_override.get("duration_seconds")
            if (
                isinstance(duration, int)
                and not isinstance(duration, bool)
                and 0 < duration <= 31_536_000
            ):
                normalized_override["duration_seconds"] = duration
            normalized_overrides.append(normalized_override)
    if projected["user_overrides_confirmed"] and overrides_valid:
        projected["user_overrides"] = normalized_overrides
    else:
        projected["user_overrides"] = []
        projected["user_overrides_confirmed"] = False
    for field in (
        "title_confirmed",
        "settings_confirmed",
        "statement_confirmed",
        "schedule_confirmed",
        "duration_confirmed",
        "grade_confirmed",
        "attempt_policy_confirmed",
    ):
        projected[field] = raw.get(field) is True
    answer_transport = normalize_moodle_essay_answer_transport(raw.get("answer_transport"))
    if answer_transport is not None:
        projected["answer_transport"] = answer_transport
    for field in (
        "submission_drafts",
        "requires_submission_statement",
        "team_submission",
        "file_types_confirmed",
        "max_submission_bytes_inherited",
    ):
        if isinstance(raw.get(field), bool):
            projected[field] = raw[field]
    for field, maximum in (
        ("max_submission_files", 128),
        ("max_submission_bytes", 4 * 1024 * 1024 * 1024),
    ):
        value = raw.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= maximum:
            projected[field] = value
    accepted_file_types = raw.get("accepted_file_types")
    if isinstance(accepted_file_types, str):
        projected["accepted_file_types"] = accepted_file_types[:2_000]
    available = raw.get("available_answer_transports")
    if isinstance(available, list):
        normalized_available = [
            transport
            for item in available[:4]
            if (transport := normalize_moodle_essay_answer_transport(item)) is not None
        ]
        if len(normalized_available) == len(set(normalized_available)):
            projected["available_answer_transports"] = normalized_available
    return projected


def _activity_policy_projection(activity: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in activity.items() if key != "description"}


def _confirmed_activity_user_overrides(
    activity: dict[str, Any] | None,
) -> dict[str, dict[str, Any]] | None:
    """Return a complete override map, or ``None`` when it is not authoritative."""

    if not isinstance(activity, dict) or activity.get("user_overrides_confirmed") is not True:
        return None
    raw_rows = activity.get("user_overrides")
    if not isinstance(raw_rows, list) or len(raw_rows) > 256:
        return None
    overrides: dict[str, dict[str, Any]] = {}
    for raw in raw_rows:
        if not isinstance(raw, dict) or raw.get("confirmed") is not True:
            return None
        user_id = str(raw.get("user_id", ""))
        if not user_id.isdigit() or int(user_id) <= 0 or user_id in overrides:
            return None
        overrides[user_id] = raw
    return overrides


def _moodle_override_rule_values(
    activity: dict[str, Any],
    override: dict[str, Any],
    *,
    module: str,
) -> tuple[datetime | None, datetime | None, int | None, int | None]:
    """Project only explicit Moodle override values into an availability rule."""

    opens_at = (
        _epoch_datetime(override.get("opens_at_epoch"))
        if override.get("opens_at_overridden") is True
        else None
    )
    if module == "assign":
        global_cutoff = _epoch_datetime(activity.get("cutoff_at_epoch"))
        if override.get("cutoff_at_overridden") is True:
            closes_at = _epoch_datetime(override.get("cutoff_at_epoch"))
        elif global_cutoff is None and override.get("due_at_overridden") is True:
            closes_at = _epoch_datetime(override.get("due_at_epoch"))
        else:
            closes_at = None
    else:
        closes_at = (
            _epoch_datetime(override.get("due_at_epoch"))
            if override.get("due_at_overridden") is True
            else None
        )
    duration_seconds: int | None = None
    if override.get("duration_overridden") is True:
        raw_duration = override.get("duration_seconds")
        if (
            isinstance(raw_duration, int)
            and not isinstance(raw_duration, bool)
            and 0 < raw_duration <= 31_536_000
        ):
            duration_seconds = raw_duration
    attempt_limit: int | None = None
    if override.get("attempt_limit_overridden") is True:
        if override.get("attempt_limit_unlimited") is True:
            attempt_limit = 0
        else:
            raw_attempts = override.get("attempt_limit")
            if (
                isinstance(raw_attempts, int)
                and not isinstance(raw_attempts, bool)
                and 0 < raw_attempts <= 100
            ):
                attempt_limit = raw_attempts
    return opens_at, closes_at, duration_seconds, attempt_limit


async def _refresh_managed_principal_availability_rules(
    db: AsyncSession,
    *,
    assessment: Assessment,
    module: str,
    activity: dict[str, Any] | None,
) -> None:
    """Refresh published Moodle principal targets without broadening access.

    Moodle-managed publication is the only writer allowed to create rules for
    an imported assessment. Synchronization therefore updates existing numeric
    principal targets only. It never creates a rule for a newly discovered
    override. Missing or incomplete override evidence disables those existing
    targets until a later authoritative synchronization restores them.
    """

    rules = list(
        (
            await db.scalars(
                select(AvailabilityRule)
                .where(
                    AvailabilityRule.assessment_id == assessment.id,
                    AvailabilityRule.target_type == AvailabilityTarget.PRINCIPAL.value,
                )
                .with_for_update()
            )
        ).all()
    )
    if not rules:
        return
    overrides = _confirmed_activity_user_overrides(activity)
    for rule in rules:
        # Moodle subjects are positive integer user IDs. A UUID/string target
        # may be a legacy local rule and must not be rewritten by LMS sync.
        if not rule.target_external_id.isdigit() or int(rule.target_external_id) <= 0:
            continue
        override = overrides.get(rule.target_external_id) if overrides is not None else None
        if override is None or activity is None:
            rule.allowed = False
            rule.opens_at = None
            rule.closes_at = None
            rule.duration_seconds = None
            rule.attempt_limit = None
            continue
        (
            rule.opens_at,
            rule.closes_at,
            rule.duration_seconds,
            rule.attempt_limit,
        ) = _moodle_override_rule_values(activity, override, module=module)
        rule.allowed = True


async def _apply_activity_deadlines(
    db: AsyncSession,
    course: Course,
    activities: list[dict[str, Any]],
) -> None:
    """Apply schedules only through explicit Assessment -> Moodle mappings."""

    by_activity = {
        (str(item.get("module", "")).removeprefix("mod_"), str(item["cmid"])): item
        for item in activities
        if str(item.get("module", "")).removeprefix("mod_") in {"assign", "quiz"}
        and item.get("cmid")
    }
    sections = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseSection).where(CourseSection.course_id == course.id))
        ).all()
    }
    mappings = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                )
            )
        ).all()
    )
    for mapping in mappings:
        module = _moodle_activity_mapping_module(mapping)
        if module not in {"assign", "quiz"}:
            continue
        assessment = await db.get(Assessment, mapping.local_id)
        if assessment is None or assessment.course_id != course.id:
            continue
        # Older releases projected Moodle user overrides into PRINCIPAL rules.
        # Publication is group-only now and Moodle performs the per-student
        # check live, so retaining those rows would create a second authority.
        await db.execute(
            delete(AvailabilityRule).where(
                AvailabilityRule.assessment_id == assessment.id,
                AvailabilityRule.target_type == AvailabilityTarget.PRINCIPAL.value,
            )
        )
        metadata = dict(mapping.metadata_json or {})
        cmid = str(metadata.get("cmid", mapping.external_id))
        activity = by_activity.get((module, cmid))
        mapping.external_revision = course.external_revision
        if activity is None:
            # Keep the last known activity and deadlines for incident recovery/audit.
            metadata["sync_state"] = "MISSING_IN_MOODLE"
            mapping.metadata_json = metadata
            continue
        answer_transport = confirmed_moodle_activity_answer_transport(
            activity,
            module=module,
        )
        source_confirmation = moodle_source_confirmation_from_activity(activity)
        source_confirmed = moodle_source_is_confirmed(source_confirmation)
        metadata["submission_mode"] = answer_transport or "REQUIRES_CONFIGURATION"
        metadata["moodle_source_confirmation"] = source_confirmation
        sync_state = (
            "CURRENT"
            if answer_transport is not None and source_confirmed
            else (
                "MOODLE_SOURCE_UNCONFIRMED"
                if not source_confirmed
                else "ANSWER_TRANSPORT_UNSUPPORTED"
            )
        )
        if module == "assign":
            grade_max = positive_decimal(activity.get("grade_max"))
            if "grade_max" in activity and grade_max is None:
                sync_state = "UNSUPPORTED_MOODLE_GRADING"
            elif grade_max is not None and grade_max != assessment.max_score:
                sync_state = "GRADE_RANGE_MISMATCH"
        metadata.update({"activity": activity, "sync_state": sync_state})
        mapping.metadata_json = metadata
        if not bool(metadata.get("sync_deadlines", True)):
            continue
        if not source_confirmation["schedule"]:
            continue
        opens_at = _epoch_datetime(activity.get("opens_at_epoch"))
        closes_at = _epoch_datetime(activity.get("cutoff_at_epoch")) or _epoch_datetime(
            activity.get("due_at_epoch")
        )
        if opens_at is not None and closes_at is not None and closes_at <= opens_at:
            metadata["sync_state"] = "INVALID_MOODLE_WINDOW"
            mapping.metadata_json = metadata
            continue
        # A confirmed zero means Moodle explicitly disabled that boundary.
        assessment.opens_at = opens_at
        assessment.closes_at = closes_at
        section = sections.get(str(activity.get("section_external_id", "")))
        if section is not None:
            assessment.section_id = section.id
        attempts = list(
            (
                await db.scalars(
                    select(Attempt).where(
                        Attempt.assessment_id == assessment.id,
                        Attempt.state == AttemptState.ACTIVE.value,
                    )
                )
            ).all()
        )
        for attempt in attempts:
            assessment_policy = assessment.policy if isinstance(assessment.policy, dict) else {}
            if assessment_policy.get("moodle_metadata_read_only") is True:
                # A course-wide date cannot represent a user override.  The
                # live Moodle attempt is checked on every browser checkpoint;
                # keep only its approximate timer in expected_end_at.
                attempt.deadline_at = None
                continue
            if closes_at is None:
                attempt.deadline_at = attempt.expected_end_at
                continue
            candidates = [_as_utc(closes_at)]
            if attempt.expected_end_at is not None:
                candidates.append(_as_utc(attempt.expected_end_at))
            attempt.deadline_at = min(candidates)


async def _apply_task_mirror_receipt(
    db: AsyncSession,
    context: _TaskVersionDelivery,
    receipt: dict[str, Any],
) -> None:
    if str(receipt.get("status", "")).upper() != "DELIVERED":
        raise IntegrationProtocolError("Moodle task mirror delivery was not acknowledged")
    remote = receipt.get("receipt")
    if not isinstance(remote, dict) or str(remote.get("status", "")).upper() != "MIRRORED":
        raise IntegrationProtocolError("Moodle task mirror receipt has an invalid status")
    mirror_id = remote.get("mirrorid")
    if isinstance(mirror_id, bool) or not isinstance(mirror_id, int) or mirror_id <= 0:
        raise IntegrationProtocolError("Moodle task mirror receipt has an invalid identifier")
    external_id = str(mirror_id)
    external_type = "local_programming_bridge_task"
    conflicts = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == context.connection.id,
                    ExternalMapping.external_type == external_type,
                    ExternalMapping.external_id == external_id,
                )
            )
        ).all()
    )
    if any(row.local_id != context.task_version_id for row in conflicts):
        raise IntegrationProtocolError("Moodle task mirror identifier is already mapped")
    candidates = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == context.connection.id,
                    ExternalMapping.local_type == "TaskVersion",
                    ExternalMapping.local_id == context.task_version_id,
                    ExternalMapping.external_type == external_type,
                )
            )
        ).all()
    )
    mapping = next(
        (
            row
            for row in candidates
            if str(row.metadata_json.get("course_local_id", "")) == str(context.course_id)
        ),
        None,
    )
    if mapping is None:
        mapping = (
            conflicts[0]
            if conflicts
            else ExternalMapping(
                connection_id=context.connection.id,
                local_type="TaskVersion",
                local_id=context.task_version_id,
                external_type=external_type,
                external_id=external_id,
            )
        )
        if not conflicts:
            db.add(mapping)
    elif mapping.external_id != external_id:
        raise IntegrationProtocolError("Moodle task mirror identifier changed unexpectedly")
    mapping.external_revision = str(context.payload["definition_sha256"])
    mapping.metadata_json = {
        "course_local_id": str(context.course_id),
        "task_item_id": str(context.task_item_id),
        "task_ref": context.payload["task_ref"],
        "version": context.payload["version"],
        "content_hash": context.payload["content_hash"],
        "definition_sha256": context.payload["definition_sha256"],
        "status": context.payload["status"],
        "sync_state": "CURRENT",
        "receipt": receipt,
    }


async def _apply_memberships(
    db: AsyncSession,
    course: Course,
    members: list[Any],
    revision: str,
    *,
    complete: bool = True,
) -> None:
    group_specs: dict[str, str] = {}
    for member in members:
        if not isinstance(member, dict):
            continue
        for group in member.get("groups", []):
            if not isinstance(group, dict):
                continue
            external_group_id = str(group.get("id", group.get("external_id", ""))).strip()
            if external_group_id:
                group_specs[external_group_id] = str(group.get("name", ""))
    existing_groups = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseGroup).where(CourseGroup.course_id == course.id))
        ).all()
    }
    group_rows: dict[str, CourseGroup] = {}
    for external_id, name in group_specs.items():
        group = existing_groups.get(external_id)
        if group is None:
            group = CourseGroup(course_id=course.id, external_id=external_id, name=name)
            db.add(group)
        group.name = name
        group.active = True
        group_rows[external_id] = group
    if complete:
        for external_id, group in existing_groups.items():
            if external_id not in group_specs:
                group.active = False
    await db.flush()

    memberships = list(
        (
            await db.scalars(
                select(CourseMembership).where(CourseMembership.course_id == course.id)
            )
        ).all()
    )
    membership_by_key = {(row.principal_id, row.role): row for row in memberships}
    seen_memberships: set[tuple[uuid.UUID, str]] = set()
    for member in members:
        if not isinstance(member, dict):
            continue
        external_subject = str(member.get("user_id", member.get("id", ""))).strip()
        if not external_subject:
            continue
        principal = await db.scalar(
            select(ExternalPrincipal).where(
                ExternalPrincipal.connection_id == course.connection_id,
                ExternalPrincipal.external_subject == external_subject,
            )
        )
        if principal is None:
            principal = ExternalPrincipal(
                connection_id=course.connection_id,
                external_subject=external_subject,
                display_name=_member_name(member, external_subject),
            )
            db.add(principal)
            await db.flush()
        else:
            principal.display_name = _member_name(member, external_subject)
        principal.email = str(member.get("email", principal.email or ""))
        principal.profile_revision = revision

        suspended = bool(member.get("suspended", False))
        teacher_token = await teacher_token_for_principal(db, principal.id)
        effective_roles = (
            CourseRole.TEACHER.value if teacher_token is not None else CourseRole.STUDENT.value,
        )
        for role in effective_roles:
            await db.execute(
                update(CourseMembership)
                .where(
                    CourseMembership.course_id == course.id,
                    CourseMembership.principal_id == principal.id,
                    CourseMembership.role != role,
                    CourseMembership.active.is_(True),
                )
                .values(active=False, synced_at=utcnow())
            )
            key = (principal.id, role)
            membership = membership_by_key.get(key)
            if membership is None:
                membership = CourseMembership(
                    course_id=course.id,
                    principal_id=principal.id,
                    role=role,
                )
                db.add(membership)
                await db.flush()
                membership_by_key[key] = membership
            membership.active = not suspended
            membership.external_revision = (
                teacher_membership_revision(teacher_token.id, revision)
                if teacher_token is not None
                else revision
            )
            membership.synced_at = utcnow()
            seen_memberships.add(key)
            existing_membership_group_ids: set[uuid.UUID] = set()
            if complete:
                await db.execute(
                    delete(CourseMembershipGroup).where(
                        CourseMembershipGroup.coursemembership_id == membership.id
                    )
                )
            else:
                # A bounded Moodle roster can be explicitly incomplete (for
                # example, course 549 exposes at least 5000 rows).  Such a
                # snapshot must not delete unseen memberships or group links,
                # but every row that *was* returned is still authoritative
                # positive evidence and can safely be merged.
                existing_membership_group_ids = set(
                    (
                        await db.scalars(
                            select(CourseMembershipGroup.coursegroup_id).where(
                                CourseMembershipGroup.coursemembership_id == membership.id
                            )
                        )
                    ).all()
                )
            if not suspended:
                for raw_group in member.get("groups", []):
                    external_group_id = (
                        str(raw_group.get("id", raw_group.get("external_id", ""))).strip()
                        if isinstance(raw_group, dict)
                        else ""
                    )
                    group = group_rows.get(external_group_id)
                    if group is None or group.id in existing_membership_group_ids:
                        continue
                    db.add(
                        CourseMembershipGroup(
                            coursemembership_id=membership.id,
                            coursegroup_id=group.id,
                        )
                    )
                    existing_membership_group_ids.add(group.id)
    if complete:
        for membership in memberships:
            if (membership.principal_id, membership.role) not in seen_memberships:
                membership.active = False
                membership.external_revision = revision
                membership.synced_at = utcnow()
    # Callers inspect the refreshed scope in the same transaction (and the
    # history importer can run immediately after course discovery).  Flush the
    # positive group links here so a partial roster becomes usable without
    # waiting for a later commit/autoflush boundary.
    await db.flush()


def _member_roles(member: dict[str, Any]) -> tuple[str, ...]:
    raw = member.get("roles", member.get("role", []))
    values = raw if isinstance(raw, list) else [raw]
    roles: list[str] = []
    for value in values:
        normalized = str(value).upper().replace("-", "_")
        if normalized in {"TEACHER", "INSTRUCTOR", "EDITINGTEACHER", "TEACHING_ASSISTANT"}:
            role = CourseRole.TEACHER.value
        elif normalized in {"STUDENT", "LEARNER"}:
            role = CourseRole.STUDENT.value
        else:
            continue
        if role not in roles:
            roles.append(role)
    return tuple(roles)


def _member_name(member: dict[str, Any], fallback: str) -> str:
    for key in ("display_name", "fullname", "full_name", "name", "username"):
        value = str(member.get(key, "")).strip()
        if value:
            return value[:255]
    return fallback[:255]


async def _release_browser_context(
    session_factory: SessionFactory,
    context: DeliveryContext,
) -> None:
    target = context.connection
    if target.transport != "PLAYWRIGHT" or not (
        target.browser_credential_id and target.browser_lease_owner
    ):
        return
    async with session_factory() as db, db.begin():
        await db.execute(
            update(MoodleCredential)
            .where(
                MoodleCredential.id == target.browser_credential_id,
                MoodleCredential.lease_owner == target.browser_lease_owner,
            )
            .values(lease_owner=None, lease_expires_at=None)
            .execution_options(synchronize_session=False)
        )


async def _release_browser_context_best_effort(
    session_factory: SessionFactory,
    context: DeliveryContext,
) -> None:
    """Release a browser lease even while the worker task is being cancelled."""

    release_task = asyncio.create_task(_release_browser_context(session_factory, context))
    try:
        await asyncio.shield(release_task)
    except asyncio.CancelledError:
        # The caller will re-raise its cancellation after the lease cleanup completes.
        try:
            await asyncio.shield(release_task)
        except BaseException:
            pass
    except Exception:
        # Lease expiration remains the final recovery mechanism if the database is unavailable.
        pass


async def _invalidate_browser_context(
    session_factory: SessionFactory,
    context: DeliveryContext,
) -> None:
    """Expire a leased browser state after Moodle rejects the authenticated page."""

    target = context.connection
    if target.transport != "PLAYWRIGHT" or not (
        target.browser_credential_id and target.browser_lease_owner
    ):
        return
    now = utcnow()
    async with session_factory() as db, db.begin():
        await db.execute(
            update(MoodleCredential)
            .where(
                MoodleCredential.id == target.browser_credential_id,
                MoodleCredential.lease_owner == target.browser_lease_owner,
            )
            .values(
                status="EXPIRED",
                expires_at=now,
                lease_owner=None,
                lease_expires_at=None,
            )
            .execution_options(synchronize_session=False)
        )


async def _invalidate_browser_context_best_effort(
    session_factory: SessionFactory,
    context: DeliveryContext,
) -> None:
    invalidate_task = asyncio.create_task(_invalidate_browser_context(session_factory, context))
    try:
        await asyncio.shield(invalidate_task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(invalidate_task)
        except BaseException:
            pass
    except Exception:
        # Lease expiry still recovers the worker if the database is unavailable.
        pass


async def _close_client_best_effort(client: httpx.AsyncClient) -> None:
    """Close an owned HTTP client without losing an otherwise durable delivery result."""

    try:
        await client.aclose()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _persist_browser_context(
    db: AsyncSession,
    settings: Settings,
    target: ConnectionTarget,
    storage_state: dict[str, Any],
) -> None:
    if (
        target.transport != "PLAYWRIGHT"
        or target.principal_id is None
        or target.browser_credential_id is None
        or target.browser_credential_revision is None
        or target.browser_lease_owner is None
    ):
        raise IntegrationProtocolError("Moodle browser credential lease is incomplete")
    try:
        encrypted = encrypt_moodle_browser_state(
            storage_state,
            settings,
            connection_id=target.id,
            principal_id=target.principal_id,
        )
    except ValueError as exc:
        raise IntegrationProtocolError("Moodle browser returned an invalid session state") from exc
    now = utcnow()
    updated = await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == target.browser_credential_id,
            MoodleCredential.lease_owner == target.browser_lease_owner,
            MoodleCredential.revision == target.browser_credential_revision,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
        )
        .values(
            encrypted_secret=encrypted,
            revision=target.browser_credential_revision + 1,
            lease_owner=None,
            lease_expires_at=None,
            last_used_at=now,
            last_verified_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:  # type: ignore[attr-defined]
        raise IntegrationUnavailable("Moodle browser session changed during delivery")


async def _principal_credential_target(
    db: AsyncSession,
    settings: Settings,
    connection: ConnectionTarget,
    principal_id: uuid.UUID,
) -> ConnectionTarget:
    if connection.mode != "PLUGINLESS":
        return connection
    if connection.transport == "PLAYWRIGHT":
        return await _principal_browser_credential_target(
            db,
            settings,
            connection,
            principal_id,
        )
    credential = await db.scalar(
        select(MoodleCredential).where(
            MoodleCredential.connection_id == connection.id,
            MoodleCredential.principal_id == principal_id,
            MoodleCredential.kind == "MOBILE_TOKEN",
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
            or_(
                MoodleCredential.expires_at.is_(None),
                MoodleCredential.expires_at > utcnow(),
            ),
        )
    )
    if credential is None:
        raise _BlockedDelivery("LMS_REAUTH_REQUIRED", "Moodle reauthentication is required")
    try:
        token = decrypt_moodle_credential(
            credential.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=principal_id,
            kind=credential.kind,
        )
    except CredentialDecryptionError as exc:
        raise _BlockedDelivery(
            "LMS_REAUTH_REQUIRED", "Stored Moodle credential cannot be opened"
        ) from exc
    return replace(connection, service_token=token, principal_id=principal_id)


async def _principal_browser_credential_target(
    db: AsyncSession,
    settings: Settings,
    connection: ConnectionTarget,
    principal_id: uuid.UUID,
) -> ConnectionTarget:
    """Atomically lease and decrypt one Playwright session before external I/O."""

    now = utcnow()
    credential = await db.scalar(
        select(MoodleCredential)
        .where(
            MoodleCredential.connection_id == connection.id,
            MoodleCredential.principal_id == principal_id,
            MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
        )
        .with_for_update()
    )
    if (
        credential is None
        or credential.status != "ACTIVE"
        or credential.revoked_at is not None
        or (credential.expires_at is not None and _as_utc(credential.expires_at) <= _as_utc(now))
    ):
        await db.rollback()
        raise _BlockedDelivery("LMS_REAUTH_REQUIRED", "Moodle reauthentication is required")
    if (
        credential.lease_owner
        and credential.lease_expires_at is not None
        and _as_utc(credential.lease_expires_at) > _as_utc(now)
    ):
        await db.rollback()
        raise IntegrationBusy("Moodle browser session is busy")
    try:
        state = decrypt_moodle_browser_state(
            credential.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=principal_id,
        )
    except CredentialDecryptionError as exc:
        await db.rollback()
        raise _BlockedDelivery(
            "LMS_REAUTH_REQUIRED", "Stored Moodle browser session cannot be opened"
        ) from exc

    revision = credential.revision
    owner = uuid.uuid4().hex
    lease_seconds = max(
        30,
        min(
            615,
            math.ceil(float(getattr(settings, "moodle_browser_http_timeout_seconds", 45))) + 15,
        ),
    )
    updated = await db.execute(
        update(MoodleCredential)
        .where(
            MoodleCredential.id == credential.id,
            MoodleCredential.revision == revision,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
            or_(MoodleCredential.expires_at.is_(None), MoodleCredential.expires_at > now),
            or_(
                MoodleCredential.lease_owner.is_(None),
                MoodleCredential.lease_expires_at.is_(None),
                MoodleCredential.lease_expires_at <= now,
            ),
        )
        .values(
            lease_owner=owner,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
        )
        .execution_options(synchronize_session=False)
    )
    if updated.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise IntegrationBusy("Moodle browser session is busy")
    credential_id = credential.id
    await db.commit()
    return replace(
        connection,
        principal_id=principal_id,
        browser_state=state,
        browser_credential_id=credential_id,
        browser_credential_revision=revision,
        browser_lease_owner=owner,
    )


def _connection_target(settings: Settings, connection: LMSConnection) -> ConnectionTarget:
    config = connection.config if isinstance(connection.config, dict) else {}
    mode = moodle_auth_mode(connection)
    token: str | None = None
    if mode == "BRIDGE":
        env_name = config.get("service_token_env")
        if isinstance(env_name, str) and env_name:
            token = os.environ.get(env_name)
            if not token:
                raise _BlockedDelivery(
                    "CONNECTION_SECRET_MISSING",
                    "Configured Moodle service token is unavailable",
                )
        elif settings.moodle_service_token.get_secret_value():
            token = settings.moodle_service_token.get_secret_value()
    transport = moodle_pluginless_transport(connection) if mode == "PLUGINLESS" else "BRIDGE"
    return ConnectionTarget(
        id=connection.id,
        base_url=connection.base_url,
        service_token=token,
        mode=mode,
        transport=transport,
    )


def _is_mod_assign_mapping(mapping: ExternalMapping) -> bool:
    return _moodle_activity_mapping_module(mapping) == "assign"


def _is_mod_quiz_essay_mapping(mapping: ExternalMapping) -> bool:
    return _moodle_quiz_answer_transport(mapping) is not None


def _moodle_quiz_answer_transport(mapping: ExternalMapping) -> str | None:
    if _moodle_activity_mapping_module(mapping) != "quiz":
        return None
    transport = _moodle_activity_answer_transport(mapping)
    return transport if transport in {"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"} else None


def _moodle_activity_answer_transport(mapping: ExternalMapping) -> str | None:
    module = _moodle_activity_mapping_module(mapping)
    if module not in {"assign", "quiz"}:
        return None
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    # Only a successful synchronization confirms a live delivery contract.
    # Legacy rows without an explicit marker must be refreshed, never guessed.
    if metadata.get("sync_state") != "CURRENT":
        return None
    transport = normalize_moodle_essay_answer_transport(metadata.get("submission_mode"))
    expected = (
        {"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"}
        if module == "quiz"
        else {"ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"}
    )
    return transport if transport in expected else None


def _moodle_activity_mapping_module(mapping: ExternalMapping) -> str | None:
    if mapping.local_type.lower() not in {"assessment", "core.assessment"}:
        return None
    external_type = mapping.external_type.lower().replace("-", "_")
    external_modules = {
        "assign": "assign",
        "mod_assign": "assign",
        "moodle_assignment": "assign",
        "moodle_mod_assign": "assign",
        "quiz": "quiz",
        "mod_quiz": "quiz",
        "moodle_quiz": "quiz",
        "moodle_mod_quiz": "quiz",
    }
    external_module = external_modules.get(external_type)
    if external_module is None:
        return None
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    raw_module = str(metadata.get("module", external_module)).lower().replace("-", "_")
    module = raw_module.removeprefix("mod_")
    return module if module == external_module and module in {"assign", "quiz"} else None


def _positive_integer(value: Any, label: str) -> int:
    text = str(value).strip()
    if not text.isdigit() or int(text) <= 0:
        raise _BlockedDelivery("INVALID_EXTERNAL_ID", f"{label} must be a positive integer")
    return int(text)


def _score_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


_RUSSIAN_SURNAME_SUFFIXES = (
    "ов",
    "ев",
    "ёв",
    "ин",
    "ын",
    "ова",
    "ева",
    "ёва",
    "ина",
    "ына",
    "ский",
    "цкий",
    "ская",
    "цкая",
    "енко",
    "ко",
    "ук",
    "юк",
    "ич",
    "ян",
    "дзе",
    "швили",
)


def _looks_like_russian_surname(value: str) -> bool:
    normalized = value.casefold().strip(" .,-")
    return len(normalized) >= 4 and normalized.endswith(_RUSSIAN_SURNAME_SUFFIXES)


def _reviewer_signature(display_name: str) -> str:
    parts = [part.strip(" ,") for part in display_name.split() if part.strip(" ,")]
    if not parts:
        return "Преподаватель"
    # Older pluginless sessions may still carry a theme avatar prefix such as
    # ``АК Алексей Коваленко``.  It is redundant only when it exactly matches
    # the initials of the following words; never discard an arbitrary short
    # name.  This also repairs signatures without requiring the user to log out
    # immediately after deploying the canonical Moodle identity parser.
    if len(parts) >= 3:
        compact = parts[0].replace(".", "")
        expected = "".join(part[0] for part in parts[1:] if part and part[0].isalpha())
        if compact.isalpha() and compact.casefold() == expected[: len(compact)].casefold():
            parts = parts[1:]
    if len(parts) == 1:
        return parts[0]

    # Current MMCS parsing stores ``Фамилия Имя``.  Previous revisions stored
    # some principals as ``Имя Фамилия``.  Recognise the latter conservatively
    # from common Russian surname endings so an already active database session
    # still emits the required ``Фамилия И.`` attribution.  Ambiguous and
    # non-Russian names retain the stable surname-first contract.
    surname_index = 0
    if _looks_like_russian_surname(parts[-1]) and not _looks_like_russian_surname(parts[0]):
        surname_index = len(parts) - 1
    surname = parts[surname_index]
    given_names = [part for index, part in enumerate(parts) if index != surname_index]
    initial = next((part[0] for part in given_names if part and part[0].isalpha()), "")
    return f"{surname} {initial}." if initial else surname


def _comment_with_reviewer_signature(comment: str, signature: str) -> str:
    # Retries and subsequent revisions may already contain the signature copied
    # from Moodle. Keep one standalone attribution line and place it last.
    lines = [line for line in comment.splitlines() if line.strip() != signature]
    body = "\n".join(lines).strip()
    separator = "\n\n" if body else ""
    available = max(0, 20_000 - len(separator) - len(signature))
    body = body[:available].rstrip()
    separator = "\n\n" if body else ""
    return f"{body}{separator}{signature}"


def _matching_positive_external_value(
    receipt: dict[str, Any],
    metadata: dict[str, Any],
    key: str,
    label: str,
    *,
    fallback_key: str | None = None,
) -> int:
    receipt_value = receipt.get(key)
    metadata_value = metadata.get(key)
    if receipt_value in {None, ""} and fallback_key is not None:
        receipt_value = receipt.get(fallback_key)
    if metadata_value in {None, ""} and fallback_key is not None:
        metadata_value = metadata.get(fallback_key)
    receipt_number = _positive_integer(receipt_value, label)
    metadata_number = _positive_integer(metadata_value, label)
    if receipt_number != metadata_number:
        raise _BlockedDelivery("MAPPING_NOT_CONFIRMED", f"{label} mapping is inconsistent")
    return receipt_number


def _confirmed_historical_assignment_attempt_number(
    *,
    mapping: ExternalMapping,
    metadata: dict[str, Any],
    receipt: dict[str, Any],
    course: Course,
    assessment: Assessment,
    principal: ExternalPrincipal,
) -> tuple[int, int]:
    """Return the exact reopened Assignment attempt proven by two import records.

    Moodle's Assignment grader defaults to the current attempt when
    ``attemptnumber`` is omitted.  That is unsafe for historical/reopened
    submissions: the local immutable response, its historical mapping and the
    Moodle user/attempt identity must all agree before a grade is exported.
    """

    if str(metadata.get("course_id", "")) != str(course.external_id) or str(
        metadata.get("assessment_id", "")
    ) != str(assessment.id):
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Historical Moodle Assignment ownership mapping is inconsistent",
        )
    cmid = _positive_integer(metadata.get("cmid"), "Historical Moodle Assignment cmid")
    if str(receipt.get("lms_module", "")).removeprefix("mod_").lower() != "assign" or str(
        receipt.get("lms_cmid", "")
    ) != str(cmid):
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Historical Moodle Assignment response mapping is inconsistent",
        )

    attempt_id = str(receipt.get("moodle_parent_attempt_id", "")).strip()
    mapped_attempt_id = str(metadata.get("moodle_parent_attempt_id", "")).strip()
    match = re.fullmatch(r"user-([1-9][0-9]*)-attempt-([0-9]{1,7})", attempt_id)
    if match is None:
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Historical Moodle Assignment attempt mapping is invalid",
        )
    user_id = _positive_integer(principal.external_subject, "Moodle user id")
    attempt_number = int(match.group(2))
    if (
        int(match.group(1)) != user_id
        or attempt_number > 1_000_000
        or (mapped_attempt_id and mapped_attempt_id != attempt_id)
    ):
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Historical Moodle Assignment attempt mapping is inconsistent",
        )

    expected_external_id = f"assign:{cmid}:{attempt_id}"
    if (
        receipt.get("external_id") != mapping.external_id
        or mapping.external_id != expected_external_id
    ):
        raise _BlockedDelivery(
            "MAPPING_NOT_CONFIRMED",
            "Historical Moodle Assignment submission mapping is inconsistent",
        )
    return cmid, attempt_number


def _task_version_content(version: TaskVersion) -> dict[str, Any]:
    return {
        "title": version.title,
        "statement": version.statement,
        "language": version.language,
        "language_standard": version.language_standard,
        "multi_file": version.multi_file,
        "starter_files": version.starter_files,
        "build_profile": version.build_profile,
        "public_examples": version.public_examples,
        "hidden_test_manifest": version.hidden_test_manifest,
        "max_score": _score_text(version.max_score),
        "difficulty": version.difficulty,
        "ai_policy": version.ai_policy,
    }


def _task_mirror_definition(item: TaskBankItem, version: TaskVersion) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_ref": str(item.id),
        "slug": item.slug,
        "category": item.category,
        "tags": item.tags,
        "version": version.number,
        "content_hash": version.content_hash.lower(),
        **_task_version_content(version),
    }


def _same_task_payload(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    version = actual.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        return False
    for key in (
        "course_id",
        "task_ref",
        "version",
        "content_hash",
        "definition_sha256",
        "definition_json",
        "status",
    ):
        if actual.get(key) != expected[key]:
            return False
    return True


def _bounded_canonical_json(value: Any, *, maximum: int, label: str) -> str:
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        encoded = serialized.encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IntegrationProtocolError(f"{label} is not valid JSON") from exc
    if len(encoded) > maximum:
        raise IntegrationProtocolError(f"{label} exceeds the configured size limit")
    return serialized


def _bounded_text(value: Any, maximum: int) -> str:
    return str(value if value is not None else "").strip()[:maximum]


def _is_sha256(value: Any, *, allow_empty: bool = False) -> bool:
    if allow_empty and value == "":
        return True
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdefABCDEF" for char in value)
    )


def _required_hash(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    label: str,
    *,
    allow_empty: bool = False,
) -> str:
    for key in keys:
        if key in payload:
            value = payload[key]
            if not _is_sha256(value, allow_empty=allow_empty):
                break
            return str(value).lower()
    raise IntegrationProtocolError(f"Checkpoint {label} is invalid")


def _required_nonnegative_integer(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    label: str,
) -> int:
    for key in keys:
        if key in payload:
            value = payload[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                break
            return value
    raise IntegrationProtocolError(f"Checkpoint {label} is invalid")


def _required_string(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    raise IntegrationProtocolError(f"Checkpoint field is missing: {keys[-1]}")


def _require_equal(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    expected: str,
    label: str,
) -> None:
    for key in keys:
        if key in payload:
            if str(payload[key]) != expected:
                raise IntegrationProtocolError(f"Checkpoint {label} ownership does not match")
            return
    raise IntegrationProtocolError(f"Checkpoint {label} ownership is missing")


def _bounded_receipt(result: dict[str, Any], settings: Settings) -> dict[str, Any]:
    limit = max(1024, int(getattr(settings, "sync_receipt_max_bytes", 64 * 1024)))
    try:
        encoded = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IntegrationProtocolError("Moodle receipt is not valid JSON") from exc
    if len(encoded) <= limit:
        return result
    return {
        "status": str(result.get("status", "DELIVERED")),
        "truncated": True,
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "size": len(encoded),
    }


def _epoch_datetime(value: Any) -> datetime | None:
    if isinstance(value, bool):
        raise IntegrationProtocolError("Course timestamp is invalid")
    try:
        epoch = int(value or 0)
    except (TypeError, ValueError) as exc:
        raise IntegrationProtocolError("Course timestamp is invalid") from exc
    if epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise IntegrationProtocolError("Course timestamp is out of range") from exc


def _same_instant(left: datetime, right: datetime) -> bool:
    return _as_utc(left) == _as_utc(right)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


__all__ = [
    "BridgeFactory",
    "ClientFactory",
    "ClaimedOutboxEvent",
    "ConnectionTarget",
    "RecoveredCheckpoint",
    "SchedulerResult",
    "checkpoint_interval_seconds",
    "claim_next_outbox_event",
    "default_bridge_factory",
    "enqueue_course_synchronizations",
    "maintain_attempts",
    "process_outbox_once",
    "recover_attempt_checkpoint",
    "recover_latest_checkpoint",
    "retry_delay_seconds",
    "run_scheduler_iteration",
    "validate_checkpoint_manifest",
]
