from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import event, func, select

from app.models.attempts import Attempt, Workspace, WorkspaceFile
from app.models.courses import Course
from app.models.identity import ExternalPrincipal
from app.models.integration import SyncOutbox
from app.services import sync
from app.services.common import sha256_text
from app.services.workspace import refresh_workspace_hash
from tests.test_sync_workers import _seed_attempt


async def _seed_class(sessions, now, count):
    first_id, course_id = await _seed_attempt(
        sessions,
        now=now,
        deadline=now - timedelta(seconds=1),
    )
    ids = [first_id]
    async with sessions() as db, db.begin():
        first = await db.get(Attempt, first_id)
        course = await db.get(Course, course_id)
        for index in range(1, count):
            principal = ExternalPrincipal(
                connection_id=course.connection_id,
                external_subject=f"student-{index}",
                display_name=f"Student {index}",
            )
            db.add(principal)
            await db.flush()
            attempt = Attempt(
                assessment_id=first.assessment_id,
                principal_id=principal.id,
                started_at=first.started_at,
                deadline_at=first.deadline_at,
                current_revision=1,
            )
            db.add(attempt)
            await db.flush()
            workspace = Workspace(attempt_id=attempt.id, current_revision=1)
            db.add(workspace)
            await db.flush()
            content = f"int main() {{ return {index}; }}\n"
            db.add(
                WorkspaceFile(
                    workspace_id=workspace.id,
                    path="main.cpp",
                    language="CPP",
                    content=content,
                    content_hash=sha256_text(content),
                )
            )
            await db.flush()
            await refresh_workspace_hash(db, workspace)
            ids.append(attempt.id)
    return ids


async def test_twenty_deadline_submissions_commit_independently(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    now = datetime.now(UTC)
    await _seed_class(sessions, now, 20)
    maintain = sync.maintain_attempts
    committed_before_next = []

    async def observe(db, config, **kwargs):
        # This session has not executed any SQL yet. Previously the scheduler
        # reused one transaction for the entire class, retaining every row lock.
        async with sessions() as check:
            committed_before_next.append(
                await check.scalar(
                    select(func.count(Attempt.id)).where(Attempt.state == "AUTO_SUBMITTED")
                )
            )
        assert len(kwargs["attempt_ids"]) == 1
        return await maintain(db, config, **kwargs)

    monkeypatch.setattr(sync, "maintain_attempts", observe)
    result = await sync.run_scheduler_iteration(
        sessions,
        settings,
        now=now,
    )
    assert committed_before_next == list(range(20))
    assert result.attempts_submitted == result.checkpoints_enqueued == 20
    async with sessions() as db:
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 20


async def test_broken_attempt_does_not_rollback_or_block_other_deadlines(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    now = datetime.now(UTC)
    ids = await _seed_class(sessions, now, 3)
    maintain = sync.maintain_attempts

    async def with_one_failure(db, config, **kwargs):
        if kwargs["attempt_ids"] == [ids[1]]:
            raise ValueError("malformed attempt fixture")
        return await maintain(db, config, **kwargs)

    monkeypatch.setattr(sync, "maintain_attempts", with_one_failure)
    result = await sync.run_scheduler_iteration(
        sessions,
        settings,
        now=now,
    )
    assert result.attempts_submitted == 2
    async with sessions() as db:
        assert (await db.get(Attempt, ids[1])).state == "ACTIVE"
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 2


async def test_scheduler_does_not_load_historical_source_manifests_when_not_due(app_bundle):
    _, sessions, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        sessions,
        now=now,
        deadline=now + timedelta(seconds=30),
    )
    async with sessions() as db, db.begin():
        course = await db.get(Course, course_id)
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course_id,
                attempt_id=attempt_id,
                event_type="attempt.checkpoint",
                aggregate_type="Attempt",
                aggregate_id=attempt_id,
                idempotency_key=f"historical:{attempt_id}",
                created_at=now,
                payload={"reason": "FINAL_MINUTE", "manifest_json": "old source" * 10000},
            )
        )

    statements = []
    engine = sessions.kw["bind"].sync_engine

    def record(_connection, _cursor, statement, _params, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement.split("FROM", 1)[0].lower())

    event.listen(engine, "before_cursor_execute", record)
    try:
        result = await sync.run_scheduler_iteration(
            sessions,
            settings,
            now=now,
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert result.checkpoints_enqueued == 0
    assert not any("payload" in projection for projection in statements)
