from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import Settings
from app.db.base import utcnow
from app.models.integration import AuditEntry
from app.schemas.system import StorageCleanupRead
from app.workers import scheduler

GIB = 1024 ** 3


def _status(used_bytes: int = 129 * GIB):
    return SimpleNamespace(
        used_bytes=used_bytes, storage_limit_gb=128, eligible_before=utcnow(),
    )


def _result(*, removed=1, over_limit=False):
    return StorageCleanupRead(
        used_bytes_before=129 * GIB,
        used_bytes_after=127 * GIB if not over_limit else 129 * GIB,
        reclaimed_bytes=2 * GIB if not over_limit else 0,
        over_limit=over_limit,
        removed_records=removed,
        categories=[],
    )


def test_storage_maintenance_interval_defaults_and_environment(monkeypatch):
    monkeypatch.delenv("STORAGE_CLEANUP_INTERVAL_SECONDS", raising=False)
    assert Settings(_env_file=None).storage_cleanup_interval_seconds == 900
    monkeypatch.setenv("STORAGE_CLEANUP_INTERVAL_SECONDS", "1800")
    assert Settings(_env_file=None).storage_cleanup_interval_seconds == 1800
    monkeypatch.setenv("STORAGE_CLEANUP_INTERVAL_SECONDS", "5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("used_bytes", [0, 127 * GIB, 128 * GIB])
async def test_under_quota_never_cleans_or_writes_audit(app_bundle, monkeypatch, used_bytes):
    _, sessions, settings = app_bundle
    cleanup = AsyncMock()
    monkeypatch.setattr(
        scheduler, "get_storage_status", AsyncMock(return_value=_status(used_bytes)),
    )
    monkeypatch.setattr(scheduler, "cleanup_storage", cleanup)
    await scheduler.run_storage_maintenance_once(sessions, settings)
    cleanup.assert_not_awaited()
    async with sessions() as db:
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


async def test_over_quota_uses_confirmed_cutoff_and_anonymous_count_only_audit(
    app_bundle, monkeypatch,
):
    _, sessions, settings = app_bundle
    status = _status()
    result = _result()
    cleanup = AsyncMock(return_value=result)
    monkeypatch.setattr(scheduler, "get_storage_status", AsyncMock(return_value=status))
    monkeypatch.setattr(scheduler, "cleanup_storage", cleanup)
    await scheduler.run_storage_maintenance_once(sessions, settings)
    assert cleanup.await_args.kwargs == {
        "category": "quota", "eligible_before": status.eligible_before, "automatic": True,
    }
    async with sessions() as db:
        audit = await db.scalar(select(AuditEntry))
        assert audit.actor_id is None
        assert audit.action == "storage.cleaned_up"
        assert audit.metadata_json == {"automatic": True, **result.model_dump(mode="json")}


async def test_protected_data_over_quota_warns_without_repeated_empty_audit(
    app_bundle, monkeypatch, caplog,
):
    _, sessions, settings = app_bundle
    monkeypatch.setattr(scheduler, "get_storage_status", AsyncMock(return_value=_status()))
    monkeypatch.setattr(
        scheduler, "cleanup_storage", AsyncMock(return_value=_result(removed=0, over_limit=True)),
    )
    await scheduler.run_storage_maintenance_once(sessions, settings)
    assert "storage quota remains exceeded" in caplog.text
    async with sessions() as db:
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


async def test_cadence_is_sequential_and_independent_of_deadline_poll(monkeypatch):
    settings = SimpleNamespace(storage_cleanup_interval_seconds=900, scheduler_poll_seconds=5)
    calls = []
    active = 0

    async def maintain(_sessions, _settings):
        nonlocal active
        active += 1
        assert active == 1
        calls.append("scan")
        await asyncio.sleep(0)
        active -= 1

    async def sleep(seconds):
        calls.append(seconds)
        assert active == 0
        if calls.count("scan") == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(scheduler, "run_storage_maintenance_once", maintain)
    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_storage_maintenance(None, settings, sleep=sleep)
    assert calls == ["scan", 900, "scan", 900, "scan", 900]


async def test_failure_rolls_back_and_future_scan_can_run_without_logging_payloads(
    app_bundle, monkeypatch, caplog,
):
    _, sessions, settings = app_bundle
    status = AsyncMock(side_effect=[_status(), _status(127 * GIB)])
    monkeypatch.setattr(scheduler, "get_storage_status", status)

    async def fail(db, _settings, **_kwargs):
        db.add(AuditEntry(action="must_rollback"))
        await db.flush()
        raise RuntimeError("private transcript, source code and SQL parameters")

    monkeypatch.setattr(scheduler, "cleanup_storage", fail)
    sleeps = 0

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        async with sessions() as db:
            assert await db.scalar(select(func.count(AuditEntry.id))) == 0
        if sleeps == 2:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await scheduler.run_storage_maintenance(sessions, settings, sleep=sleep)
    assert status.await_count == 2
    assert "error_type=RuntimeError" in caplog.text
    assert "private transcript" not in caplog.text
    assert "SQL parameters" not in caplog.text


async def test_cancelling_inflight_cleanup_rolls_back_its_transaction(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    entered = asyncio.Event()
    monkeypatch.setattr(scheduler, "get_storage_status", AsyncMock(return_value=_status()))

    async def block(db, _settings, **_kwargs):
        db.add(AuditEntry(action="must_rollback"))
        await db.flush()
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(scheduler, "cleanup_storage", block)
    task = asyncio.create_task(scheduler.run_storage_maintenance_once(sessions, settings))
    await asyncio.wait_for(entered.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with sessions() as db:
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


class _Engine:
    @asynccontextmanager
    async def connect(self):
        yield object()


async def test_scheduler_deadlines_continue_and_cleanup_stops_before_unlock(monkeypatch):
    started, stopped, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    iterations = []
    result = object()

    async def maintenance(_sessions, _settings):
        started.set()
        try:
            await asyncio.Future()
        finally:
            stopped.set()

    async def iterate(_sessions, _settings):
        await asyncio.wait_for(started.wait(), timeout=2)
        iterations.append(len(iterations) + 1)
        if len(iterations) == 2:
            stop.set()
        return result

    async def release(_connection):
        assert stopped.is_set()

    monkeypatch.setattr(scheduler, "acquire_scheduler_lock", AsyncMock(return_value=True))
    monkeypatch.setattr(scheduler, "release_scheduler_lock", release)
    monkeypatch.setattr(scheduler, "run_storage_maintenance", maintenance)
    monkeypatch.setattr(scheduler, "run_scheduler_iteration", iterate)
    assert await scheduler.run_scheduler(
        _Engine(), None, SimpleNamespace(scheduler_poll_seconds=5),
        stop_event=stop, sleep=AsyncMock(),
    ) is result
    assert iterations == [1, 2]
    assert stopped.is_set()


async def test_scheduler_once_and_non_owner_never_start_cleanup(monkeypatch):
    maintenance, release = AsyncMock(), AsyncMock()
    acquire = AsyncMock(return_value=True)
    iteration = AsyncMock(return_value=object())
    monkeypatch.setattr(scheduler, "acquire_scheduler_lock", acquire)
    monkeypatch.setattr(scheduler, "release_scheduler_lock", release)
    monkeypatch.setattr(scheduler, "run_storage_maintenance", maintenance)
    monkeypatch.setattr(scheduler, "run_scheduler_iteration", iteration)
    await scheduler.run_scheduler(
        _Engine(), None, SimpleNamespace(scheduler_poll_seconds=5), once=True,
    )
    maintenance.assert_not_called()
    release.assert_awaited_once()
    acquire.return_value = False
    assert await scheduler.run_scheduler(
        _Engine(), None, SimpleNamespace(scheduler_poll_seconds=5),
    ) is None
    maintenance.assert_not_called()
    assert iteration.await_count == 1
    assert release.await_count == 1
