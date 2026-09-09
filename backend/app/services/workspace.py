from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.integrations.moodle_modes import moodle_auth_mode, moodle_pluginless_transport
from app.models.attempts import (
    Attempt,
    ClipboardReceipt,
    EditEvent,
    Snapshot,
    Submission,
    Workspace,
    WorkspaceFile,
)
from app.models.courses import Course
from app.models.enums import AttemptState, CourseRole, LMSProvider, SyncOutboxState
from app.models.identity import ExternalPrincipal, LMSConnection
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment, TaskVersion
from app.services.client_context import client_context_for_hash, normalize_client_context
from app.services.common import (
    DomainError,
    canonical_hash,
    canonical_json,
    contiguous_delta,
    language_for_path,
    sha256_text,
    validate_source_path,
)
from app.services.delivery_profile import resolve_assessment_workspace_delivery_profile
from app.services.moodle_quiz_runtime import (
    PreparedMoodleAssignment,
    PreparedMoodleQuizAttempt,
    materialize_prepared_task_version,
    pinned_moodle_quiz_binding,
    prepared_binding_matches_attempt,
    resolve_moodle_assignment_context,
    resolve_moodle_quiz_context,
)
from app.services.policy import (
    ensure_assessment_available,
    require_membership,
    resolve_assigned_task_version,
)

TERMINAL_CHECKPOINT_REASONS = frozenset({"SUBMISSION", "DEADLINE", "FINAL_MINUTE"})

# The runner accepts at most 64 files.  A 512 KiB UTF-8 budget still fits the
# 4 MiB runner/Moodle envelopes under worst-case six-byte JSON escaping, with
# room left for paths, hashes and manifest metadata.
MAX_WORKSPACE_FILES = 64
MAX_WORKSPACE_BYTES = 512 * 1024
MAX_SOURCE_FILE_BYTES = MAX_WORKSPACE_BYTES
TRANSLATION_UNIT_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})


