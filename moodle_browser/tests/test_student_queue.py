from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState
from moodle_browser.service import BrowserBusy, MoodleBrowserService


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
