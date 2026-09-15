from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState, GradeRequest
from moodle_browser.service import (
    MoodleBrowserService,
    MoodleProtocolError,
    MoodleSessionExpired,
)


@dataclass
class Control:
    tag: str = "input"
    visible: bool = True
    attributes: dict[str, str] = field(default_factory=dict)
    value: str = ""
    clicked: bool = False
    on_click: Callable[[], None] | None = None


class FakeLocator:
    def __init__(self, controls: list[Control]) -> None:
        self.controls = controls

    async def count(self) -> int:
        return len(self.controls)

    def nth(self, index: int) -> FakeLocator:
        return FakeLocator([self.controls[index]])

    async def is_visible(self) -> bool:
        return self.controls[0].visible

    async def get_attribute(self, name: str) -> str | None:
        return self.controls[0].attributes.get(name)

    async def evaluate(self, script: str, value: str | None = None) -> Any:
        control = self.controls[0]
        if "tagName" in script:
            return control.tag.upper()
        if "innerText" in script and value is None:
            return control.attributes.get("context_text", "")
        if value is not None:
            control.value = value
        return None

    async def evaluate_all(self, _script: str) -> list[str]:
        return [control.attributes["href"] for control in self.controls]

    async def fill(self, value: str) -> None:
        self.controls[0].value = value

    async def select_option(self, *, value: str) -> None:
        self.controls[0].value = value

    async def click(self) -> None:
        self.controls[0].clicked = True
        if self.controls[0].on_click is not None:
            self.controls[0].on_click()


class FakePage:
    def __init__(self, controls: dict[str, list[Control]]) -> None:
        self.controls = controls
        self.url = ""
        self.closed = False

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self.controls.get(selector, []))

    async def content(self) -> str:
        return "<html><body></body></html>"

    async def close(self) -> None:
        self.closed = True


def quiz_settings_markup(
    grading_method: str, *, course_id: str = "549", cmid: str = "777", grade_max: str = "3"
) -> str:
    return f"""
    <html><body class="course-{course_id} path-mod-quiz">
      <form class="mform">
        <input name="coursemodule" value="{cmid}">
        <input name="course" value="{course_id}">
        <input name="modulename" value="quiz">
        <input name="grade" value="{grade_max}">
        <select name="attempts"><option selected value="10">10</option></select>
        <select name="grademethod"><option selected value="{grading_method}"></option></select>
      </form>
    </body></html>
    """