@dataclass(frozen=True, slots=True)
class WorkspaceMutation:
    event: EditEvent
    workspace: Workspace
    file: WorkspaceFile


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _utf8_size(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise DomainError(422, "INVALID_UTF8_SOURCE", "Source must be valid UTF-8") from exc


def _source_hash(value: str) -> str:
    _utf8_size(value)
    return sha256_text(value)


def _is_translation_unit(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in TRANSLATION_UNIT_SUFFIXES


def _is_text_data_file(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() == ".txt"


def _translation_unit_count(files: list[WorkspaceFile]) -> int:
    return sum(_is_translation_unit(file.path) for file in files)


def _ensure_single_translation_unit(files: list[WorkspaceFile]) -> None:
    if _translation_unit_count(files) != 1:
        raise DomainError(
            409,
            "INVALID_SINGLE_FILE_WORKSPACE",
            "A single-file workspace must contain exactly one C/C++ translation unit",
        )


def _bounded_limit(requested: int, ceiling: int) -> int:
    if isinstance(requested, bool) or requested < 1:
        raise DomainError(500, "INVALID_WORKSPACE_LIMIT", "Workspace limit is invalid")
    return min(requested, ceiling)


def _request_id(value: str) -> str:
    if not value or len(value) > 100:
        raise DomainError(
            422,
            "INVALID_IDEMPOTENCY_KEY",
            "Client request id must contain between 1 and 100 characters",
        )
    return value


def _client_id(value: str) -> str:
    if len(value) > 100:
        raise DomainError(422, "INVALID_CLIENT_ID", "Client id cannot exceed 100 characters")
    return value


def _request_hash(operation: str, payload: dict[str, object]) -> str:
    return canonical_hash({"operation": operation, **payload})


def _assert_idempotent_replay(prior: EditEvent, expected_hash: str) -> None:
    stored_hash = None
    if len(prior.changes) == 1 and isinstance(prior.changes[0], dict):
        stored_hash = prior.changes[0].get("request_hash")
    if stored_hash != expected_hash:
        raise DomainError(
            409,
            "IDEMPOTENCY_CONFLICT",
            "The idempotency key was already used with a different request payload",
        )


def ensure_attempt_not_finalized_in_moodle(attempt: Attempt) -> None:
    if attempt.submission_source == "MOODLE_FINALIZED":
        raise DomainError(
            423,
            "LMS_ATTEMPT_FINALIZED",
            "The work session was finalized directly in Moodle",
        )


def _ensure_writable(attempt: Attempt) -> None:
    ensure_attempt_not_finalized_in_moodle(attempt)
    if attempt.state != AttemptState.ACTIVE.value:
        raise DomainError(409, "ATTEMPT_READ_ONLY", "Attempt is read-only")
    if attempt.deadline_at and utcnow() >= _as_utc(attempt.deadline_at):
        raise DomainError(409, "DEADLINE_PASSED", "Deadline has passed")


def _validate_workspace_size(*, file_count: int, total_bytes: int) -> None:
    if file_count > MAX_WORKSPACE_FILES:
        raise DomainError(
            413,
            "WORKSPACE_FILE_LIMIT_EXCEEDED",
            f"Workspace cannot contain more than {MAX_WORKSPACE_FILES} files",
        )
    if total_bytes > MAX_WORKSPACE_BYTES:
        raise DomainError(
            413,
            "WORKSPACE_SIZE_LIMIT_EXCEEDED",
            f"Workspace source text cannot exceed {MAX_WORKSPACE_BYTES} UTF-8 bytes",
        )


async def _active_files(db: AsyncSession, workspace_id: uuid.UUID) -> list[WorkspaceFile]:
    return list(
        (
            await db.scalars(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == workspace_id,
                    WorkspaceFile.deleted_revision.is_(None),
                )
                .order_by(WorkspaceFile.path)
            )
        ).all()
    )


async def refresh_workspace_hash(db: AsyncSession, workspace: Workspace) -> list[WorkspaceFile]:
    files = await _active_files(db, workspace.id)
    manifest = [{"id": str(row.id), "path": row.path, "hash": row.content_hash} for row in files]
    workspace.aggregate_size = sum(_utf8_size(row.content) for row in files)
    workspace.current_hash = canonical_hash(manifest)
    return files


async def create_snapshot(
    db: AsyncSession,
    workspace: Workspace,
    reason: str,
) -> Snapshot:
    existing = await db.scalar(
        select(Snapshot).where(
            Snapshot.workspace_id == workspace.id,
            Snapshot.revision == workspace.current_revision,
        )
    )
    if existing is not None:
        return existing
    files = await _active_files(db, workspace.id)
    payload = [
        {
            "id": str(row.id),
            "path": row.path,
            "language": row.language,
            "content": row.content,
            "content_hash": row.content_hash,
        }
        for row in files
    ]
    snapshot = Snapshot(
        workspace_id=workspace.id,
        revision=workspace.current_revision,
        event_chain_head=workspace.event_chain_head,
        manifest_hash=canonical_hash(payload),
        files=payload,
        reason=reason,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def enqueue_checkpoint(
    db: AsyncSession,
    *,
    attempt: Attempt,
    snapshot: Snapshot,
    reason: str,
    heartbeat_generation: int | None = None,
) -> SyncOutbox:
    assessment = await db.get(Assessment, attempt.assessment_id)
    principal = await db.get(ExternalPrincipal, attempt.principal_id)
    if assessment is None or principal is None:
        raise DomainError(500, "CHECKPOINT_CONTEXT_MISSING", "Checkpoint context is incomplete")
    course = await db.get(Course, assessment.course_id)
    if course is None:
        raise DomainError(500, "CHECKPOINT_CONTEXT_MISSING", "Course was not found")
    connection = await db.get(LMSConnection, course.connection_id)
    if connection is None:
        raise DomainError(500, "CHECKPOINT_CONTEXT_MISSING", "LMS connection was not found")
    if heartbeat_generation is not None and (
        reason != "PERIODIC"
        or isinstance(heartbeat_generation, bool)
        or not isinstance(heartbeat_generation, int)
        or heartbeat_generation < 0
    ):
        raise DomainError(
            500,
            "INVALID_CHECKPOINT_GENERATION",
            "Periodic checkpoint generation is invalid",
        )
    terminal_generation = (
        reason
        if reason in TERMINAL_CHECKPOINT_REASONS
        else f"P{heartbeat_generation:x}"
        if heartbeat_generation is not None
        else "REVISION"
    )
    idempotency_key = f"checkpoint:{attempt.id}:{snapshot.id}:{terminal_generation}"[:100]
    existing = await db.scalar(
        select(SyncOutbox).where(SyncOutbox.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    manifest_json = canonical_json(snapshot.files)
    chain_head = snapshot.event_chain_head
    if chain_head and (
        len(chain_head) != 64 or any(char not in "0123456789abcdef" for char in chain_head)
    ):
        raise DomainError(500, "INVALID_EVENT_CHAIN_HEAD", "Snapshot event chain head is invalid")
    if attempt.epoch < 1:
        raise DomainError(500, "INVALID_ATTEMPT_EPOCH", "Attempt epoch is invalid")
    payload: dict[str, object] = {
        "course_id": course.external_id,
        "user_id": principal.external_subject,
        "attempt_ref": str(attempt.id),
        "snapshot_ref": str(snapshot.id),
        "snapshot_sha256": snapshot.manifest_hash,
        "manifest_hash": snapshot.manifest_hash,
        "event_chain_head": chain_head,
        "epoch": attempt.epoch,
        "reason": reason,
        "manifest_json": manifest_json,
        "workspace_revision": snapshot.revision,
    }
    if heartbeat_generation is not None:
        payload["heartbeat_generation"] = heartbeat_generation
    event = SyncOutbox(
        connection_id=course.connection_id,
        course_id=course.id,
        attempt_id=attempt.id,
        event_type="attempt.checkpoint",
        aggregate_type="Snapshot",
        aggregate_id=snapshot.id,
        idempotency_key=idempotency_key,
        payload=payload,
    )
    if connection.provider != LMSProvider.MOODLE.value or (
        moodle_auth_mode(connection) == "PLUGINLESS"
        and moodle_pluginless_transport(connection) != "PLAYWRIGHT"
    ):
        # Mobile-token Moodle has no connector-owned checkpoint store. The
        # Playwright transport is different: it mirrors source into Quiz Essay.
        event.state = SyncOutboxState.DELIVERED.value
        event.delivered_at = utcnow()
        event.receipt = {
            "status": "LOCAL_ONLY",
            "external_sync": False,
            "reason": "PLUGINLESS_MOODLE" if connection.provider == "MOODLE" else "LOCAL_LMS",
        }
    db.add(event)
    await db.flush()
    return event


async def start_attempt(
    db: AsyncSession,
    *,
    assessment_id: uuid.UUID,
    principal_id: uuid.UUID,
    prepared_moodle_quiz: PreparedMoodleQuizAttempt | None = None,
    prepared_moodle_assignment: PreparedMoodleAssignment | None = None,
    client_context: dict[str, str] | None = None,
) -> Attempt:
    assessment = await db.scalar(
        select(Assessment).where(Assessment.id == assessment_id).with_for_update()
    )
    if assessment is None:
        raise DomainError(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found")
    membership = await require_membership(
        db,
        principal_id=principal_id,
        course_id=assessment.course_id,
        role=CourseRole.STUDENT,
    )
    effective_policy = await ensure_assessment_available(db, assessment, membership)
    assessment_policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    moodle_managed = (
        assessment_policy.get("moodle_metadata_read_only") is True
        or prepared_moodle_quiz is not None
        or prepared_moodle_assignment is not None
    )
    existing = list(
        (
            await db.scalars(
                select(Attempt)
                .where(
                    Attempt.assessment_id == assessment.id,
                    Attempt.principal_id == principal_id,
                )
                .order_by(Attempt.sequence)
                .with_for_update()
            )
        ).all()
    )
    active = next((row for row in existing if row.state == AttemptState.ACTIVE.value), None)
    if active is not None:
        quiz_context = await resolve_moodle_quiz_context(db, assessment.id)
        assignment_context = await resolve_moodle_assignment_context(db, assessment.id)
        if quiz_context is not None:
            current_binding = pinned_moodle_quiz_binding(
                active.integrity_policy,
                course_external_id=quiz_context.course.external_id,
                cmid=quiz_context.cmid,
            )
            if prepared_moodle_quiz is not None:
                active_version = (
                    await db.get(TaskVersion, active.assigned_task_version_id)
                    if active.assigned_task_version_id is not None
                    else None
                )
                raw_integrity = (
                    dict(active.integrity_policy)
                    if isinstance(active.integrity_policy, dict)
                    else {}
                )
                if current_binding is not None and not prepared_binding_matches_attempt(
                    raw_integrity,
                    prepared_moodle_quiz,
                ):
                    raise DomainError(
                        409,
                        "MOODLE_ATTEMPT_BINDING_CONFLICT",
                        "The active local attempt is bound to another Moodle attempt",
                    )
                if active_version is None:
                    raise DomainError(
                        409,
                        "MOODLE_ATTEMPT_BINDING_CONFLICT",
                        "The active local attempt has no task snapshot",
                    )

                # Releases before the live-preparation boundary could create a
                # local ACTIVE row which still referenced the shared imported
                # task version.  Its statement may be empty for a random slot,
                # or non-empty for an ordinary Essay question.  Do not make the
                # student abandon that workspace: once the connector has proved
                # the exact live attempt/slot, upgrade the row in place to the
                # immutable runtime task version.  A partially present binding,
                # or a runtime version detached from its binding, still fails
                # closed.
                if current_binding is None:
                    binding_keys = {
                        "moodle_course_id",
                        "moodle_cmid",
                        "moodle_attempt_id",
                        "moodle_question_slot",
                        "moodle_answer_transport",
                        "moodle_runtime_prepared",
                    }
                    version_policy = (
                        active_version.ai_policy
                        if isinstance(active_version.ai_policy, dict)
                        else {}
                    )
                    if any(key in raw_integrity for key in binding_keys) or (
                        version_policy.get("source") == "MOODLE_RUNTIME_ESSAY"
                    ):
                        raise DomainError(
                            409,
                            "MOODLE_ATTEMPT_BINDING_CONFLICT",
                            "The active local attempt has an incomplete Moodle binding",
                        )

                if (
                    current_binding is None
                    or active_version.statement != prepared_moodle_quiz.question_text
                ):
                    active_version = await materialize_prepared_task_version(
                        db,
                        base_version=active_version,
                        prepared=prepared_moodle_quiz,
                    )
                    active.assigned_task_version_id = active_version.id

                expected_multi_file = prepared_moodle_quiz.answer_transport == "ESSAY_ATTACHMENT"
                workspace = await db.scalar(
                    select(Workspace).where(Workspace.attempt_id == active.id).with_for_update()
                )
                if workspace is None:
                    raise DomainError(
                        500,
                        "WORKSPACE_NOT_FOUND",
                        "The active attempt workspace is missing",
                    )
                if not expected_multi_file:
                    files = await _active_files(db, workspace.id)
                    if _translation_unit_count(files) != 1 or any(
                        not _is_translation_unit(file.path) and not _is_text_data_file(file.path)
                        for file in files
                    ):
                        raise DomainError(
                            409,
                            "MOODLE_ATTEMPT_BINDING_CONFLICT",
                            "The existing workspace cannot be sent as Moodle online text",
                        )
                workspace.multi_file = expected_multi_file
                raw_integrity.update(
                    {
                        "moodle_course_id": prepared_moodle_quiz.course_external_id,
                        "moodle_cmid": prepared_moodle_quiz.cmid,
                        "moodle_attempt_id": prepared_moodle_quiz.external_attempt_id,
                        "moodle_question_slot": prepared_moodle_quiz.question_slot,
                        "moodle_answer_transport": prepared_moodle_quiz.answer_transport,
                        "moodle_available_answer_transports": list(
                            prepared_moodle_quiz.available_answer_transports
                        ),
                        "moodle_runtime_prepared": True,
                    }
                )
                active.integrity_policy = raw_integrity
                # Moodle is authoritative for per-user overrides and the live
                # attempt lifetime.  A legacy local deadline may reflect only
                # the expired global window and must not keep an overridden
                # active attempt read-only after successful live preparation.
                active.expected_end_at = (
                    utcnow() + timedelta(seconds=prepared_moodle_quiz.remaining_seconds)
                    if prepared_moodle_quiz.remaining_seconds is not None
                    else None
                )
                active.deadline_at = None
                await db.flush()
            if prepared_moodle_quiz is None:
                if current_binding is None:
                    raise DomainError(
                        409,
                        "MOODLE_RUNTIME_PREPARATION_REQUIRED",
                        "The Moodle Quiz attempt must be checked before it can be opened",
                    )
        if assignment_context is not None:
            if prepared_moodle_assignment is None:
                raise DomainError(
                    409,
                    "MOODLE_RUNTIME_PREPARATION_REQUIRED",
                    "The Moodle Assignment must be checked before it can be opened",
                )
            if (
                prepared_moodle_assignment.course_external_id
                != assignment_context.course.external_id
                or prepared_moodle_assignment.cmid != assignment_context.cmid
            ):
                raise DomainError(
                    409,
                    "MOODLE_ATTEMPT_BINDING_CONFLICT",
                    "Moodle prepared a different course activity",
                )
            raw_integrity = (
                dict(active.integrity_policy) if isinstance(active.integrity_policy, dict) else {}
            )
            expected_assignment_binding = {
                "moodle_course_id": prepared_moodle_assignment.course_external_id,
                "moodle_cmid": prepared_moodle_assignment.cmid,
                "moodle_answer_transport": prepared_moodle_assignment.answer_transport,
            }
            for key, expected in expected_assignment_binding.items():
                current = raw_integrity.get(key)
                if current is not None and str(current) != str(expected):
                    raise DomainError(
                        409,
                        "MOODLE_ATTEMPT_BINDING_CONFLICT",
                        "The active local attempt is bound to another Moodle submission form",
                    )
            workspace = await db.scalar(
                select(Workspace).where(Workspace.attempt_id == active.id).with_for_update()
            )
            if workspace is None:
                raise DomainError(
                    500,
                    "WORKSPACE_NOT_FOUND",
                    "The active attempt workspace is missing",
                )
            expected_multi_file = prepared_moodle_assignment.answer_transport == "ASSIGN_FILE"
            if not expected_multi_file:
                files = await _active_files(db, workspace.id)
                if _translation_unit_count(files) != 1 or any(
                    not _is_translation_unit(file.path) and not _is_text_data_file(file.path)
                    for file in files
                ):
                    raise DomainError(
                        409,
                        "MOODLE_ATTEMPT_BINDING_CONFLICT",
                        "The existing workspace cannot be sent as Moodle online text",
                    )
            workspace.multi_file = expected_multi_file
            raw_integrity.update(
                {
                    **expected_assignment_binding,
                    "moodle_available_answer_transports": list(
                        prepared_moodle_assignment.available_answer_transports
                    ),
                    "moodle_runtime_prepared": True,
                }
            )
            active.integrity_policy = raw_integrity
            active.expected_end_at = None
            active.deadline_at = None
            await db.flush()
        return active
    sequence = len(existing) + 1
    if prepared_moodle_quiz is not None and any(
        prepared_binding_matches_attempt(row.integrity_policy, prepared_moodle_quiz)
        for row in existing
    ):
        raise DomainError(
            409,
            "MOODLE_ATTEMPT_STILL_FINALIZING",
            "The previous local attempt is still the active Moodle attempt; "
            "wait for its final synchronization and retry",
        )
    if (
        not moodle_managed
        and effective_policy.attempt_limit is not None
        and sequence > effective_policy.attempt_limit
    ):
        raise DomainError(409, "ATTEMPT_LIMIT_REACHED", "Attempt limit reached")
    task_version = await resolve_assigned_task_version(db, assessment, membership)
    quiz_context = await resolve_moodle_quiz_context(db, assessment.id)
    assignment_context = await resolve_moodle_assignment_context(db, assessment.id)
    if quiz_context is not None:
        if prepared_moodle_quiz is None:
            raise DomainError(
                409,
                "MOODLE_RUNTIME_PREPARATION_REQUIRED",
                "The Moodle Quiz attempt must be checked before it can be opened",
            )
        if (
            prepared_moodle_quiz.course_external_id != quiz_context.course.external_id
            or prepared_moodle_quiz.cmid != quiz_context.cmid
        ):
            raise DomainError(
                409,
                "MOODLE_ATTEMPT_BINDING_CONFLICT",
                "Moodle prepared a different course activity",
            )
        task_version = await materialize_prepared_task_version(
            db,
            base_version=task_version,
            prepared=prepared_moodle_quiz,
        )
    if assignment_context is not None:
        if prepared_moodle_assignment is None:
            raise DomainError(
                409,
                "MOODLE_RUNTIME_PREPARATION_REQUIRED",
                "The Moodle Assignment must be checked before it can be opened",
            )
        if (
            prepared_moodle_assignment.course_external_id != assignment_context.course.external_id
            or prepared_moodle_assignment.cmid != assignment_context.cmid
        ):
            raise DomainError(
                409,
                "MOODLE_ATTEMPT_BINDING_CONFLICT",
                "Moodle prepared a different course activity",
            )
    starter_files = task_version.starter_files or [
        {
            "path": "main.c" if task_version.language == "C" else "main.cpp",
            "content": "",
        }
    ]
    normalized_starters: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    for item in starter_files:
        path = validate_source_path(str(item.get("path", "main.cpp")))
        if path in seen:
            raise DomainError(
                409,
                "DUPLICATE_STARTER_PATH",
                "Assigned task contains duplicate starter file paths",
            )
        content = str(item.get("content", ""))
        content_size = _utf8_size(content)
        if content_size > MAX_SOURCE_FILE_BYTES:
            raise DomainError(413, "SOURCE_FILE_TOO_LARGE", "Starter source file is too large")
        total_bytes += content_size
        normalized_starters.append((path, content, _source_hash(content)))
        seen.add(path)
    _validate_workspace_size(file_count=len(normalized_starters), total_bytes=total_bytes)
    delivery_resolution = await resolve_assessment_workspace_delivery_profile(db, assessment.id)
    if (
        delivery_resolution.external
        and delivery_resolution.profile is None
        and prepared_moodle_quiz is None
        and prepared_moodle_assignment is None
    ):
        raise DomainError(
            409,
            "LMS_DELIVERY_PROFILE_UNRESOLVED",
            "The LMS answer format is not known yet; synchronize the course and retry",
        )
    workspace_multi_file = (
        prepared_moodle_quiz.answer_transport == "ESSAY_ATTACHMENT"
        if prepared_moodle_quiz is not None
        else prepared_moodle_assignment.answer_transport == "ASSIGN_FILE"
        if prepared_moodle_assignment is not None
        else (
            assessment.multi_file
            if delivery_resolution.profile is None
            else delivery_resolution.profile.multi_file
        )
    )
    if not workspace_multi_file and (
        sum(_is_translation_unit(path) for path, _content, _hash in normalized_starters) != 1
        or any(
            not _is_translation_unit(path) and not _is_text_data_file(path)
            for path, _content, _hash in normalized_starters
        )
    ):
        raise DomainError(
            409,
            "INVALID_STARTER_FILES",
            "A single-file assessment must provide exactly one C/C++ translation unit "
            "and may additionally provide .txt data files",
        )
    now = utcnow()
    # Per-user Moodle overrides are intentionally not copied into local rules.
    # Without a live exact deadline, a global close/time limit could terminate
    # a legitimately extended attempt. Moodle remains authoritative and every
    # checkpoint/final submission revalidates its live form.
    expected_end = (
        now + timedelta(seconds=prepared_moodle_quiz.remaining_seconds)
        if prepared_moodle_quiz is not None and prepared_moodle_quiz.remaining_seconds is not None
        else None
        if moodle_managed
        else now + timedelta(seconds=effective_policy.duration_seconds)
        if effective_policy.duration_seconds
        else None
    )
    deadlines = [
        _as_utc(value)
        for value in (
            expected_end,
            None if moodle_managed else effective_policy.closes_at,
        )
        if value is not None
    ]
    # ``expected_end_at`` is an informational countdown captured from the live
    # Moodle page.  Moodle remains authoritative for an overridden quiz, so it
    # must not become a local hard deadline that can make the editor read-only
    # before Moodle itself closes the attempt.
    deadline = None if moodle_managed else min(deadlines) if deadlines else None
    integrity_policy = {"paste_policy": assessment.paste_policy, "variant_locked": True}
    if prepared_moodle_quiz is not None:
        integrity_policy.update(
            {
                "moodle_course_id": prepared_moodle_quiz.course_external_id,
                "moodle_cmid": prepared_moodle_quiz.cmid,
                "moodle_attempt_id": prepared_moodle_quiz.external_attempt_id,
                "moodle_question_slot": prepared_moodle_quiz.question_slot,
                "moodle_answer_transport": prepared_moodle_quiz.answer_transport,
                "moodle_available_answer_transports": list(
                    prepared_moodle_quiz.available_answer_transports
                ),
                "moodle_runtime_prepared": True,
            }
        )
    if prepared_moodle_assignment is not None:
        integrity_policy.update(
            {
                "moodle_course_id": prepared_moodle_assignment.course_external_id,
                "moodle_cmid": prepared_moodle_assignment.cmid,
                "moodle_answer_transport": prepared_moodle_assignment.answer_transport,
                "moodle_available_answer_transports": list(
                    prepared_moodle_assignment.available_answer_transports
                ),
                "moodle_runtime_prepared": True,
            }
        )
    attempt = Attempt(
        assessment_id=assessment.id,
        assigned_task_version_id=task_version.id,
        principal_id=principal_id,
        sequence=sequence,
        expected_end_at=expected_end,
        deadline_at=deadline,
        integrity_policy=integrity_policy,
        client_context=normalize_client_context(client_context),
    )
    db.add(attempt)
    await db.flush()
    workspace = Workspace(attempt_id=attempt.id, multi_file=workspace_multi_file)
    db.add(workspace)
    await db.flush()
    for path, content, content_hash in normalized_starters:
        db.add(
            WorkspaceFile(
                workspace_id=workspace.id,
                path=path,
                language=language_for_path(path),
                content=content,
                content_hash=content_hash,
            )
        )
    await db.flush()
    await refresh_workspace_hash(db, workspace)
    snapshot = await create_snapshot(db, workspace, "ATTEMPT_STARTED")
    await enqueue_checkpoint(db, attempt=attempt, snapshot=snapshot, reason="ATTEMPT_STARTED")
    await db.flush()
    return attempt


async def _locked_workspace(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    require_active: bool = True,
    require_current_membership: bool = True,
) -> tuple[Attempt, Workspace]:
    attempt = await db.scalar(select(Attempt).where(Attempt.id == attempt_id).with_for_update())
    if attempt is None or attempt.principal_id != principal_id:
        raise DomainError(404, "ATTEMPT_NOT_FOUND", "Attempt was not found")
    if require_current_membership:
        assessment = await db.get(Assessment, attempt.assessment_id)
        if assessment is None:
            raise DomainError(500, "ASSESSMENT_MISSING", "Attempt assessment is missing")
        await require_membership(
            db,
            principal_id=principal_id,
            course_id=assessment.course_id,
            role=CourseRole.STUDENT,
        )
    workspace = await db.scalar(
        select(Workspace).where(Workspace.attempt_id == attempt.id).with_for_update()
    )
    if workspace is None:
        raise DomainError(500, "WORKSPACE_MISSING", "Attempt workspace is missing")
    if require_active:
        _ensure_writable(attempt)
    return attempt, workspace


async def replace_file_content(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    file_id: uuid.UUID,
    content: str,
    expected_revision: int,
    client_request_id: str,
    source: str = "TYPING",
    receipt_id: uuid.UUID | None = None,
    client_id: str = "",
    max_file_bytes: int = MAX_SOURCE_FILE_BYTES,
    client_context: dict[str, str] | None = None,
) -> WorkspaceMutation:
    attempt, workspace = await _locked_workspace(
        db,
        attempt_id=attempt_id,
        principal_id=principal_id,
        require_active=False,
    )
    request_id = _request_id(client_request_id)
    normalized_client_id = _client_id(client_id)
    requested_source = source.upper()
    content_hash = _source_hash(content)
    semantic_hash = _request_hash(
        "REPLACE_CONTENT",
        {
            "attempt_id": str(attempt.id),
            "file_id": str(file_id),
            "content_hash": content_hash,
            "expected_revision": expected_revision,
            "source": requested_source,
            "receipt_id": str(receipt_id) if receipt_id is not None else None,
            "client_id": normalized_client_id,
        },
    )
    prior = await db.scalar(
        select(EditEvent).where(
            EditEvent.workspace_id == workspace.id,
            EditEvent.client_request_id == request_id,
        )
    )
    if prior is not None:
        _assert_idempotent_replay(prior, semantic_hash)
        prior_file = await db.get(WorkspaceFile, prior.file_id)
        if prior_file is None:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "Prior edit no longer references a file")
        return WorkspaceMutation(prior, workspace, prior_file)
    _ensure_writable(attempt)
    if workspace.current_revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Workspace revision is stale",
            {"current_revision": workspace.current_revision},
        )
    effective_file_limit = _bounded_limit(max_file_bytes, MAX_SOURCE_FILE_BYTES)
    content_size = _utf8_size(content)
    if content_size > effective_file_limit:
        raise DomainError(413, "SOURCE_FILE_TOO_LARGE", "Source file is too large")
    file = await db.scalar(
        select(WorkspaceFile)
        .where(
            WorkspaceFile.id == file_id,
            WorkspaceFile.workspace_id == workspace.id,
            WorkspaceFile.deleted_revision.is_(None),
        )
        .with_for_update()
    )
    if file is None:
        raise DomainError(404, "WORKSPACE_FILE_NOT_FOUND", "Workspace file was not found")
    active_files = await _active_files(db, workspace.id)
    resulting_size = workspace.aggregate_size - _utf8_size(file.content) + content_size
    _validate_workspace_size(
        file_count=len(active_files),
        total_bytes=resulting_size,
    )
    previous_content = file.content
    previous_hash = file.content_hash
    delta = contiguous_delta(previous_content, content)
    delta.update(
        {
            "previous_content_hash": previous_hash,
            "content_hash": content_hash,
            "request_hash": semantic_hash,
        }
    )
    normalized_source = "EDITOR_CHANGE"
    if requested_source == "INTERNAL_PASTE":
        inserted_text = str(delta["insert_text"])
        receipt = await db.scalar(
            select(ClipboardReceipt)
            .where(
                ClipboardReceipt.id == receipt_id,
                ClipboardReceipt.attempt_id == attempt.id,
                ClipboardReceipt.principal_id == principal_id,
                ClipboardReceipt.expires_at > utcnow(),
                ClipboardReceipt.used_at.is_(None),
                ClipboardReceipt.revision <= workspace.current_revision,
                ClipboardReceipt.text_hash == sha256_text(inserted_text),
                ClipboardReceipt.text_length == len(inserted_text),
            )
            .with_for_update()
        )
        if receipt is None:
            raise DomainError(
                422, "INVALID_CLIPBOARD_RECEIPT", "A valid internal clipboard receipt is required"
            )
        if not any(inserted_text in candidate.content for candidate in active_files):
            raise DomainError(
                422,
                "CLIPBOARD_TEXT_NOT_IN_WORKSPACE",
                "Pasted text is no longer present in the current workspace",
            )
        receipt.used_at = utcnow()
        normalized_source = "INTERNAL_PASTE"
    elif requested_source in {"PASTE", "EXTERNAL_PASTE", "UNVERIFIED_PASTE"}:
        normalized_source = "UNVERIFIED_PASTE"
    elif len(str(delta["insert_text"])) + int(delta["delete_count"]) > 256:
        normalized_source = "UNVERIFIED_BULK_EDIT"
    new_revision = workspace.current_revision + 1
    file.content = content
    file.content_hash = str(delta["content_hash"])
    received_at = utcnow()
    normalized_client_context = client_context_for_hash(client_context)
    payload = {
        "epoch": attempt.epoch,
        "sequence": new_revision,
        "client_id": normalized_client_id,
        "client_request_id": request_id,
        "file_id": str(file.id),
        "source": normalized_source,
        "event_type": "REPLACE_CONTENT",
        "changes": [delta],
        "received_at": received_at.isoformat(),
        "client_context": normalized_client_context,
    }
    event_hash = canonical_hash({"previous": workspace.event_chain_head, **payload})
    event = EditEvent(
        workspace_id=workspace.id,
        epoch=attempt.epoch,
        sequence=new_revision,
        client_id=normalized_client_id,
        client_request_id=request_id,
        source=normalized_source,
        event_type="REPLACE_CONTENT",
        file_id=file.id,
        changes=[delta],
        previous_hash=workspace.event_chain_head,
        event_hash=event_hash,
        received_at=received_at,
        client_context=normalized_client_context,
    )
    db.add(event)
    workspace.current_revision = new_revision
    workspace.event_chain_head = event_hash
    attempt.current_revision = new_revision
    await refresh_workspace_hash(db, workspace)
    await db.flush()
    return WorkspaceMutation(event, workspace, file)


async def create_workspace_file(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    path: str,
    content: str,
    expected_revision: int,
    client_request_id: str,
    client_context: dict[str, str] | None = None,
) -> WorkspaceMutation:
    attempt, workspace = await _locked_workspace(
        db,
        attempt_id=attempt_id,
        principal_id=principal_id,
        require_active=False,
    )
    request_id = _request_id(client_request_id)
    normalized_path = validate_source_path(path)
    content_hash = _source_hash(content)
    semantic_hash = _request_hash(
        "FILE_CREATE",
        {
            "attempt_id": str(attempt.id),
            "path": normalized_path,
            "content_hash": content_hash,
            "expected_revision": expected_revision,
        },
    )
    prior = await db.scalar(
        select(EditEvent).where(
            EditEvent.workspace_id == workspace.id,
            EditEvent.client_request_id == request_id,
        )
    )
    if prior is not None:
        _assert_idempotent_replay(prior, semantic_hash)
        prior_file = await db.get(WorkspaceFile, prior.file_id)
        if prior_file is None:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "Prior request is not reproducible")
        return WorkspaceMutation(prior, workspace, prior_file)
    _ensure_writable(attempt)
    if not workspace.multi_file and not _is_text_data_file(normalized_path):
        raise DomainError(
            409,
            "SINGLE_FILE_ASSESSMENT",
            "A single-file assessment only allows additional .txt data files",
        )
    if workspace.current_revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Workspace revision is stale",
            {"current_revision": workspace.current_revision},
        )
    files = await _active_files(db, workspace.id)
    if not workspace.multi_file:
        _ensure_single_translation_unit(files)
    _validate_workspace_size(
        file_count=len(files) + 1,
        total_bytes=workspace.aggregate_size + _utf8_size(content),
    )
    duplicate = await db.scalar(
        select(WorkspaceFile.id).where(
            WorkspaceFile.workspace_id == workspace.id,
            WorkspaceFile.path == normalized_path,
        )
    )
    if duplicate is not None:
        raise DomainError(409, "SOURCE_PATH_EXISTS", "A file with this path already exists")
    new_revision = workspace.current_revision + 1
    file = WorkspaceFile(
        workspace_id=workspace.id,
        path=normalized_path,
        language=language_for_path(normalized_path),
        content=content,
        content_hash=content_hash,
        created_revision=new_revision,
    )
    db.add(file)
    await db.flush()
    change = {
        "type": "file_create",
        "path": normalized_path,
        "content_hash": file.content_hash,
        "request_hash": semantic_hash,
    }
    received_at = utcnow()
    normalized_client_context = client_context_for_hash(client_context)
    event_hash = canonical_hash(
        {
            "previous": workspace.event_chain_head,
            "epoch": attempt.epoch,
            "sequence": new_revision,
            "client_id": "",
            "client_request_id": request_id,
            "file_id": str(file.id),
            "source": "EDITOR_CHANGE",
            "event_type": "FILE_CREATE",
            "changes": [change],
            "received_at": received_at.isoformat(),
            "client_context": normalized_client_context,
        }
    )
    event = EditEvent(
        workspace_id=workspace.id,
        epoch=attempt.epoch,
        sequence=new_revision,
        client_request_id=request_id,
        source="EDITOR_CHANGE",
        event_type="FILE_CREATE",
        file_id=file.id,
        changes=[change],
        previous_hash=workspace.event_chain_head,
        event_hash=event_hash,
        received_at=received_at,
        client_context=normalized_client_context,
    )
    db.add(event)
    workspace.current_revision = new_revision
    workspace.event_chain_head = event_hash
    attempt.current_revision = new_revision
    await refresh_workspace_hash(db, workspace)
    await db.flush()
    return WorkspaceMutation(event, workspace, file)


async def delete_workspace_file(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    file_id: uuid.UUID,
    expected_revision: int,
    client_request_id: str,
    client_context: dict[str, str] | None = None,
) -> WorkspaceMutation:
    attempt, workspace = await _locked_workspace(
        db,
        attempt_id=attempt_id,
        principal_id=principal_id,
        require_active=False,
    )
    request_id = _request_id(client_request_id)
    semantic_hash = _request_hash(
        "FILE_DELETE",
        {
            "attempt_id": str(attempt.id),
            "file_id": str(file_id),
            "expected_revision": expected_revision,
        },
    )
    prior = await db.scalar(
        select(EditEvent).where(
            EditEvent.workspace_id == workspace.id,
            EditEvent.client_request_id == request_id,
        )
    )
    if prior is not None:
        _assert_idempotent_replay(prior, semantic_hash)
        prior_file = await db.get(WorkspaceFile, prior.file_id)
        if prior_file is None:
            raise DomainError(409, "IDEMPOTENCY_CONFLICT", "Prior request is not reproducible")
        return WorkspaceMutation(prior, workspace, prior_file)
    _ensure_writable(attempt)
    if workspace.current_revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Workspace revision is stale",
            {"current_revision": workspace.current_revision},
        )
    file = await db.scalar(
        select(WorkspaceFile)
        .where(
            WorkspaceFile.id == file_id,
            WorkspaceFile.workspace_id == workspace.id,
            WorkspaceFile.deleted_revision.is_(None),
        )
        .with_for_update()
    )
    if file is None:
        raise DomainError(404, "WORKSPACE_FILE_NOT_FOUND", "Workspace file was not found")
    files = await _active_files(db, workspace.id)
    if not workspace.multi_file and not _is_text_data_file(file.path):
        raise DomainError(
            409,
            "LAST_TRANSLATION_UNIT",
            "The single C/C++ translation unit cannot be deleted",
        )
    remaining_files = [item for item in files if item.id != file.id]
    remaining_translation_units = _translation_unit_count(remaining_files)
    if workspace.multi_file and remaining_translation_units < 1:
        raise DomainError(
            409,
            "LAST_TRANSLATION_UNIT",
            "The last C/C++ translation unit cannot be deleted",
        )
    if not workspace.multi_file:
        _ensure_single_translation_unit(remaining_files)
    new_revision = workspace.current_revision + 1
    change = {
        "type": "file_delete",
        "path": file.path,
        "content_hash": file.content_hash,
        "request_hash": semantic_hash,
    }
    received_at = utcnow()
    normalized_client_context = client_context_for_hash(client_context)
    event_hash = canonical_hash(
        {
            "previous": workspace.event_chain_head,
            "epoch": attempt.epoch,
            "sequence": new_revision,
            "client_id": "",
            "client_request_id": request_id,
            "file_id": str(file.id),
            "source": "EDITOR_CHANGE",
            "event_type": "FILE_DELETE",
            "changes": [change],
            "received_at": received_at.isoformat(),
            "client_context": normalized_client_context,
        }
    )
    event = EditEvent(
        workspace_id=workspace.id,
        epoch=attempt.epoch,
        sequence=new_revision,
        client_request_id=request_id,
        source="EDITOR_CHANGE",
        event_type="FILE_DELETE",
        file_id=file.id,
        changes=[change],
        previous_hash=workspace.event_chain_head,
        event_hash=event_hash,
        received_at=received_at,
        client_context=normalized_client_context,
    )
    db.add(event)
    file.deleted_revision = new_revision
    workspace.current_revision = new_revision
    workspace.event_chain_head = event_hash
    attempt.current_revision = new_revision
    await refresh_workspace_hash(db, workspace)
    await db.flush()
    return WorkspaceMutation(event, workspace, file)


async def create_clipboard_receipt(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    source_file_id: uuid.UUID,
    text: str,
    revision: int,
    ttl_seconds: int = 300,
) -> ClipboardReceipt:
    attempt, workspace = await _locked_workspace(
        db, attempt_id=attempt_id, principal_id=principal_id
    )
    if revision != workspace.current_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Clipboard source revision is stale",
            {"current_revision": workspace.current_revision},
        )
    if not text or len(text.encode("utf-8")) > 262_144:
        raise DomainError(422, "INVALID_CLIPBOARD_TEXT", "Clipboard text is empty or too large")
    source_file = await db.scalar(
        select(WorkspaceFile).where(
            WorkspaceFile.id == source_file_id,
            WorkspaceFile.workspace_id == workspace.id,
            WorkspaceFile.deleted_revision.is_(None),
        )
    )
    if source_file is None or text not in source_file.content:
        raise DomainError(
            422, "CLIPBOARD_TEXT_NOT_IN_WORKSPACE", "Copied text is not present in the source file"
        )
    receipt = ClipboardReceipt(
        attempt_id=attempt.id,
        file_id=source_file.id,
        principal_id=principal_id,
        revision=revision,
        text_hash=sha256_text(text),
        text_length=len(text),
        expires_at=utcnow() + timedelta(seconds=ttl_seconds),
    )
    db.add(receipt)
    await db.flush()
    return receipt


