from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState, CourseDiscoverRequest
from moodle_browser.service import BrowserBusy, MoodleBrowserService


class CourseContext:
    def __init__(self, state: BrowserStorageState) -> None:
        self.state = state
        self.closed = False

    async def new_page(self) -> object:
        return object()

    async def close(self) -> None:
        self.closed = True


class ControlledCourseService(MoodleBrowserService):
    """Keep real discovery/admission/parsing, stub only Moodle/browser I/O."""

    def __init__(self, **settings: Any) -> None:
        super().__init__(
            Settings(shared_secret=b"x" * 32, queue_wait_seconds=0.05, **settings),
        )
        self._browser = SimpleNamespace(is_connected=lambda: True)
        self.entered = {course: asyncio.Event() for course in ("549", "550")}
        self.release = {course: asyncio.Event() for course in ("549", "550")}
        self.contexts: list[CourseContext] = []

    async def _new_context(self, _browser: object, *, storage_state, html_only=False):
        assert html_only
        context = CourseContext(storage_state)
        self.contexts.append(context)
        return context

    async def _goto(self, _page: object, url: str) -> str:
        course_id = parse_qs(urlsplit(url).query)["id"][0]
        html = (Path(__file__).parent / "fixtures" / "course.html").read_text()
        return html.replace("id=549", f"id={course_id}")

    async def _authenticated(self, _context: object, _html: str):
        return True, ""

    async def _crawl_course_section_pages(
        self,
        _page: object,
        _context: object,
        _course_id: str,
        _html: str,
        course,
    ):
        return course

    async def _participants(self, _page: object, _context: object, course_id: str):
        self.entered[course_id].set()
        await self.release[course_id].wait()
        return [
            {
                "user_id": "42",
                "display_name": "Преподаватель",
                "role": "TEACHER",
                "roles": ["TEACHER"],
                "groups": [],
            }
        ], True

    async def _enrich_course_activities(
        self,
        _page: object,
        _context: object,
        _course_id: str,
        course,
        **_kwargs,
    ):
        raise AssertionError("Course catalog synchronization must not read activity settings")

    async def _state(self, context: CourseContext) -> dict[str, Any]:
        return context.state.model_dump(mode="json")


@pytest.mark.asyncio
async def test_course_queue_is_bounded_and_cancelled_reader_releases_capacity():
    connector = ControlledCourseService(
        max_concurrent_course_reads=1, course_queue_wait_seconds=0.05
    )
    first = asyncio.create_task(connector.discover_course(request("549", interactive=True)))
    try:
        await asyncio.wait_for(connector.entered["549"].wait(), 1)
        with pytest.raises(BrowserBusy):
            await connector.discover_course(request("550", interactive=True))
        assert connector._pending_course_reads == connector._active_course_reads == 1
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert connector._pending_course_reads == connector._active_course_reads == 0
        assert connector.contexts[0].closed
        connector.release["550"].set()
        assert (
            await connector.discover_course(request("550", interactive=True))
        ).discovery.external_id == "550"
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


def request(course_id: str, *, interactive: bool) -> CourseDiscoverRequest:
    return CourseDiscoverRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": "https://edu.mmcs.sfedu.ru",
            "external_id": course_id,
            "actor_external_subject": "42",
            "interactive": interactive,
            "storage_state": {
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": "same-teacher-session",
                        "domain": "edu.mmcs.sfedu.ru",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            },
        }
    )


@pytest.mark.asyncio
async def test_course_catalog_returns_roster_and_activity_titles_without_enrichment():
    connector = ControlledCourseService()
    connector.release["549"].set()
    result = await connector.discover_course(request("549", interactive=True))
    preview = result.discovery.preview
    assert preview.membership_snapshot.complete
    assert preview.membership_snapshot.members[0].roles == ["TEACHER"]
    activities = [activity for section in preview.sections for activity in section.activities]
    assert {activity.cmid for activity in activities} == {777, 778}
    assert all(activity.title_confirmed for activity in activities)
    assert not any(activity.settings_confirmed for activity in activities)


