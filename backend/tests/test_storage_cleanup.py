from __future__ import annotations

import copy
import importlib
import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import func, select, update

from app.db.base import utcnow
from app.models.analysis import AICodeReview
from app.models.attempts import (
    Attempt,
    EditEvent,
    RunRequest,
    Snapshot,
    Submission,
    Workspace,
    WorkspaceFile,
)
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ChatMessage, ChatThread, ReviewClaim, ReviewDecision, ReviewDraft
from app.services import storage_cleanup as service
from app.services.common import DomainError
from app.services.moodle_history import _materialize_flat_historical_submissions
from tests.test_moodle_history import _finished_item, _seed_history_target

OLD = utcnow() - timedelta(days=30)


@pytest_asyncio.fixture
async def storage_bundle(app_bundle):
    _app, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions)
    async with sessions() as db, db.begin():
        from app.models.courses import Course
        from app.models.tasks import Assessment

        stats = await _materialize_flat_historical_submissions(
            db,
            course=await db.get(Course, ids["course_id"]),
            assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42",
            items=[_finished_item()],
        )
        assert stats.created == 1
        submission = await db.scalar(select(Submission))
        workspace = await db.scalar(
            select(Workspace).where(Workspace.attempt_id == submission.attempt_id)
        )
        ids.update(
            submission_id=submission.id,
            attempt_id=submission.attempt_id,
            workspace_id=workspace.id,
            snapshot_id=submission.snapshot_id,
        )
        for model in (Attempt, Submission, ReviewDecision, Workspace, WorkspaceFile, Snapshot):
            await db.execute(update(model).values(created_at=OLD, updated_at=OLD))
    return sessions, settings, ids


async def _event(db, ids, *, sequence=1, content="изменение" * 1000):
    event = EditEvent(
        workspace_id=ids["workspace_id"],
        epoch=1,
        sequence=sequence,
        client_request_id=uuid.uuid4().hex,
        event_hash="b" * 64,
        changes=[{"insert_text": content}],
        received_at=OLD + timedelta(seconds=sequence),
        created_at=OLD,
        updated_at=OLD,
    )
    db.add(event)
    await db.flush()
    return event.id


async def _chat(db, ids, *, mode="TEACHER", status="CLOSED", recent=False):
    when = utcnow() if recent else OLD
    thread = ChatThread(
        owner_id=ids["teacher_id"],
        course_id=ids["course_id"],
        mode=mode,
        status=status,
        created_at=when,
        updated_at=when,
    )
    db.add(thread)
    await db.flush()
    db.add(
        ChatMessage(
            thread_id=thread.id,
            role="ASSISTANT",
            content="secret transcript" * 500,
            created_at=when,
            updated_at=when,
        )
    )
    return thread.id


async def test_default_quota_and_utf8_json_accounting_are_read_only(storage_bundle):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        initial = await service.get_storage_status(db, settings)
        assert initial.storage_limit_gb == 128
        assert initial.database_bytes is None
        assert initial.used_bytes > 0
        await _event(db, ids, content="данные" * 10000)
        status = await service.get_storage_status(db, settings)
        assert status.used_bytes > initial.used_bytes + len(("данные" * 10000).encode())
        history = next(item for item in status.categories if item.category == "edit_history")
        assert history.eligible_records == history.records == 1
        assert history.eligible_bytes == history.used_bytes
        assert history.used_bytes > len(("данные" * 10000).encode())
        assert (await db.get(Submission, ids["submission_id"])).payload_evicted_at is None
        assert await db.scalar(select(func.count(EditEvent.id))) == 1
        assert "secret transcript" not in status.model_dump_json()


async def test_under_quota_does_not_remove_anything(storage_bundle):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        event_id = await _event(db, ids)
        thread_id = await _chat(db, ids)
        status = await service.get_storage_status(db, settings)
        result = await service.cleanup_storage(
            db, settings, category="quota", eligible_before=status.eligible_before, automatic=True
        )
        assert result.removed_records == result.reclaimed_bytes == 0
        assert result.used_bytes_after == result.used_bytes_before
        assert result.over_limit is False
        assert await db.get(EditEvent, event_id) is not None
        assert await db.get(ChatThread, thread_id) is not None


