from __future__ import annotations

import asyncio
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState
from moodle_browser.service import BrowserBusy, BrowserUnavailable, MoodleBrowserService


def state(student: int) -> BrowserStorageState:
    return BrowserStorageState.model_validate(
        {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": f"student-{student}",
                    "domain": "edu.mmcs.sfedu.ru",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }
    )


def service() -> MoodleBrowserService:
    result = MoodleBrowserService(
        Settings(
            shared_secret=b"student-queue-regression-secret-32bytes",
            queue_wait_seconds=0.05,
            student_queue_wait_seconds=2,
        )
    )
    result._browser = SimpleNamespace(is_connected=lambda: True)
    return result


@pytest.mark.asyncio
async def test_twenty_students_queue_without_busy_errors_during_course_import():
    connector = service()
    completed = []
    active = 0
    peak = 0

    async def student_operation(student):
        nonlocal active, peak
        async with (
            connector._student_session_operation(state(student)),
            connector._operation(foreground=True, student=True),
        ):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            completed.append(student)
            active -= 1

    async with connector._operation():  # A long-running teacher import.
        tasks = [asyncio.create_task(student_operation(student)) for student in range(20)]
        await asyncio.sleep(0)
        # Login retains its reserved slot even with twenty student requests queued.
        async with connector._operation(interactive=True):
            pass
        await asyncio.gather(*tasks)
    assert completed == list(range(20))
    assert peak == 1  # Waiters must not each open another Chromium context.
    assert connector._pending_student_operations == 0
    assert not connector._student_session_locks


@pytest.mark.asyncio
async def test_cancelling_queued_students_releases_all_admission_permits():
    connector = service()
    async with connector._operation(), connector._operation(foreground=True, student=True):

        async def waiter():
            async with connector._operation(foreground=True, student=True):
                pytest.fail("Occupied student permit must not be acquired")

        waiting = asyncio.create_task(waiter())
        await asyncio.sleep(0.01)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert connector._pending_student_operations == 1
    assert connector._pending_student_operations == 0
    async with connector._operation(foreground=True, student=True):
        pass


@pytest.mark.asyncio
async def test_student_queue_is_bounded_and_background_still_fails_fast():
    connector = service()
    connector._pending_student_operations = connector.settings.max_pending_student_operations
    with pytest.raises(BrowserBusy, match="queue is full"):
        async with connector._operation(foreground=True, student=True):
            pytest.fail("Queue overflow must be rejected")
    assert (
        connector._pending_student_operations == connector.settings.max_pending_student_operations
    )
    async with connector._operation():
        with pytest.raises(BrowserBusy):
            async with connector._operation():
                pytest.fail("Background import must not occupy the reserved student slot")


@pytest.mark.asyncio
async def test_total_student_budget_cancels_hung_work_and_releases_session_and_pool():
    connector = service()
    # Speed up this scheduling test without relaxing production config validation.
    connector.settings = SimpleNamespace(
        **{**asdict(connector.settings), "student_operation_timeout_seconds": 0.03}
    )
    cancelled = asyncio.Event()

    with pytest.raises(BrowserUnavailable, match="total time budget"):
        async with (
            connector._student_session_operation(state(1), terminal=True),
            connector._operation(foreground=True, student=True, lightweight=True),
        ):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
    assert cancelled.is_set()
    assert connector._pending_student_operations == 0
    assert not connector._student_session_locks
    async with (
        connector._student_session_operation(state(1), terminal=True),
        connector._operation(foreground=True, student=True, lightweight=True),
    ):
        pass


@pytest.mark.asyncio
async def test_total_budget_also_bounds_admission_before_a_browser_is_opened():
    connector = service()
    connector.settings = SimpleNamespace(
        **{**asdict(connector.settings), "student_operation_timeout_seconds": 0.03}
    )
    permits = connector.settings.max_concurrent_student_operations
    for _ in range(permits):
        await connector._student_http_semaphore.acquire()
    try:
        with pytest.raises(BrowserUnavailable, match="total time budget"):
            async with (
                connector._student_session_operation(state(1), terminal=True),
                connector._operation(foreground=True, student=True, lightweight=True),
            ):
                pytest.fail("Browser capacity is occupied")
        assert connector._pending_student_operations == 0
        assert not connector._student_session_locks
    finally:
        for _ in range(permits):
            connector._student_http_semaphore.release()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_context_setup_error_or_cancellation_closes_unreturned_context(cancel):
    connector = service()
    routing = asyncio.Event()
    closed = asyncio.Event()

    class Context:
        def set_default_timeout(self, value):
            pass

        def set_default_navigation_timeout(self, value):
            pass

        def on(self, *args):
            pass

        async def route(self, *args):
            routing.set()
            if cancel:
                await asyncio.Event().wait()
            raise RuntimeError("Fixture route setup failure")

        async def close(self):
            closed.set()

    async def new_context(**kwargs):
        return Context()

    task = asyncio.create_task(connector._new_context(SimpleNamespace(new_context=new_context)))
    await routing.wait()
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="Fixture route"):
            await task
    assert closed.is_set()
