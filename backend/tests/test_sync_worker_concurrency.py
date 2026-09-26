from __future__ import annotations

import asyncio

import httpx
import pytest

from app.workers import sync as sync_worker


async def test_sync_worker_uses_parallel_lanes_for_latency_sensitive_events(
    app_bundle,
    monkeypatch,
) -> None:
    _, session_factory, settings = app_bundle
    settings.sync_worker_concurrency = 2
    settings.sync_terminal_concurrency = 0
    settings.sync_history_concurrency = 0
    stop = asyncio.Event()
    both_started = asyncio.Event()
    calls = 0
    active = 0
    maximum_active = 0

    async def process(*_args, **_kwargs) -> bool:  # type: ignore[no-untyped-def]
        nonlocal calls, active, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        if active == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)
        await asyncio.sleep(0)
        active -= 1
        stop.set()
        return True

    monkeypatch.setattr(sync_worker, "process_outbox_once", process)
    async with httpx.AsyncClient() as client:
        processed = await sync_worker.run_sync_worker(
            session_factory,
            settings,
            stop_event=stop,
            client=client,
        )

    assert calls == 2
    assert processed == 2
    assert maximum_active == 2


async def test_embedded_terminal_worker_uses_one_filtered_lane(
    app_bundle,
    monkeypatch,
) -> None:
    _, session_factory, settings = app_bundle
    stop = asyncio.Event()
    calls: list[bool] = []

    async def process(*_args, **kwargs) -> bool:  # type: ignore[no-untyped-def]
        calls.append(bool(kwargs.get("terminal_checkpoints_only")))
        stop.set()
        return True

    monkeypatch.setattr(sync_worker, "process_outbox_once", process)
    async with httpx.AsyncClient() as client:
        processed = await sync_worker.run_sync_worker(
            session_factory,
            settings,
            stop_event=stop,
            client=client,
            terminal_checkpoints_only=True,
            concurrency=1,
        )

    assert processed == 1
    assert calls == [True]


async def test_reserved_terminal_lanes_deliver_twenty_students_during_long_imports(
    app_bundle,
    monkeypatch,
):
    _, sessions, settings = app_bundle
    settings.sync_worker_concurrency = 2
    settings.sync_terminal_concurrency = 2
    settings.sync_history_concurrency = 0
    settings.sync_poll_seconds = 0.1
    stop = asyncio.Event()
    imports_started = asyncio.Event()
    active_imports = 0
    submissions = list(range(20))
    delivered = []

    async def process(*_args, terminal_checkpoints_only=False, **_kwargs):
        nonlocal active_imports
        if not terminal_checkpoints_only:
            active_imports += 1
            if active_imports == 2:
                imports_started.set()
            await stop.wait()
            return True
        await imports_started.wait()
        if not submissions:
            return False
        student = submissions.pop(0)
        await asyncio.sleep(0.001)
        delivered.append(student)
        if len(delivered) == 20:
            stop.set()
        return True

    monkeypatch.setattr(sync_worker, "process_outbox_once", process)
    async with httpx.AsyncClient() as client:
        await asyncio.wait_for(
            sync_worker.run_sync_worker(
                sessions,
                settings,
                stop_event=stop,
                client=client,
            ),
            2,
        )
    assert sorted(delivered) == list(range(20))
    assert active_imports == 2


async def test_history_lanes_progress_while_all_general_lanes_are_crawling_courses(
    app_bundle, monkeypatch
):
    _, sessions, settings = app_bundle
    settings.sync_worker_concurrency = 2
    settings.sync_terminal_concurrency = 0
    settings.sync_history_concurrency = 2
    stop = asyncio.Event()
    courses_started = asyncio.Event()
    active_courses = 0
    reports = list(range(20))
    imported = []

    async def process(*_args, history_imports_only=False, exclude_history_imports=False, **_kwargs):
        nonlocal active_courses
        if not history_imports_only:
            assert exclude_history_imports
            active_courses += 1
            if active_courses == 2:
                courses_started.set()
            await stop.wait()
            return True
        await courses_started.wait()
        assert not exclude_history_imports
        if not reports:
            return False
        report = reports.pop(0)
        await asyncio.sleep(0.001)
        imported.append(report)
        if len(imported) == 20:
            stop.set()
        return True

    monkeypatch.setattr(sync_worker, "process_outbox_once", process)
    async with httpx.AsyncClient() as client:
        await asyncio.wait_for(
            sync_worker.run_sync_worker(
                sessions,
                settings,
                stop_event=stop,
                client=client,
            ),
            5,
        )
    assert sorted(imported) == list(range(20))
    assert active_courses == 2


@pytest.mark.parametrize("mode", ["once", "explicit_concurrency", "no_reserved_history"])
async def test_general_lanes_still_read_history_when_no_history_lanes_exist(
    app_bundle, monkeypatch, mode,
):
    _, sessions, settings = app_bundle
    settings.sync_worker_concurrency = 1
    settings.sync_terminal_concurrency = 0
    settings.sync_history_concurrency = 0 if mode == "no_reserved_history" else 2
    stop = asyncio.Event()
    filters = []

    async def process(*_args, **kwargs):
        filters.append((kwargs.get("history_imports_only"), kwargs.get("exclude_history_imports")))
        stop.set()
        return True

    monkeypatch.setattr(sync_worker, "process_outbox_once", process)
    async with httpx.AsyncClient() as client:
        await sync_worker.run_sync_worker(
            sessions, settings, client=client, stop_event=stop,
            once=mode == "once",
            concurrency=1 if mode == "explicit_concurrency" else None,
        )
    assert len(filters) == 1
    assert not any(filters[0])