@pytest.mark.asyncio
async def test_adding_course_uses_reserved_capacity_alongside_same_teacher_background_sync():
    connector = ControlledCourseService()
    background = asyncio.create_task(connector.discover_course(request("549", interactive=False)))
    foreground = None
    try:
        await asyncio.wait_for(connector.entered["549"].wait(), timeout=1)
        foreground = asyncio.create_task(
            connector.discover_course(request("550", interactive=True))
        )
        await asyncio.wait_for(connector.entered["550"].wait(), timeout=1)
        assert not background.done()
        assert not foreground.done()
        assert len(connector.contexts) == 2
        assert connector.contexts[0].state == connector.contexts[1].state

        # Parallel read-only discovery must neither take the login reserve nor
        # remove the cap on expensive background crawls.
        async with connector._operation(interactive=True):
            pass
        with pytest.raises(BrowserBusy):
            async with connector._operation():
                pytest.fail("Background capacity must remain bounded")

        connector.release["550"].set()
        added = await asyncio.wait_for(foreground, timeout=1)
        assert added.discovery.external_id == "550"
        assert added.discovery.actor_role == "TEACHER"
        assert not background.done()

        connector.release["549"].set()
        synced = await asyncio.wait_for(background, timeout=1)
        assert synced.discovery.external_id == "549"
        assert all(context.closed for context in connector.contexts)
        assert added.storage_state == synced.storage_state
    finally:
        tasks = [background] + ([foreground] if foreground is not None else [])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("heavy_limit", [1, 2, 3])
async def test_add_course_while_manual_refresh_and_heavy_pool_are_busy(heavy_limit: int):
    connector = ControlledCourseService(max_concurrent_operations=heavy_limit)
    manual_refresh = None
    addition = None
    try:
        # A manual sync is itself foreground work, not a background crawl.
        # It must not use the only slot available to adding another course.
        # Saturate the heavy pool as a history crawl plus concurrent logins
        # would, including an old NUC configuration with just one heavy slot.
        async with AsyncExitStack() as stack:
            for _ in range(heavy_limit):
                await stack.enter_async_context(connector._operation(interactive=True))
            manual_refresh = asyncio.create_task(
                connector.discover_course(request("549", interactive=True))
            )
            addition = asyncio.create_task(
                connector.discover_course(request("550", interactive=True))
            )
            await asyncio.wait_for(
                asyncio.gather(*(event.wait() for event in connector.entered.values())),
                timeout=1,
            )
            assert len(connector.contexts) == 2
            assert not manual_refresh.done()
            connector.release["550"].set()
            added = await asyncio.wait_for(addition, timeout=1)
            assert added.discovery.external_id == "550"
            assert not manual_refresh.done()
            connector.release["549"].set()
            await asyncio.wait_for(manual_refresh, timeout=1)
            assert all(context.closed for context in connector.contexts)
    finally:
        tasks = [task for task in (manual_refresh, addition) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_second_foreground_course_runs_during_manual_sync_and_history_import():
    connector = ControlledCourseService()
    tasks = []
    try:
        async with connector._operation():
            tasks.append(
                asyncio.create_task(connector.discover_course(request("549", interactive=True)))
            )
            await asyncio.wait_for(connector.entered["549"].wait(), timeout=1)
            tasks.append(
                asyncio.create_task(connector.discover_course(request("550", interactive=True)))
            )
            # Surface BrowserBusy directly instead of only timing out the event.
            entered = asyncio.create_task(connector.entered["550"].wait())
            tasks.append(entered)
            done, _ = await asyncio.wait(
                [tasks[1], entered], timeout=1, return_when=asyncio.FIRST_COMPLETED
            )
            if tasks[1] in done:
                await tasks[1]
            assert entered in done
            assert not tasks[0].done()
            connector.release["550"].set()
            assert (await tasks[1]).discovery.external_id == "550"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