def live_session_state(settings: Settings) -> BrowserStorageState:
    return BrowserStorageState.model_validate(
        {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "opaque",
                    "domain": settings.base_url.removeprefix("https://"),
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


@pytest.mark.asyncio
@pytest.mark.parametrize("grading_method", ["1", "2", "3", "4"])
async def test_quiz_grade_uses_canonical_manual_grading_form(
    settings: Settings, grading_method: str
) -> None:
    mark = Control(attributes={"context_text": "Балл из 5,00"})
    textarea = Control(tag="textarea")
    editor = Control(tag="div")
    submit = Control(tag="input")
    page = FakePage(
        {
            "input[name='attempt']": [Control(attributes={"value": "134403"})],
            "input[name='slot']": [Control(attributes={"value": "1"})],
            "form#manualgradingform": [Control(tag="form")],
            "input[name$='-mark']": [mark],
            "textarea[name$='-comment']": [textarea],
            "div.editor_atto_content[contenteditable='true']": [editor],
            "form#manualgradingform #id_submitbutton": [submit],
        }
    )
    request = GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": settings.base_url,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "quiz",
                "course_id": "549",
                "cmid": 777,
                "user_id": "77",
                "attempt_id": "134403",
                "question_slot": 1,
                "grade": 1.2,
                "grade_scale_max": 3,
                "quiz_overall_grade_max": 3,
                "comment": "Проверено\nКоваленко А.",
            },
            "idempotency_key": "quiz-grade:134403:1:v1",
        }
    )
    service = MoodleBrowserService(settings)
    visited: list[str] = []
    guard_page = FakePage({})

    class Context:
        async def new_page(self) -> FakePage:
            return guard_page

    submit.on_click = lambda: setattr(page, "url", f"{settings.base_url}/mod/quiz/comment.php")

    async def goto(fake_page: FakePage, url: str) -> str:
        visited.append(url)
        fake_page.url = url
        if "/course/modedit.php" in url:
            return quiz_settings_markup(grading_method)
        if "/mod/quiz/report.php" in url:
            # A different aggregate/report grade must not replace the requested
            # question mark or make a valid manual-grade save fail.
            return f"""
            <html><body class="course-549">
              <a href="{settings.base_url}/course/view.php?id=549">Course</a>
              <table id="attempts"><tbody><tr>
                <td><a href="/user/view.php?id=77&amp;course=549">Student</a></td>
                <td>Завершено</td><td class="grade">3,00 / 3,00</td>
                <td><a class="reviewlink"
                  href="/mod/quiz/review.php?attempt=134403&amp;cmid=777">Review</a></td>
              </tr></tbody></table>
            </body></html>
            """
        return await fake_page.content()

    async def state(_context: object) -> BrowserStorageState:
        return live_session_state(settings)

    async def authenticated(_context: object, markup: str) -> None:
        assert "course-549" in markup, "standalone quiz grader has no global navigation"

    service._goto = goto  # type: ignore[method-assign]
    service._state = state  # type: ignore[method-assign]
    service._require_authenticated_page = authenticated  # type: ignore[method-assign]

    target, question_max, submitted_mark = await service._grade_quiz_essay(
        page,
        Context(),
        request,  # type: ignore[arg-type]
    )

    assert visited == [
        f"{settings.base_url}/mod/quiz/comment.php?attempt=134403&slot=1",
        f"{settings.base_url}/course/modedit.php?update=777&return=1",
        f"{settings.base_url}/mod/quiz/report.php?id=777&mode=overview"
        "&attempts=enrolled_with&onlygraded=0&onlyregraded=0&slotmarks=1&group=0&page=0",
    ]
    assert guard_page.closed is True
    assert target == "/mod/quiz/comment.php"
    assert question_max == 5
    assert submitted_mark == 2
    assert mark.value == "2"
    assert textarea.value == "<p>Проверено</p><p>Коваленко А.</p>"
    assert editor.value == "Проверено\nКоваленко А."
    assert submit.clicked is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("settings_values", "error"),
    [
        ({"grading_method": ""}, "grading method could not be confirmed"),
        ({"grading_method": "5"}, "grading method could not be confirmed"),
        ({"grade_max": "5"}, "overall grade scale changed"),
        ({"grade_max": ""}, "overall grade scale changed"),
        ({"course_id": "508"}, "identify another activity"),
        ({"cmid": "31529"}, "identify another activity"),
    ],
)
async def test_quiz_grading_configuration_still_requires_confirmed_policy_identity_and_scale(
    settings: Settings, settings_values: dict[str, str], error: str
) -> None:
    service = MoodleBrowserService(settings)
    request = GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": settings.base_url,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "quiz",
                "course_id": "549",
                "cmid": 777,
                "user_id": "77",
                "attempt_id": "134403",
                "question_slot": 1,
                "grade": 1.2,
                "grade_scale_max": 3,
                "quiz_overall_grade_max": 3,
            },
            "idempotency_key": "quiz-grade:134403:1:guard",
        }
    )
    page = FakePage({})

    async def goto(fake_page: FakePage, url: str) -> str:
        fake_page.url = url
        return quiz_settings_markup(**({"grading_method": "1"} | settings_values))

    async def authenticated(*_args: object) -> None:
        return None

    service._goto = goto  # type: ignore[method-assign]
    service._require_authenticated_page = authenticated  # type: ignore[method-assign]
    with pytest.raises(MoodleProtocolError, match=error):
        await service._require_live_quiz_grading_configuration(
            page,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            request,
        )


@pytest.mark.asyncio
async def test_standalone_quiz_grader_rejects_login_even_with_cookie(
    settings: Settings,
) -> None:
    service = MoodleBrowserService(settings)

    async def state(_context: object) -> BrowserStorageState:
        return live_session_state(settings)

    service._state = state  # type: ignore[method-assign]
    with pytest.raises(MoodleSessionExpired):
        await service._require_session_without_global_navigation(
            object(),  # type: ignore[arg-type]
            '<html><form action="/login/index.php"><input name="username"></form></html>',
            f"{settings.base_url}/mod/quiz/comment.php?attempt=134403&slot=1",
        )


