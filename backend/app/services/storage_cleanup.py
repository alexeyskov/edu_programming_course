"""Bounded, conservative retention of local application payloads.

The quota measures logical UTF-8 database payload, not PostgreSQL heap/WAL or
Docker disk usage. DELETE frees reusable database space; it need not shrink the
database file. No filesystem paths, database maintenance commands or LMS writes
are used here. The caller owns the transaction and its audit entry.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import JSON, LargeBinary, String, Text, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.config import Settings
from app.db.base import Base, utcnow
from app.models.analysis import AICodeReview, AuthorshipAnalysisJob, SimilarityAnalysis
from app.models.attempts import (
    Attempt,
    EditEvent,
    RunRequest,
    Snapshot,
    Submission,
    Workspace,
    WorkspaceFile,
)
from app.models.evidence import EvidenceReport
from app.models.integration import AuditEntry, ExternalMapping, SyncOutbox, SystemSetting
from app.models.review import (
    ChatMessage,
    ChatThread,
    ReviewClaim,
    ReviewDecision,
    ReviewDraft,
    TeacherExperiment,
)
from app.models.tasks import Assessment
from app.schemas.system import (
    StorageCategoryRead,
    StorageCleanupCategoryRead,
    StorageCleanupRead,
    StorageStatusRead,
)
from app.services.ai_chat_cleanup import _eligible_threads, cleanup_grace_seconds
from app.services.common import DomainError

StorageCategory = Literal["edit_history", "teacher_ai_chats", "synced_submissions"]
_CATEGORIES: tuple[StorageCategory, ...] = (
    "edit_history",
    "teacher_ai_chats",
    "synced_submissions",
)
_GIB = 1024**3
_BATCH = 100
_MAX_RECORDS = 2000
_CLEANUP_LOCK_KEY = 3386471045812
_local_lock = asyncio.Lock()


def _bytes(db: AsyncSession, column):
    value = cast(column, Text)
    if db.get_bind().dialect.name == "postgresql":
        return func.coalesce(func.octet_length(value), 0)
    # SQLite length(TEXT) counts code points; BLOB counts stored UTF-8 bytes.
    return func.coalesce(func.length(cast(value, LargeBinary)), 0)


def _payload_columns(table):
    return [column for column in table.columns if isinstance(column.type, String | JSON)]


def _row_bytes(db: AsyncSession, table):
    return sum((_bytes(db, column) for column in _payload_columns(table)), start=0)


async def _sum(db: AsyncSession, expression, *where) -> int:
    return int(await db.scalar(select(func.coalesce(func.sum(expression), 0)).where(*where)) or 0)


async def _used_bytes(db: AsyncSession) -> int:
    # Database-side aggregates only: even huge source files/transcripts never
    # cross the SQL connection merely to calculate the storage meter.
    total = 0
    for table in Base.metadata.tables.values():
        columns = _payload_columns(table)
        if columns:
            total += await _sum(db, _row_bytes(db, table))
    return total


async def _limit_gb(db: AsyncSession) -> int:
    value = await db.scalar(select(SystemSetting.value).where(SystemSetting.key == "effective"))
    raw = value.get("storage_limit_gb", 128) if isinstance(value, dict) else 128
    return raw if type(raw) is int and 1 <= raw <= 65_536 else 128


def storage_cleanup_cutoff(settings: Settings) -> datetime:
    return utcnow() - timedelta(seconds=cleanup_grace_seconds(settings))


def _safe_attempt_ids(cutoff: datetime):
    submissions = (
        select(Submission.id).where(Submission.attempt_id == Attempt.id).correlate(Attempt)
    )
    decisions = select(ReviewDecision.id).where(ReviewDecision.submission_id.in_(submissions))
    busy_claim = (
        select(ReviewClaim.id)
        .where(
            ReviewClaim.submission_id.in_(submissions),
            ReviewClaim.state == "ACTIVE",
        )
        .exists()
    )
    # Conservatively protect every saved draft, including an old one retained
    # after approval. Retention must never guess whether handwritten work matters.
    draft = select(ReviewDraft.id).where(ReviewDraft.submission_id.in_(submissions)).exists()
    experiment = (
        select(TeacherExperiment.id)
        .where(
            TeacherExperiment.submission_id.in_(submissions),
            TeacherExperiment.deleted_at.is_(None),
            TeacherExperiment.reset_at.is_(None),
        )
        .exists()
    )
    run = (
        select(RunRequest.id)
        .where(
            or_(RunRequest.attempt_id == Attempt.id, RunRequest.submission_id.in_(submissions)),
            RunRequest.status.in_(("QUEUED", "RUNNING")),
        )
        .exists()
    )
    ai = (
        select(AICodeReview.id)
        .where(
            AICodeReview.submission_id.in_(submissions),
            AICodeReview.state.in_(("QUEUED", "RUNNING")),
        )
        .exists()
    )
    author = (
        select(AuthorshipAnalysisJob.id)
        .where(
            AuthorshipAnalysisJob.submission_id.in_(submissions),
            AuthorshipAnalysisJob.state.in_(("PENDING", "RUNNING")),
        )
        .exists()
    )
    evidence = (
        select(EvidenceReport.id)
        .where(
            EvidenceReport.submission_id.in_(submissions),
            EvidenceReport.status == "RUNNING",
        )
        .exists()
    )
    similarity = (
        select(SimilarityAnalysis.id)
        .where(
            SimilarityAnalysis.assessment_id == Attempt.assessment_id,
            SimilarityAnalysis.state.in_(("PENDING", "RUNNING")),
        )
        .exists()
    )
    outbox = (
        select(SyncOutbox.id)
        .where(
            SyncOutbox.state != "DELIVERED",
            or_(
                SyncOutbox.attempt_id == Attempt.id,
                SyncOutbox.aggregate_id == Attempt.id,
                SyncOutbox.aggregate_id.in_(submissions),
                SyncOutbox.aggregate_id.in_(decisions),
                # A history response may already be in flight. Its activity/course
                # scope is protected until it has finished the local apply phase.
                (
                    (
                        SyncOutbox.course_id
                        == select(Assessment.course_id)
                        .where(
                            Assessment.id == Attempt.assessment_id,
                        )
                        .correlate(Attempt)
                        .scalar_subquery()
                    )
                    & SyncOutbox.event_type.in_(("course.sync", "moodle.history.import"))
                    & SyncOutbox.state.in_(("PENDING", "PROCESSING", "RETRY"))
                ),
            ),
        )
        .exists()
    )
    return select(Attempt.id).where(
        Attempt.state.in_(("SUBMITTED", "AUTO_SUBMITTED", "LOCKED")),
        Attempt.updated_at <= cutoff,
        ~busy_claim,
        ~draft,
        ~experiment,
        ~run,
        ~ai,
        ~author,
        ~evidence,
        ~similarity,
        ~outbox,
    )


def _event_ids(cutoff: datetime):
    return (
        select(EditEvent.id)
        .join(Workspace, Workspace.id == EditEvent.workspace_id)
        .where(
            Workspace.attempt_id.in_(_safe_attempt_ids(cutoff)),
            EditEvent.received_at <= cutoff,
        )
    )


def _chat_ids(cutoff: datetime):
    # Deleted/closed chats only. Open conversations and student chats are never
    # sacrificed by the automatic teacher-chat tier.
    busy_claim = (
        select(ReviewClaim.id)
        .where(
            ReviewClaim.submission_id == ChatThread.submission_id,
            ReviewClaim.state == "ACTIVE",
        )
        .exists()
    )
    return select(ChatThread.id).where(
        *_eligible_threads(cutoff),
        ChatThread.mode == "TEACHER",
        ~busy_claim,
    )


def _recoverable_submission_ids(cutoff: datetime):
    other_submission = aliased(Submission)
    shared_attempt = (
        select(other_submission.id)
        .where(
            other_submission.attempt_id == Submission.attempt_id,
            other_submission.id != Submission.id,
        )
        .exists()
    )
    mapping = (
        select(ExternalMapping.id)
        .where(
            ExternalMapping.local_id == Submission.id,
            ExternalMapping.local_type == "Submission",
            ExternalMapping.external_type == "moodle_historical_submission",
            ExternalMapping.external_id != "",
            ExternalMapping.external_id == Submission.external_receipt["external_id"].as_string(),
            ExternalMapping.external_revision
            == Submission.external_receipt["external_revision"].as_string(),
            ExternalMapping.metadata_json["source_complete"].as_boolean().is_(True),
            ExternalMapping.metadata_json["deleted_in_moodle"].as_boolean().is_not(True),
            ExternalMapping.metadata_json["retired_by_authoritative_quiz_refresh"]
            .as_boolean()
            .is_not(True),
            ExternalMapping.metadata_json["sync_state"]
            .as_string()
            .is_distinct_from(
                "MISSING_IN_MOODLE",
            ),
        )
        .exists()
    )
    latest_id = (
        select(ReviewDecision.id)
        .where(
            ReviewDecision.submission_id == Submission.id,
        )
        .order_by(ReviewDecision.revision.desc())
        .limit(1)
        .correlate(Submission)
        .scalar_subquery()
    )
    approved = (
        select(ReviewDecision.id)
        .where(
            ReviewDecision.id == latest_id,
            ReviewDecision.status == "APPLIED",
            ReviewDecision.lms_export_state.in_(("IMPORTED", "DELIVERED")),
            ReviewDecision.updated_at <= cutoff,
        )
        .exists()
    )
    unsynced_decision = (
        select(ReviewDecision.id)
        .where(
            ReviewDecision.submission_id == Submission.id,
            ~ReviewDecision.lms_export_state.in_(("IMPORTED", "DELIVERED")),
        )
        .exists()
    )
    return select(Submission.id).where(
        Submission.source == "MOODLE_IMPORT",
        Submission.payload_evicted_at.is_(None),
        Submission.updated_at <= cutoff,
        Submission.attempt_id.in_(_safe_attempt_ids(cutoff)),
        Submission.external_receipt["source_complete"].as_boolean().is_(True),
        Submission.external_receipt["deleted_in_moodle"].as_boolean().is_not(True),
        Submission.external_receipt["retired_by_authoritative_quiz_refresh"]
        .as_boolean()
        .is_not(True),
        Submission.external_receipt["source_refresh_complete"].as_boolean().is_not(False),
        Submission.lms_export_state.in_(("IMPORTED", "DELIVERED")),
        mapping,
        approved,
        ~unsynced_decision,
        ~shared_attempt,
        select(Snapshot.id)
        .join(Workspace, Workspace.id == Snapshot.workspace_id)
        .where(
            Snapshot.id == Submission.snapshot_id,
            Workspace.attempt_id == Submission.attempt_id,
        )
        .exists(),
    )


def _submission_workspace_ids(submission_ids):
    return (
        select(Workspace.id)
        .join(Attempt, Attempt.id == Workspace.attempt_id)
        .join(
            Submission,
            Submission.attempt_id == Attempt.id,
        )
        .where(Submission.id.in_(submission_ids))
    )


async def _submission_payload_bytes(db: AsyncSession, ids, *, eligible: bool = False) -> int:
    workspaces = _submission_workspace_ids(ids)
    snapshots = select(Submission.snapshot_id).where(Submission.id.in_(ids))
    comments = [ReviewDecision.submission_id.in_(ids)]
    if eligible:
        latest = aliased(ReviewDecision)
        latest_comment = (
            select(latest.comment)
            .where(
                latest.submission_id == ReviewDecision.submission_id,
            )
            .order_by(latest.revision.desc())
            .limit(1)
            .correlate(ReviewDecision)
            .scalar_subquery()
        )
        # Unique comments on superseded reviews remain local and cannot be
        # advertised as reclaimable from Moodle's current feedback.
        comments.append(ReviewDecision.comment == latest_comment)
    return (
        await _sum(db, _bytes(db, Snapshot.files), Snapshot.id.in_(snapshots))
        + await _sum(
            db, _bytes(db, WorkspaceFile.content), WorkspaceFile.workspace_id.in_(workspaces)
        )
        + await _sum(db, _bytes(db, ReviewDecision.comment), *comments)
    )


async def _categories(db: AsyncSession, cutoff: datetime) -> list[StorageCategoryRead]:
    event_bytes = _row_bytes(db, EditEvent.__table__)
    all_chats = select(ChatThread.id).where(ChatThread.mode == "TEACHER")
    eligible_chats = _chat_ids(cutoff)
    all_submissions = select(Submission.id).where(Submission.source == "MOODLE_IMPORT")
    eligible_submissions = _recoverable_submission_ids(cutoff)

    async def chat_bytes(ids):
        return await _sum(
            db, _row_bytes(db, ChatThread.__table__), ChatThread.id.in_(ids)
        ) + await _sum(
            db,
            _row_bytes(db, ChatMessage.__table__),
            ChatMessage.thread_id.in_(ids),
        )

    async def count(model, ids=None):
        query = select(func.count(model.id))
        if ids is not None:
            query = query.where(model.id.in_(ids))
        return int(await db.scalar(query) or 0)

    return [
        StorageCategoryRead(
            category="edit_history",
            records=await count(EditEvent),
            used_bytes=await _sum(db, event_bytes),
            eligible_records=await count(EditEvent, _event_ids(cutoff)),
            eligible_bytes=await _sum(db, event_bytes, EditEvent.id.in_(_event_ids(cutoff))),
        ),
        StorageCategoryRead(
            category="teacher_ai_chats",
            records=await count(ChatThread, all_chats),
            used_bytes=await chat_bytes(all_chats),
            eligible_records=await count(ChatThread, eligible_chats),
            eligible_bytes=await chat_bytes(eligible_chats),
        ),
        StorageCategoryRead(
            category="synced_submissions",
            records=await count(Submission, all_submissions),
            used_bytes=await _submission_payload_bytes(db, all_submissions),
            eligible_records=await count(Submission, eligible_submissions),
            eligible_bytes=await _submission_payload_bytes(db, eligible_submissions, eligible=True),
        ),
    ]


_WARNINGS = [
    "Лимит относится к логическому объёму данных приложения, а не к RAM или всему диску. "
    "После удаления PostgreSQL переиспользует свободное место; файл БД может не уменьшиться.",
    "Незавершённые работы, черновики, активные проверки и несинхронизированные данные защищены. "
    "Исходники без подтверждённой возможности восстановления из Moodle не удаляются.",
]


async def get_storage_status(db: AsyncSession, settings: Settings) -> StorageStatusRead:
    cutoff = storage_cleanup_cutoff(settings)
    database_bytes = None
    if db.get_bind().dialect.name == "postgresql":
        database_bytes = int(
            await db.scalar(select(func.pg_database_size(func.current_database()))) or 0
        )
    last_cleanup_at = await db.scalar(
        select(func.max(AuditEntry.occurred_at)).where(
            AuditEntry.action.in_(("storage.cleaned_up", "storage.auto_cleaned_up")),
        )
    )
    if last_cleanup_at is not None and last_cleanup_at.tzinfo is None:
        last_cleanup_at = last_cleanup_at.replace(tzinfo=UTC)
    return StorageStatusRead(
        storage_limit_gb=await _limit_gb(db),
        used_bytes=await _used_bytes(db),
        database_bytes=database_bytes,
        eligible_before=cutoff,
        categories=await _categories(db, cutoff),
        warnings=list(_WARNINGS),
        last_cleanup_at=last_cleanup_at,
    )


async def _lock_attempt(db: AsyncSession, attempt_id, cutoff: datetime) -> Attempt | None:
    attempt = await db.scalar(
        select(Attempt)
        .where(Attempt.id == attempt_id)
        .with_for_update(
            skip_locked=True,
        )
        .execution_options(populate_existing=True)
    )
    if (
        attempt is None
        or await db.scalar(
            select(Attempt.id).where(
                Attempt.id == attempt_id,
                Attempt.id.in_(_safe_attempt_ids(cutoff)),
            )
        )
        is None
    ):
        return None
    return attempt


async def _prune_events(db: AsyncSession, cutoff: datetime, maximum: int) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(EditEvent.id, EditEvent.workspace_id, Workspace.attempt_id)
            .join(Workspace, Workspace.id == EditEvent.workspace_id)
            .join(Attempt, Attempt.id == Workspace.attempt_id)
            .where(EditEvent.id.in_(_event_ids(cutoff)))
            .order_by(EditEvent.received_at, EditEvent.id)
            .limit(maximum)
            .with_for_update(of=Attempt, skip_locked=True)
        )
    ).all()
    event_ids = [row.id for row in rows]
    attempt_ids = list({row.attempt_id for row in rows})

    async def footprint():
        return await _sum(
            db, _row_bytes(db, EditEvent.__table__), EditEvent.id.in_(event_ids)
        ) + await _sum(
            db,
            _bytes(db, Attempt.integrity_policy),
            Attempt.id.in_(attempt_ids),
        )

    before = await footprint()
    removed = 0
    attempts = {}
    for row in rows:
        if row.attempt_id not in attempts:
            attempts[row.attempt_id] = await _lock_attempt(db, row.attempt_id, cutoff)
        attempt = attempts[row.attempt_id]
        if attempt is None:
            continue
        await db.execute(delete(EditEvent).where(EditEvent.id == row.id))
        policy = dict(attempt.integrity_policy or {})
        policy.update(
            history_available=False,
            history_pruned_at=utcnow().isoformat(),
            history_pruned_events=int(policy.get("history_pruned_events", 0)) + 1,
        )
        await db.execute(
            update(Attempt)
            .where(Attempt.id == attempt.id)
            .values(
                integrity_policy=policy,
                updated_at=Attempt.updated_at,
            )
        )
        removed += 1
    await db.flush()
    return removed, before - await footprint()


async def _prune_chats(db: AsyncSession, cutoff: datetime, maximum: int) -> tuple[int, int]:
    ids = list(
        (
            await db.scalars(
                select(ChatThread.id)
                .where(ChatThread.id.in_(_chat_ids(cutoff)))
                .order_by(ChatThread.updated_at, ChatThread.id)
                .limit(maximum)
                .with_for_update(skip_locked=True)
            )
        ).all()
    )
    before = await _sum(
        db, _row_bytes(db, ChatMessage.__table__), ChatMessage.thread_id.in_(ids)
    ) + await _sum(
        db,
        _row_bytes(db, ChatThread.__table__),
        ChatThread.id.in_(ids),
    )
    if ids:
        await db.execute(delete(ChatMessage).where(ChatMessage.thread_id.in_(ids)))
        await db.execute(delete(ChatThread).where(ChatThread.id.in_(ids)))
    return len(ids), before


def _without_response_comments(value):
    result = dict(value or {})
    if isinstance(result.get("responses"), list):
        result["responses"] = [
            {key: item for key, item in response.items() if key != "comment"}
            if isinstance(response, dict)
            else response
            for response in result["responses"]
        ]
    return result


async def _prune_submissions(db: AsyncSession, cutoff: datetime, maximum: int) -> tuple[int, int]:
    rows = (
        await db.execute(
            select(Submission.id, Submission.attempt_id)
            .join(Attempt, Attempt.id == Submission.attempt_id)
            .join(Workspace, Workspace.attempt_id == Attempt.id)
            .where(Submission.id.in_(_recoverable_submission_ids(cutoff)))
            .order_by(Submission.submitted_at, Submission.id)
            .limit(maximum)
            .with_for_update(of=Attempt, skip_locked=True)
        )
    ).all()
    ids = [row.id for row in rows]

    async def footprint():
        workspaces = _submission_workspace_ids(ids)
        snapshots = select(Submission.snapshot_id).where(Submission.id.in_(ids))
        return (
            await _sum(db, _bytes(db, Snapshot.files), Snapshot.id.in_(snapshots))
            + await _sum(
                db, _bytes(db, WorkspaceFile.content), WorkspaceFile.workspace_id.in_(workspaces)
            )
            + await _sum(
                db, _row_bytes(db, ReviewDecision.__table__), ReviewDecision.submission_id.in_(ids)
            )
            + await _sum(db, _row_bytes(db, Submission.__table__), Submission.id.in_(ids))
        )

    before = await footprint()
    removed = 0
    for row in rows:
        if await _lock_attempt(db, row.attempt_id, cutoff) is None:
            continue
        submission = await db.scalar(
            select(Submission)
            .where(Submission.id == row.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if (
            submission is None
            or await db.scalar(
                select(Submission.id).where(
                    Submission.id == row.id,
                    Submission.id.in_(_recoverable_submission_ids(cutoff)),
                )
            )
            is None
        ):
            continue
        # Imported historical workspaces belong to exactly one immutable answer;
        # do not evict a shared workspace with any separately retained answer.
        other = await db.scalar(
            select(Submission.id)
            .where(
                Submission.attempt_id == row.attempt_id,
                Submission.id != row.id,
            )
            .limit(1)
        )
        if other is not None:
            continue
        workspace = await db.scalar(
            select(Workspace)
            .where(Workspace.attempt_id == row.attempt_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if workspace is None:
            continue
        await db.execute(
            update(Snapshot).where(Snapshot.id == submission.snapshot_id).values(files=[])
        )
        await db.execute(
            update(WorkspaceFile)
            .where(WorkspaceFile.workspace_id == workspace.id)
            .values(content="")
        )
        decisions = list(
            (
                await db.scalars(
                    select(ReviewDecision).where(
                        ReviewDecision.submission_id == row.id,
                    )
                )
            ).all()
        )
        latest = max(decisions, key=lambda item: item.revision)
        latest_comment = latest.comment
        latest_criteria = latest.criterion_scores
        for decision in decisions:
            # Moodle restores the current review, not unique comments from an
            # older revision. Preserve historical feedback that differs.
            if decision.comment == latest_comment:
                decision.comment = ""
            if decision.criterion_scores == latest_criteria:
                decision.criterion_scores = _without_response_comments(decision.criterion_scores)
        receipt = dict(submission.external_receipt or {})
        if isinstance(receipt.get("moodle_review"), dict):
            receipt["moodle_review"] = {**receipt["moodle_review"], "comment": ""}
        submission.external_receipt = receipt
        submission.payload_evicted_at = utcnow()
        removed += 1
    await db.flush()
    return removed, before - await footprint()


async def cleanup_storage(
    db: AsyncSession,
    settings: Settings,
    *,
    category: StorageCategory | Literal["quota"],
    eligible_before: datetime,
    automatic: bool = False,
) -> StorageCleanupRead:
    if category not in (*_CATEGORIES, "quota"):
        raise DomainError(422, "STORAGE_CATEGORY_INVALID", "Неизвестная категория очистки.")
    cutoff = (
        eligible_before.replace(tzinfo=UTC) if eligible_before.tzinfo is None else eligible_before
    )
    if cutoff > storage_cleanup_cutoff(settings):
        raise DomainError(422, "STORAGE_CUTOFF_INVALID", "Обновите данные об объёме хранилища.")
    # PostgreSQL transaction advisory lock is shared by HTTP and worker
    # processes and remains held until the caller commits/rolls back.
    async with _local_lock:
        if db.get_bind().dialect.name == "postgresql" and not await db.scalar(
            select(func.pg_try_advisory_xact_lock(_CLEANUP_LOCK_KEY)),
        ):
            raise DomainError(409, "STORAGE_CLEANUP_BUSY", "Очистка уже выполняется.")
        before = await _used_bytes(db)
        used = before
        limit = (await _limit_gb(db)) * _GIB
        counts = []
        remaining = _MAX_RECORDS
        warnings = list(_WARNINGS)
        for tier in _CATEGORIES if category == "quota" else (category,):
            count = reclaimed = 0
            while remaining and (category != "quota" or used > limit):
                operation_before = used
                handler = {
                    "edit_history": _prune_events,
                    "teacher_ai_chats": _prune_chats,
                    "synced_submissions": _prune_submissions,
                }[tier]
                # Quota processing takes a single oldest record at a time near
                # the threshold, so a large batch cannot over-delete data.
                maximum = min(remaining, 1 if category == "quota" else _BATCH)
                changed, delta = await handler(db, cutoff, maximum)
                if not changed:
                    break
                count += changed
                remaining -= changed
                await db.flush()
                # Exact SQL byte delta over only the touched rows, never a
                # full-database scan for each deleted record.
                used = max(0, used - delta)
                reclaimed += max(0, operation_before - used)
            counts.append(
                StorageCleanupCategoryRead(
                    category=tier, removed_records=count, reclaimed_bytes=reclaimed
                )
            )
            if not remaining:
                warnings.append(
                    "Достигнут безопасный предел одной очистки; следующий проход продолжит её."
                )
                break
        # One final aggregate reconciles the reported result with concurrent
        # writers. Such growth is handled on the next scheduled pass.
        used = await _used_bytes(db)
        if used > limit:
            warnings.append(
                "Лимит всё ещё превышен: оставшиеся данные защищены или ожидают следующего прохода."
            )
        return StorageCleanupRead(
            used_bytes_before=before,
            used_bytes_after=used,
            reclaimed_bytes=max(0, before - used),
            over_limit=used > limit,
            removed_records=sum(item.removed_records for item in counts),
            categories=counts,
            warnings=warnings,
        )
