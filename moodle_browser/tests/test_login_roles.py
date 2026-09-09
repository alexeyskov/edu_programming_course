from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from moodle_browser.config import Settings
from moodle_browser.service import (
    BrowserBusy,
    MoodleBrowserService,
    _activity_needs_assessment_detail,
    _dashboard_courses_in_catalog,
)

FIXTURES = Path(__file__).parent / "fixtures"


class FakePage:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.visited: list[str] = []

    async def evaluate(self, _script: str, argument: dict[str, Any]) -> dict[str, Any]:
        course_id = parse_qs(urlsplit(argument["url"]).query)["id"][0]
        self.visited.append(course_id)
        return self.responses[course_id]


def course_response(course_id: str, body: str) -> dict[str, Any]:
    return {
        "status": 200,
        "url": f"https://edu.mmcs.sfedu.ru/course/view.php?id={course_id}",
        "html": f"<a href='/login/logout.php?sesskey=opaque'>Выход</a>{body}",
    }


def test_login_intersects_catalog_with_the_users_moodle_dashboard() -> None:
    dashboard = """
      <a href="/course/view.php?id=549">C++ для преподавателей</a>
      <a href="/course/view.php?id=777">Другой курс пользователя</a>
    """

    courses = _dashboard_courses_in_catalog(
        dashboard,
        "https://edu.mmcs.sfedu.ru",
        ["549", "550"],
    )

    assert list(courses) == ["549"]
    assert courses["549"]["title"] == "C++ для преподавателей"


