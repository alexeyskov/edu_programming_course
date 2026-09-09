from __future__ import annotations

import asyncio

import httpx

from app.workers import sync as sync_worker


async def test_sync_worker_uses_parallel_lanes_for_latency_sensitive_events(
    app_bundle,
    monkeypatch,
) -> None:
    _, session_factory, settings = app_bundle
    settings.sync_worker_concurrency = 2
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