async def submit_attempt(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
    expected_revision: int,
    source: str = "MANUAL",
    client_context: dict[str, str] | None = None,
) -> Submission:
    normalized_source = source.upper()
    if normalized_source not in {"MANUAL", "DEADLINE"}:
        raise DomainError(422, "INVALID_SUBMISSION_SOURCE", "Submission source is invalid")
    attempt, workspace = await _locked_workspace(
        db,
        attempt_id=attempt_id,
        principal_id=principal_id,
        require_active=False,
        require_current_membership=normalized_source != "DEADLINE",
    )
    existing = await db.scalar(
        select(Submission)
        .where(Submission.attempt_id == attempt.id)
        .order_by(Submission.revision.desc())
    )
    if (
        attempt.state in {AttemptState.SUBMITTED.value, AttemptState.AUTO_SUBMITTED.value}
        and existing
    ):
        return existing
    ensure_attempt_not_finalized_in_moodle(attempt)
    if attempt.state != AttemptState.ACTIVE.value:
        raise DomainError(409, "ATTEMPT_NOT_SUBMITTABLE", "Attempt cannot be submitted")
    now = utcnow()
    if (
        normalized_source == "MANUAL"
        and attempt.deadline_at
        and now >= _as_utc(attempt.deadline_at)
    ):
        raise DomainError(409, "DEADLINE_PASSED", "Deadline has passed")
    if workspace.current_revision != expected_revision:
        raise DomainError(
            409,
            "REVISION_CONFLICT",
            "Submit revision is stale",
            {"current_revision": workspace.current_revision},
        )
    snapshot = await create_snapshot(db, workspace, "SUBMISSION")
    submission = Submission(
        attempt_id=attempt.id,
        snapshot_id=snapshot.id,
        revision=(existing.revision + 1) if existing else 1,
        source=normalized_source,
        submitted_at=now,
        late=bool(attempt.deadline_at and now > _as_utc(attempt.deadline_at)),
        client_context=normalize_client_context(client_context),
    )
    db.add(submission)
    attempt.state = (
        AttemptState.AUTO_SUBMITTED.value
        if normalized_source == "DEADLINE"
        else AttemptState.SUBMITTED.value
    )
    attempt.submitted_at = now
    attempt.submission_source = normalized_source
    await db.flush()
    await enqueue_checkpoint(
        db,
        attempt=attempt,
        snapshot=snapshot,
        reason="DEADLINE" if normalized_source == "DEADLINE" else "SUBMISSION",
    )
    await db.flush()
    return submission