@pytest.mark.asyncio
async def test_quiz_grade_guard_rejects_newer_active_attempt(settings: Settings) -> None:
    service = MoodleBrowserService(settings)
    request = GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": settings.base_url,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "quiz",
                "course_id": "549",
                "cmid": 777,
                "user_id": "77",
                "attempt_id": "134403",
                "question_slot": 1,
                "grade": 1,
                "grade_scale_max": 3,
                "quiz_overall_grade_max": 3,
            },
            "idempotency_key": "quiz-grade:134403:1:guard",
        }
    )
    markup = f"""
    <html><body class="course-549">
      <a href="{settings.base_url}/course/view.php?id=549">Course</a>
      <table id="attempts"><tbody>
        <tr><td><a href="/user/view.php?id=77&amp;course=549">Student</a></td>
          <td>Завершено</td><td><a class="reviewlink"
          href="/mod/quiz/review.php?attempt=134403&amp;cmid=777">Review</a></td></tr>
        <tr><td><a href="/user/view.php?id=77&amp;course=549">Student</a></td>
          <td>В процессе</td><td><a
          href="/mod/quiz/attempt.php?attempt=134404&amp;cmid=777">Continue</a></td></tr>
      </tbody></table>
    </body></html>
    """
    page = FakePage({})

    async def goto(fake_page: FakePage, url: str) -> str:
        fake_page.url = url
        return markup

    async def authenticated(*_args: object) -> None:
        return None

    service._goto = goto  # type: ignore[method-assign]
    service._require_authenticated_page = authenticated  # type: ignore[method-assign]
    with pytest.raises(MoodleProtocolError, match="not the latest"):
        await service._require_latest_terminal_quiz_attempt(
            page,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            request,
        )


@pytest.mark.asyncio
async def test_assignment_grade_guard_rejects_nonlatest_attempt(settings: Settings) -> None:
    service = MoodleBrowserService(settings)
    request = GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": settings.base_url,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "assign",
                "course_id": "549",
                "cmid": 888,
                "user_id": "77",
                "attempt_number": 2,
                "grade": 8.5,
            },
            "idempotency_key": "assign-grade:888:77:guard",
        }
    )
    detail_url = "/mod/assign/view.php?id=888&amp;action=grader&amp;userid=77&amp;attemptnumber=3"
    markup = f"""
    <html><body class="course-549">
      <a href="{settings.base_url}/course/view.php?id=549">Course</a>
      <table id="gradingtable"><tbody><tr>
        <td><a href="/user/view.php?id=77&amp;course=549">Student</a></td>
        <td>Отправлено</td>
        <td><a href="{detail_url}">
          Grade
        </a></td>
      </tr></tbody></table>
    </body></html>
    """
    page = FakePage({})
    closed = False

    async def close() -> None:
        nonlocal closed
        closed = True

    page.close = close  # type: ignore[attr-defined]

    class Context:
        async def new_page(self) -> FakePage:
            return page

    async def goto(fake_page: FakePage, url: str) -> str:
        fake_page.url = url
        return markup

    async def authenticated(*_args: object) -> None:
        return None

    service._goto = goto  # type: ignore[method-assign]
    service._require_authenticated_page = authenticated  # type: ignore[method-assign]
    with pytest.raises(MoodleProtocolError, match="not the latest"):
        await service._require_live_assignment_grade_preconditions(
            Context(),  # type: ignore[arg-type]
            request,
        )
    assert closed is True


@pytest.mark.asyncio
async def test_assignment_grade_uses_canonical_user_grader(settings: Settings) -> None:
    grade = Control()
    textarea = Control(tag="textarea")
    submit = Control(tag="button")
    page = FakePage(
        {
            "a[href*='/course/view.php']": [
                Control(
                    tag="a",
                    attributes={"href": f"{settings.base_url}/course/view.php?id=549"},
                )
            ],
            "input[name='userid']": [Control(attributes={"value": "77"})],
            "input[name='grade'], select[name='grade']": [grade],
            "textarea[name='assignfeedbackcomments_editor[text]']": [textarea],
            "#id_savegrade, button[name='savegrade'], input[name='savegrade'], #id_submitbutton": [
                submit
            ],
        }
    )
    request = GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": settings.base_url,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "assign",
                "course_id": "549",
                "cmid": 888,
                "user_id": "77",
                "attempt_number": 2,
                "grade": 8.5,
                "comment": "Исправлено",
            },
            "idempotency_key": "assign-grade:888:77:v1",
        }
    )
    service = MoodleBrowserService(settings)
    visited: list[str] = []

    async def goto(fake_page: FakePage, url: str) -> str:
        visited.append(url)
        fake_page.url = url
        return await fake_page.content()

    async def authenticated(_context: object, _markup: str) -> None:
        return None

    async def live_preconditions(*_args: object) -> None:
        return None

    service._goto = goto  # type: ignore[method-assign]
    service._require_authenticated_page = authenticated  # type: ignore[method-assign]
    service._require_live_assignment_grade_preconditions = live_preconditions  # type: ignore[method-assign]

    target = await service._grade_assignment_submission(  # type: ignore[arg-type]
        page,
        object(),
        request,
    )

    assert visited == [
        f"{settings.base_url}/mod/assign/view.php?id=888&action=grader&userid=77&attemptnumber=2"
    ]
    assert target == "/mod/assign/view.php"
    assert grade.value == "8.5"
    assert textarea.value == "<p>Исправлено</p>"
    assert submit.clicked is True