@pytest.mark.parametrize(
    ("module", "name", "section_title", "expected"),
    [
        ("quiz", "Самостоятельная работа №1", "Проверочные", True),
        ("assign", "Работа №1", "Лабораторные работы", True),
        ("quiz", "Exam 1", "General", True),
        ("quiz", "Самостоятельная работа №1", "АРХИВ", False),
        ("assign", "Archive lab", "General", False),
        ("quiz", "Индивидуальное задание", "Индивидуальные", False),
        ("resource", "Лабораторная работа", "Лабораторные", False),
    ],
)
def test_activity_detail_selection_matches_materialized_assessment_families(
    module: str,
    name: str,
    section_title: str,
    expected: bool,
) -> None:
    assert (
        _activity_needs_assessment_detail(
            {"module": module, "name": name},
            section_title,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_course_enrichment_skips_details_for_archived_and_unclassified_activities() -> None:
    class EnrichmentService(MoodleBrowserService):
        def __init__(self) -> None:
            super().__init__(Settings(shared_secret=b"x" * 32))
            self.urls: list[str] = []

        async def _fetch_bounded_html(
            self,
            _page: object,
            _context: object,
            url: str,
            *,
            expected_path: str,
            expected_query: dict[str, list[str]],
        ) -> None:
            self.urls.append(url)
            assert expected_path
            assert expected_query
            return None

    course = {
        "sections": [
            {
                "external_id": "archive",
                "title": "АРХИВ",
                "activities": [
                    {"cmid": 1, "module": "quiz", "name": "Самостоятельная работа"},
                ],
            },
            {
                "external_id": "individual",
                "title": "Индивидуальные задания",
                "activities": [
                    {"cmid": 2, "module": "assign", "name": "Задание 1"},
                ],
            },
            {
                "external_id": "current",
                "title": "Проверочные (самостоятельные работы)",
                "activities": [
                    {"cmid": 30354, "module": "quiz", "name": "Самостоятельная работа №1"},
                ],
            },
        ]
    }
    service = EnrichmentService()

    enriched = await service._enrich_course_activities(  # type: ignore[arg-type]
        object(), object(), "549", course, participant_ids=frozenset()
    )

    assert len(service.urls) == 2
    assert all("30354" in url for url in service.urls)
    assert [
        activity["cmid"] for section in enriched["sections"] for activity in section["activities"]
    ] == [1, 2, 30354]
    assert enriched["sections"][0]["activities"][0]["import_supported"] is False


@pytest.mark.asyncio
async def test_background_crawl_cannot_consume_reserved_login_slot() -> None:
    service = MoodleBrowserService(
        Settings(
            shared_secret=b"x" * 32,
            max_concurrent_operations=3,
            queue_wait_seconds=0.05,
        )
    )

    class ConnectedBrowser:
        def is_connected(self) -> bool:
            return True

    service._browser = ConnectedBrowser()  # type: ignore[assignment]
    background_entered = asyncio.Event()
    release_background = asyncio.Event()

    async def hold_background_slot() -> None:
        async with service._operation():
            background_entered.set()
            await release_background.wait()

    background = asyncio.create_task(hold_background_slot())
    await background_entered.wait()
    try:
        # Interactive login still enters immediately while discovery is active.
        async with service._operation(interactive=True):
            pass
        # A manual synchronization also has its own foreground capacity.
        async with service._operation(foreground=True):
            pass
        # A second background crawl is bounded instead of consuming the slot
        # reserved for login.
        with pytest.raises(BrowserBusy):
            async with service._operation():
                pass
    finally:
        release_background.set()
        await background


@pytest.mark.asyncio
async def test_queued_background_crawl_does_not_consume_foreground_slot() -> None:
    service = MoodleBrowserService(
        Settings(
            shared_secret=b"x" * 32,
            max_concurrent_operations=3,
            queue_wait_seconds=0.2,
        )
    )

    class ConnectedBrowser:
        def is_connected(self) -> bool:
            return True

    service._browser = ConnectedBrowser()  # type: ignore[assignment]
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def hold_first_background_slot() -> None:
        async with service._operation():
            first_entered.set()
            await release_first.wait()

    async def wait_for_background_slot() -> None:
        async with service._operation():
            second_entered.set()

    first = asyncio.create_task(hold_first_background_slot())
    await first_entered.wait()
    second = asyncio.create_task(wait_for_background_slot())
    # Give the second request a chance to queue on the background-only gate.
    # It must not reserve a general non-login permit while it waits there.
    await asyncio.sleep(0)
    try:
        async with service._operation(foreground=True):
            pass
        assert not second_entered.is_set()
    finally:
        release_first.set()
        await first
        await second
    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_login_course_pages_confirm_teacher_student_and_bound_unknown() -> None:
    settings = Settings(
        shared_secret=b"x" * 32,
        base_url="https://edu.mmcs.sfedu.ru",
        max_login_course_role_pages=2,
    )
    service = MoodleBrowserService(settings)
    page = FakePage(
        {
            "549": course_response("549", "<a href='/course/edit.php?id=549'>Настройки</a>"),
            "550": course_response("550", "<main><h1>Курс студента</h1></main>"),
        }
    )
    courses = {
        "549": {"external_id": "549", "title": "C++", "short_name": "", "role": "UNKNOWN"},
        "550": {
            "external_id": "550",
            "title": "Algorithms",
            "short_name": "",
            "role": "UNKNOWN",
        },
        "551": {
            "external_id": "551",
            "title": "Outside bound",
            "short_name": "",
            "role": "UNKNOWN",
        },
    }

    await service._classify_login_course_roles(page, courses)  # type: ignore[arg-type]

    assert page.visited == ["549", "550"]
    assert courses["549"]["role"] == "TEACHER"
    assert courses["550"]["role"] == "STUDENT"
    assert courses["551"]["role"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_login_role_probe_does_not_trust_a_redirected_course_page() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    page = FakePage(
        {
            "549": {
                "status": 200,
                "url": "https://edu.mmcs.sfedu.ru/my/",
                "html": (
                    "<a href='/login/logout.php?sesskey=opaque'>Выход</a>"
                    "<a href='/course/edit.php?id=549'>Настройки</a>"
                ),
            }
        }
    )
    courses = {"549": {"external_id": "549", "title": "C++", "short_name": "", "role": "UNKNOWN"}}

    await service._classify_login_course_roles(page, courses)  # type: ignore[arg-type]

    assert courses["549"]["role"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_login_role_probe_requires_authenticated_markup() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    page = FakePage(
        {
            "549": {
                "status": 200,
                "url": "https://edu.mmcs.sfedu.ru/course/view.php?id=549",
                "html": "<a href='/course/edit.php?id=549'>Настройки</a>",
            }
        }
    )
    courses = {"549": {"external_id": "549", "title": "C++", "short_name": "", "role": "UNKNOWN"}}

    await service._classify_login_course_roles(page, courses)  # type: ignore[arg-type]

    assert courses["549"]["role"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_login_role_probe_timeout_does_not_cancel_login() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    class TimeoutPage:
        async def evaluate(self, _script: str, _argument: dict[str, Any]) -> None:
            raise PlaywrightTimeoutError("course page timed out")

    courses = {"549": {"external_id": "549", "title": "C++", "short_name": "", "role": "UNKNOWN"}}

    await service._classify_login_course_roles(TimeoutPage(), courses)  # type: ignore[arg-type]

    assert courses["549"]["role"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_course_enrichment_gets_authoritative_quiz_statement_and_transport() -> None:
    class EnrichmentService(MoodleBrowserService):
        def __init__(self) -> None:
            super().__init__(Settings(shared_secret=b"x" * 32))
            self.paths: list[str] = []

        async def _fetch_bounded_html(
            self,
            _page: object,
            _context: object,
            _url: str,
            *,
            expected_path: str,
            expected_query: dict[str, list[str]],
        ) -> str | None:
            self.paths.append(expected_path)
            fixtures = {
                "/course/modedit.php": "activity_settings_live_quiz.html",
                "/mod/quiz/overrides.php": "quiz_user_overrides_empty.html",
                "/mod/quiz/edit.php": "quiz_edit_single_essay.html",
                "/question/bank/editquestion/question.php": "question_edit_essay.html",
            }
            name = fixtures.get(expected_path)
            assert expected_query
            return (FIXTURES / name).read_text(encoding="utf-8") if name else None

    service = EnrichmentService()
    course = {
        "sections": [
            {
                "external_id": "section-1",
                "title": "Самостоятельные",
                "position": 0,
                "visible": True,
                "activities": [
                    {
                        "cmid": 30354,
                        "module": "quiz",
                        "name": "Самостоятельная работа №1",
                    }
                ],
            }
        ]
    }

    enriched = await service._enrich_course_activities(  # type: ignore[arg-type]
        object(), object(), "549", course, participant_ids=frozenset({"104684"})
    )

    activity = enriched["sections"][0]["activities"][0]
    assert service.paths == [
        "/course/modedit.php",
        "/mod/quiz/edit.php",
        "/question/bank/editquestion/question.php",
    ]
    assert activity["description"].startswith("Реализовать класс «Комплексное число»")
    assert activity["answer_transport"] == "ESSAY_ONLINE_TEXT"
    assert activity["import_supported"] is True
    assert activity["attempt_limit_unlimited"] is True


@pytest.mark.asyncio
async def test_course_enrichment_defers_statement_for_proven_all_essay_random_pool() -> None:
    random_edit = """
    <html><body class="course-549"><input name="cmid" value="30354">
      <ul class="slots"><li class="slot" data-slot="1">
        <a data-action="editrandomquestion">Random</a>
        <a class="mod_quiz_random_qbank_link"
          href="/question/edit.php?cmid=30354&amp;filter=%7B%22category%22%3A%2210197%2C123%22%7D">
          Question bank
        </a>
      </li></ul>
    </body></html>
    """
    qbank = """
    <html><body class="course-549"><a href="/login/logout.php?sesskey=x">Exit</a>
      <table id="categoryquestions" class="question-bank-table"><tbody>
        <tr><td class="qtype"><img src="/question/type/essay/pix/icon.svg" alt="Essay"></td></tr>
        <tr><td class="qtype"><img src="/question/type/essay/pix/icon.svg" alt="Essay"></td></tr>
      </tbody></table>
    </body></html>
    """

    class EnrichmentService(MoodleBrowserService):
        def __init__(self) -> None:
            super().__init__(Settings(shared_secret=b"x" * 32))
            self.paths: list[str] = []

        async def _fetch_bounded_html(
            self,
            _page: object,
            _context: object,
            _url: str,
            *,
            expected_path: str,
            expected_query: dict[str, list[str]],
        ) -> str | None:
            self.paths.append(expected_path)
            assert expected_query
            if expected_path == "/course/modedit.php":
                return (FIXTURES / "activity_settings_live_quiz.html").read_text(encoding="utf-8")
            if expected_path == "/mod/quiz/overrides.php":
                return (FIXTURES / "quiz_user_overrides_empty.html").read_text(encoding="utf-8")
            if expected_path == "/mod/quiz/edit.php":
                return random_edit
            if expected_path == "/question/edit.php":
                return qbank
            return None

    course = {
        "sections": [
            {
                "external_id": "section-1",
                "title": "Самостоятельные",
                "position": 0,
                "visible": True,
                "activities": [
                    {
                        "cmid": 30354,
                        "module": "quiz",
                        "name": "Самостоятельная работа №1",
                    }
                ],
            }
        ]
    }
    service = EnrichmentService()

    enriched = await service._enrich_course_activities(  # type: ignore[arg-type]
        object(), object(), "549", course, participant_ids=frozenset()
    )

    activity = enriched["sections"][0]["activities"][0]
    assert service.paths == [
        "/course/modedit.php",
        "/mod/quiz/edit.php",
        "/question/edit.php",
    ]
    assert activity["question_count"] == 1
    assert activity["essay_question_count"] == 0
    assert activity["random_question_count"] == 1
    assert activity["random_essay_confirmed"] is True
    assert activity["statement_deferred"] is True
    assert activity["import_supported"] is True
    assert "answer_transport" not in activity