async def retry_submission_checkpoint(
    db: AsyncSession,
    *,
    attempt_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> Submission:
    """Requeue the owner's existing terminal checkpoint without resubmitting locally."""

    attempt, _workspace = await _locked_workspace(
        db,
        attempt_id=attempt_id,
        principal_id=principal_id,
        require_active=False,
    )
    ensure_attempt_not_finalized_in_moodle(attempt)
    if attempt.state not in {
        AttemptState.SUBMITTED.value,
        AttemptState.AUTO_SUBMITTED.value,
    }:
        raise DomainError(409, "ATTEMPT_NOT_SUBMITTED", "Attempt has not been submitted")
    submission = await db.scalar(
        select(Submission)
        .where(Submission.attempt_id == attempt.id)
        .order_by(Submission.revision.desc(), Submission.created_at.desc())
    )
    if submission is None:
        raise DomainError(409, "SUBMISSION_NOT_FOUND", "Submitted attempt has no submission")
    events = list(
        (
            await db.scalars(
                select(SyncOutbox)
                .where(
                    SyncOutbox.attempt_id == attempt.id,
                    SyncOutbox.event_type == "attempt.checkpoint",
                )
                .order_by(SyncOutbox.created_at.desc(), SyncOutbox.id.desc())
                .with_for_update()
            )
        ).all()
    )
    terminal = next(
        (
            event
            for event in events
            if isinstance(event.payload, dict)
            and event.payload.get("reason") in {"SUBMISSION", "DEADLINE"}
        ),
        None,
    )
    if terminal is None:
        raise DomainError(
            409,
            "SUBMISSION_CHECKPOINT_MISSING",
            "Submitted attempt has no Moodle delivery checkpoint",
        )
    if terminal.state in {
        SyncOutboxState.PENDING.value,
        SyncOutboxState.RETRY.value,
        SyncOutboxState.FAILED.value,
        SyncOutboxState.BLOCKED.value,
    }:
        terminal.state = SyncOutboxState.RETRY.value
        # A deliberate manual retry starts a fresh bounded delivery cycle. If
        # the old exhausted counter is retained, every click gets only one
        # attempt and immediately falls back to FAILED again.
        terminal.attempts = 0
        terminal.next_attempt_at = utcnow()
        terminal.locked_at = None
        terminal.last_error = ""
        await db.flush()
    return submission


def checkpoint_cadence_seconds(
    attempt: Attempt,
    assessment: Assessment,
    *,
    now=None,
) -> int:
    now = _as_utc(now or utcnow())
    duration = (
        int((_as_utc(attempt.expected_end_at) - _as_utc(attempt.started_at)).total_seconds())
        if attempt.expected_end_at is not None
        else assessment.duration_seconds
    )
    if not duration:
        return 300
    elapsed = max(0.0, (now - _as_utc(attempt.started_at)).total_seconds())
    divisor = 20 if elapsed >= duration * 0.8 else 10
    return max(30, min(900, int(duration / divisor)))