async def test_quota_oldest_history_first_and_stop_before_chat_or_code(storage_bundle, monkeypatch):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        first = await _event(db, ids, sequence=1)
        second = await _event(db, ids, sequence=2)
        chat = await _chat(db, ids)
        status = await service.get_storage_status(db, settings)
        monkeypatch.setattr(service, "_GIB", max(1, (status.used_bytes - 1000) // 128))
        result = await service.cleanup_storage(
            db, settings, category="quota", eligible_before=status.eligible_before, automatic=True
        )
        assert result.removed_records == 1
        assert result.categories[0].category == "edit_history"
        assert result.over_limit is False
        assert await db.get(EditEvent, first) is None
        assert await db.get(EditEvent, second) is not None
        assert await db.get(ChatThread, chat) is not None
        assert (await db.get(Submission, ids["submission_id"])).payload_evicted_at is None
        attempt = await db.get(Attempt, ids["attempt_id"])
        assert attempt.integrity_policy["history_available"] is False
        assert attempt.integrity_policy["history_pruned_events"] == 1


async def test_quota_tiers_keep_ids_hashes_and_old_unique_feedback(storage_bundle, monkeypatch):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        await _event(db, ids, sequence=1)
        await _event(db, ids, sequence=2)
        chat_id = await _chat(db, ids)
        student_chat = await _chat(db, ids, mode="STUDENT")
        open_chat = await _chat(db, ids, status="OPEN")
        snapshot = await db.get(Snapshot, ids["snapshot_id"])
        manifest = snapshot.manifest_hash
        older_snapshot = Snapshot(
            workspace_id=ids["workspace_id"],
            revision=99,
            manifest_hash="c" * 64,
            files=[{"path": "old.cpp", "content": "unique old source"}],
            reason="LMS_IMPORT",
            created_at=OLD,
            updated_at=OLD,
        )
        db.add(older_snapshot)
        source_file = await db.scalar(
            select(WorkspaceFile).where(WorkspaceFile.workspace_id == ids["workspace_id"])
        )
        file_id, file_hash = source_file.id, source_file.content_hash
        decision = await db.scalar(select(ReviewDecision))
        decision.revision = 2
        await db.flush()
        older = ReviewDecision(
            submission_id=ids["submission_id"],
            reviewer_id=ids["teacher_id"],
            revision=1,
            grade=7,
            comment="Historical unique feedback",
            status="SUPERSEDED",
            lms_export_state="IMPORTED",
            created_at=OLD,
            updated_at=OLD,
        )
        db.add(older)
        await db.flush()
        await db.execute(update(ReviewDecision).values(updated_at=OLD))
        status = await service.get_storage_status(db, settings)
        monkeypatch.setattr(service, "_GIB", 1)
        result = await service.cleanup_storage(
            db, settings, category="quota", eligible_before=status.eligible_before, automatic=True
        )
        assert [(item.category, item.removed_records) for item in result.categories] == [
            ("edit_history", 2),
            ("teacher_ai_chats", 1),
            ("synced_submissions", 1),
        ]
        assert result.over_limit is True  # protected metadata is never force-deleted
        assert await db.scalar(select(func.count(EditEvent.id))) == 0
        assert await db.get(ChatThread, chat_id) is None
        assert await db.get(ChatThread, student_chat) is not None
        assert await db.get(ChatThread, open_chat) is not None
        submission = await db.get(Submission, ids["submission_id"])
        assert submission.payload_evicted_at is not None
        assert submission.snapshot_id == ids["snapshot_id"]
        assert snapshot.files == [] and snapshot.manifest_hash == manifest
        assert older_snapshot.files[0]["content"] == "unique old source"
        assert source_file.id == file_id and source_file.content_hash == file_hash
        assert source_file.content == ""
        assert decision.comment == "" and decision.grade == 8.5
        assert older.comment == "Historical unique feedback"
        assert (await db.get(Attempt, ids["attempt_id"])).integrity_policy[
            "history_pruned_events"
        ] == 2


@pytest.mark.parametrize(
    "protection",
    [
        "active",
        "claim",
        "draft",
        "run",
        "ai",
        "outbox",
        "missing_mapping",
        "incomplete",
        "native",
        "deleted_mapping",
    ],
)
async def test_protected_data_is_never_evicted(storage_bundle, protection):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        if protection == "active":
            await db.execute(update(Attempt).values(state="ACTIVE"))
        elif protection == "claim":
            db.add(
                ReviewClaim(
                    submission_id=ids["submission_id"],
                    owner_id=ids["teacher_id"],
                    lease_expires_at=utcnow() + timedelta(hours=1),
                    state="ACTIVE",
                )
            )
        elif protection == "draft":
            db.add(
                ReviewDraft(
                    submission_id=ids["submission_id"],
                    owner_id=ids["teacher_id"],
                    comment="Do not delete",
                )
            )
        elif protection == "run":
            db.add(
                RunRequest(
                    submission_id=ids["submission_id"],
                    requested_by_id=ids["teacher_id"],
                    revision=0,
                    build_profile="cpp",
                    status="QUEUED",
                )
            )
        elif protection == "ai":
            db.add(
                AICodeReview(
                    submission_id=ids["submission_id"],
                    snapshot_id=ids["snapshot_id"],
                    requested_by_id=ids["teacher_id"],
                    manifest_hash="a" * 64,
                    state="RUNNING",
                )
            )
        elif protection == "outbox":
            db.add(
                SyncOutbox(
                    connection_id=ids["connection_id"],
                    attempt_id=ids["attempt_id"],
                    aggregate_id=ids["submission_id"],
                    aggregate_type="Submission",
                    event_type="review.decision",
                    idempotency_key=uuid.uuid4().hex,
                    state="FAILED",
                )
            )
        elif protection == "missing_mapping":
            mapping = await db.scalar(
                select(ExternalMapping).where(
                    ExternalMapping.external_type == "moodle_historical_submission"
                )
            )
            mapping.external_type = "not_recoverable"
        elif protection == "incomplete":
            submission = await db.get(Submission, ids["submission_id"])
            submission.external_receipt = {**submission.external_receipt, "source_complete": False}
            submission.updated_at = OLD
        elif protection == "native":
            await db.execute(update(Submission).values(source="STUDENT"))
        elif protection == "deleted_mapping":
            mapping = await db.scalar(
                select(ExternalMapping).where(
                    ExternalMapping.external_type == "moodle_historical_submission"
                )
            )
            mapping.metadata_json = {**mapping.metadata_json, "deleted_in_moodle": True}
        await db.flush()
        for model in (Attempt, Submission):
            await db.execute(update(model).values(updated_at=OLD))
        status = await service.get_storage_status(db, settings)
        assert (
            next(
                item for item in status.categories if item.category == "synced_submissions"
            ).eligible_records
            == 0
        )
        result = await service.cleanup_storage(
            db, settings, category="synced_submissions", eligible_before=status.eligible_before
        )
        assert result.removed_records == 0
        assert (await db.get(Snapshot, ids["snapshot_id"])).files


async def test_closed_chats_have_grace_period_and_teacher_scope(storage_bundle):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        removed = await _chat(db, ids)
        kept = [
            await _chat(db, ids, status="OPEN"),
            await _chat(db, ids, recent=True),
            await _chat(db, ids, mode="STUDENT"),
        ]
        status = await service.get_storage_status(db, settings)
        result = await service.cleanup_storage(
            db, settings, category="teacher_ai_chats", eligible_before=status.eligible_before
        )
        assert result.removed_records == 1
        assert await db.get(ChatThread, removed) is None
        for thread_id in kept:
            assert await db.get(ChatThread, thread_id) is not None


async def test_archived_work_rehydrates_only_on_explicit_complete_sync(storage_bundle):
    from app.models.courses import Course
    from app.models.tasks import Assessment

    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        status = await service.get_storage_status(db, settings)
        result = await service.cleanup_storage(
            db, settings, category="synced_submissions", eligible_before=status.eligible_before
        )
        assert result.removed_records == 1
        args = dict(
            course=await db.get(Course, ids["course_id"]),
            assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42",
        )
        submission = await db.get(Submission, ids["submission_id"])
        assert submission.payload_evicted_at is not None
        same = await _materialize_flat_historical_submissions(db, **args, items=[_finished_item()])
        assert same.unchanged == 1 and submission.payload_evicted_at is not None
        incomplete = copy.deepcopy(_finished_item())
        incomplete["responses"][0]["answer_complete"] = False
        incomplete["responses"][0]["answer_omission_reason"] = "DOWNLOAD_FAILED"
        await _materialize_flat_historical_submissions(
            db, **args, items=[incomplete], restore_evicted=True
        )
        assert submission.payload_evicted_at is not None
        restored = await _materialize_flat_historical_submissions(
            db, **args, items=[_finished_item()], restore_evicted=True
        )
        assert restored.updated == 1
        assert submission.payload_evicted_at is None and submission.id == ids["submission_id"]
        snapshot = await db.get(Snapshot, submission.snapshot_id)
        assert snapshot.files and "int main()" in snapshot.files[0]["content"]
        latest = await db.scalar(select(ReviewDecision).order_by(ReviewDecision.revision.desc()))
        assert latest.comment == "Checked in Moodle" and latest.grade == 8.5


async def test_cutoff_and_bounded_cleanup(storage_bundle, monkeypatch):
    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        await _event(db, ids, sequence=1)
        await _event(db, ids, sequence=2)
        with pytest.raises(DomainError) as error:
            await service.cleanup_storage(
                db, settings, category="edit_history", eligible_before=utcnow()
            )
        assert error.value.code == "STORAGE_CUTOFF_INVALID"
        monkeypatch.setattr(service, "_MAX_RECORDS", 1)
        status = await service.get_storage_status(db, settings)
        result = await service.cleanup_storage(
            db, settings, category="edit_history", eligible_before=status.eligible_before
        )
        assert result.removed_records == 1
        assert await db.scalar(select(func.count(EditEvent.id))) == 1


async def test_oldest_shared_attempt_does_not_starve_later_recoverable_answer(storage_bundle):
    from app.models.courses import Course
    from app.models.tasks import Assessment

    sessions, settings, ids = storage_bundle
    async with sessions() as db, db.begin():
        first = await db.get(Submission, ids["submission_id"])
        db.add(
            Submission(
                attempt_id=first.attempt_id,
                snapshot_id=first.snapshot_id,
                revision=2,
                source="MOODLE_IMPORT",
                lms_export_state="IMPORTED",
            )
        )
        item = copy.deepcopy(_finished_item())
        item.update(
            external_id="quiz:777:attempt:124",
            attempt_id="attempt:124",
            submitted_at_epoch=item["submitted_at_epoch"] + 100,
        )
        await _materialize_flat_historical_submissions(
            db,
            course=await db.get(Course, ids["course_id"]),
            assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42",
            items=[item],
        )
        for model in (Attempt, Submission, ReviewDecision):
            await db.execute(update(model).values(updated_at=OLD))
        later = await db.scalar(select(Submission).where(Submission.attempt_id != first.attempt_id))
        status = await service.get_storage_status(db, settings)
        assert (
            next(
                item for item in status.categories if item.category == "synced_submissions"
            ).eligible_records
            == 1
        )
        result = await service.cleanup_storage(
            db, settings, category="synced_submissions", eligible_before=status.eligible_before
        )
        assert result.removed_records == 1
        assert first.payload_evicted_at is None
        assert later.payload_evicted_at is not None


def test_storage_migration_preserves_existing_submission_ids_and_payload():
    migration = importlib.import_module("app.db.migrations.versions.20261019_0028_storage_eviction")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "core_submission",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("external_receipt", sa.JSON(), nullable=False),
    )
    row_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(
                table.insert().values(id=row_id, external_receipt={"preserve": True})
            )
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            columns = {
                item["name"] for item in sa.inspect(connection).get_columns("core_submission")
            }
            assert "payload_evicted_at" in columns
            assert connection.scalar(sa.select(table.c.external_receipt)) == {"preserve": True}
            assert connection.scalar(sa.select(table.c.id)) == row_id
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "payload_evicted_at" not in {
                item["name"] for item in sa.inspect(connection).get_columns("core_submission")
            }
    finally:
        engine.dispose()
