from __future__ import annotations

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import BASE_URL, SHARED_SECRET, storage_state
from pydantic import ValidationError

from moodle_browser.config import Settings
from moodle_browser.models import (
    BrowserStorageState,
    QuizEssayPrepareRequest,
    QuizEssaySyncRequest,
    QuizEssaySyncResponse,
)
from moodle_browser.parsers import MoodleMarkupError, canonical_hash, parse_course_page
from moodle_browser.quiz import (
    FILE_ADD_SELECTOR,
    FILEMANAGER_SELECTOR,
    FINALIZE_ATTEMPT_SELECTOR,
    FINALIZE_CMID_SELECTOR,
    FINALIZE_FINISH_SELECTOR,
    FINALIZE_FORM_SELECTOR,
    FINALIZE_SESSKEY_SELECTOR,
    FINALIZE_TIMEUP_SELECTOR,
    FINALIZE_TRIGGER_SELECTOR,
    NEXT_NAV_SELECTOR,
    QUIZ_DIRECT_START_FORM_SELECTOR,
    QUIZ_PREFLIGHT_FORM_SELECTOR,
    QUIZ_PREFLIGHT_START_SELECTOR,
    QUIZ_START_FORM_SELECTOR,
    QuizAttempt,
    QuizAttemptNotActive,
    QuizLaunch,
    QuizSummary,
    parse_attempt_page,
    parse_quiz_view,
    parse_summary_page,
    validate_final_page,
)
from moodle_browser.service import (
    BrowserBusy,
    BrowserUnavailable,
    IdempotencyConflict,
    MoodleAttemptFinalized,
    MoodleBrowserService,
    MoodleContractError,
    MoodleProtocolError,
    MoodleSessionExpired,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def request(
    *,
    finalize: bool,
    expected_attempt_id: str | None = None,
    expected_question_slot: str | None = None,
) -> QuizEssaySyncRequest:
    artifact = b"int main() { return 0; }\n"
    return QuizEssaySyncRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "549",
            "cmid": 777,
            "artifact": {
                "filename": "solution.cpp",
                "content_base64": base64.b64encode(artifact).decode(),
                "sha256": hashlib.sha256(artifact).hexdigest(),
            },
            "expected_attempt_id": expected_attempt_id,
            "expected_question_slot": expected_question_slot,
            "finalize": finalize,
            "idempotency_key": "quiz:549:777:student:1",
            "storage_state": storage_state(),
        }
    )


@pytest.mark.asyncio
async def test_student_session_lock_serializes_one_login_without_blocking_another() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    first_state = BrowserStorageState.model_validate(storage_state())
    other_raw = storage_state()
    other_raw["cookies"][0]["value"] = "third-session"
    other_state = BrowserStorageState.model_validate(other_raw)
    same_entered = asyncio.Event()
    other_entered = asyncio.Event()

    async def enter(state: BrowserStorageState, entered: asyncio.Event) -> None:
        async with service._student_session_operation(state):
            entered.set()

    async with service._student_session_operation(first_state):
        same_task = asyncio.create_task(enter(first_state, same_entered))
        other_task = asyncio.create_task(enter(other_state, other_entered))
        await asyncio.wait_for(other_entered.wait(), timeout=0.2)
        await asyncio.sleep(0)
        assert not same_entered.is_set()

    await asyncio.wait_for(same_entered.wait(), timeout=0.2)
    await asyncio.gather(same_task, other_task)
    assert not service._student_session_locks


@pytest.mark.asyncio
async def test_timed_out_student_session_waiter_releases_its_lock_reference() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET, queue_wait_seconds=0.05))
    state = BrowserStorageState.model_validate(storage_state())

    async with service._student_session_operation(state):
        with pytest.raises(BrowserBusy, match="student browser session"):
            async with service._student_session_operation(state):
                raise AssertionError("the same Moodle session must stay serialized")
        assert len(service._student_session_locks) == 1

    assert not service._student_session_locks


def test_quiz_selectors_are_narrow_and_bound_to_one_essay_filemanager() -> None:
    assert QUIZ_START_FORM_SELECTOR == "form[action*='startattempt.php']"
    assert FILEMANAGER_SELECTOR == ".que.essay .filemanager"
    assert FILE_ADD_SELECTOR.endswith(".filemanager .fp-btn-add")
    assert NEXT_NAV_SELECTOR == "#mod_quiz-next-nav"
    assert FINALIZE_ATTEMPT_SELECTOR.endswith("input[name='attempt']")
    assert FINALIZE_FINISH_SELECTOR.endswith("input[name='finishattempt']")
    assert FINALIZE_CMID_SELECTOR.endswith("input[name='cmid']")
    assert FINALIZE_SESSKEY_SELECTOR.endswith("input[name='sesskey']")
    assert FINALIZE_TIMEUP_SELECTOR.endswith("input[name='timeup']")

    launch = parse_quiz_view(
        fixture("quiz_view.html"),
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert launch.kind == "FORM"
    assert launch.target_url == f"{BASE_URL}/mod/quiz/startattempt.php"

    attempt = parse_attempt_page(
        fixture("quiz_attempt.html"),
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert attempt == QuizAttempt("123", "1", ("solution.cpp",))

    online_attempt = parse_attempt_page(
        fixture("quiz_attempt.html").replace(
            '<div class="filemanager">\n'
            '            <button type="button" class="fp-btn-add">Добавить</button>\n'
            '            <div class="fp-file">'
            '<span class="fp-filename">solution.cpp</span></div>\n'
            "          </div>",
            '<textarea name="q123:1_answer">\tint main() {}\n</textarea>',
        ),
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert online_attempt == QuizAttempt(
        "123",
        "1",
        (),
        ("ESSAY_ONLINE_TEXT",),
        "q123:1_answer",
    )


def test_moodle_52_preflight_form_is_not_a_second_ambiguous_quiz_launch() -> None:
    html = (
        fixture("quiz_view.html")
        .replace(
            "</body>",
            """
        <form id="mod_quiz_preflight_form" action="/mod/quiz/startattempt.php" method="post">
          <input type="hidden" name="cmid" value="777">
          <input type="hidden" name="_qf__mod_quiz_form_preflight_check_form" value="1">
          <input type="submit" name="submitbutton" value="Начать попытку">
          <input type="submit" name="cancel" value="Отмена">
        </form>
        </body>
        """,
        )
        .replace("Начать тестирование", "Пройти тест")
    )

    launch = parse_quiz_view(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )

    assert launch.kind == "FORM"
    assert launch.trigger_text == "Пройти тест"
    assert launch.requires_preflight is True


def test_explicit_quiz_continuation_never_reopens_a_stale_preflight_form() -> None:
    html = (
        fixture("quiz_view.html")
        .replace(
            "</body>",
            """
        <form id="mod_quiz_preflight_form" action="/mod/quiz/startattempt.php" method="post">
          <input type="hidden" name="cmid" value="777">
          <input type="submit" name="submitbutton" value="Начать попытку">
          <input type="submit" name="cancel" value="Отмена">
        </form>
        </body>
        """,
        )
        .replace("Начать тестирование", "Продолжить текущую попытку")
    )

    launch = parse_quiz_view(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        expected_attempt_id="123",
    )

    assert launch.kind == "FORM"
    assert launch.trigger_text == "Продолжить текущую попытку"
    assert launch.requires_preflight is False


@pytest.mark.asyncio
async def test_moodle_52_preflight_launch_clicks_opener_then_exact_start_trigger() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    events: list[str] = []

    class FakeLocator:
        def __init__(self, name: str) -> None:
            self.name = name

        async def count(self) -> int:
            return 1

        async def click(self) -> None:
            events.append(f"click:{self.name}")

        async def wait_for(self, **kwargs: object) -> None:
            assert kwargs["state"] == "visible"
            events.append(f"wait:{self.name}")

        def locator(self, selector: str) -> FakeLocator:
            assert "not([name='cancel'])" in selector
            return FakeLocator("direct-trigger")

    class FakePage:
        def locator(self, selector: str) -> FakeLocator:
            if selector == QUIZ_DIRECT_START_FORM_SELECTOR:
                return FakeLocator("direct-form")
            if selector == QUIZ_PREFLIGHT_FORM_SELECTOR:
                return FakeLocator("preflight-form")
            if selector == QUIZ_PREFLIGHT_START_SELECTOR:
                return FakeLocator("preflight-trigger")
            raise AssertionError(selector)

    await service._activate_quiz_launch(
        FakePage(),  # type: ignore[arg-type]
        QuizLaunch(
            "FORM",
            f"{BASE_URL}/mod/quiz/startattempt.php",
            "Пройти тест",
            requires_preflight=True,
        ),
    )

    assert events == [
        "click:direct-trigger",
        "wait:preflight-form",
        "click:preflight-trigger",
    ]


def test_quiz_parser_retains_only_exact_same_origin_attachment_urls() -> None:
    html = fixture("quiz_attempt.html").replace(
        '<div class="fp-file"><span class="fp-filename">solution.cpp</span></div>',
        """
        <div class="fp-file" data-filename="solution.cpp">
          <a href="/draftfile.php/5/user/draft/321/solution.cpp?forcedownload=1">
            <span class="fp-filename">solution.cpp</span>
          </a>
          <a href="https://attacker.example/solution.cpp">foreign</a>
          <a href="/draftfile.php/5/user/draft/321/other.cpp">other</a>
        </div>
        """,
    )

    attempt = parse_attempt_page(
        html,
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )

    assert attempt.attachment_urls == (
        (
            "solution.cpp",
            f"{BASE_URL}/draftfile.php/5/user/draft/321/solution.cpp?forcedownload=1",
        ),
    )


def test_quiz_parser_binds_normalized_text_from_the_unique_essay_question() -> None:
    html = fixture("quiz_attempt.html").replace(
        '<div class="formulation">',
        '<div class="formulation"><div class="qtext">  Реализовать класс '
        "<strong>Vector3D</strong>.\nОперации: * и &lt;&lt;. </div>",
    )

    attempt = parse_attempt_page(
        html,
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )

    assert attempt.question_text == "Реализовать класс Vector3D . Операции: * и <<."


def test_quiz_parser_reads_live_remaining_time() -> None:
    html = fixture("quiz_attempt.html").replace(
        "</body>",
        '<div id="quiz-timer">Оставшееся время '
        '<span id="quiz-time-left">1:45:32</span></div></body>',
    )

    attempt = parse_attempt_page(
        html,
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )

    assert attempt.remaining_seconds == 6_332


def test_quiz_parser_reads_timer_bootstrap_when_label_is_localized() -> None:
    html = fixture("quiz_attempt.html").replace(
        "</body>",
        '<span id="quiz-time-left">около двух часов</span>'
        "<script>M.mod_quiz.timer.init(Y, 7017, false);</script></body>",
    )

    attempt = parse_attempt_page(
        html,
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )

    assert attempt.remaining_seconds == 7_017


def test_quiz_parser_rejects_preview_wrong_course_and_ambiguous_essay() -> None:
    with pytest.raises(MoodleMarkupError, match="preview"):
        parse_quiz_view(
            fixture("quiz_preview.html"),
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
        )
    with pytest.raises(MoodleMarkupError, match="requested course"):
        parse_quiz_view(
            fixture("quiz_view.html"),
            base_url=BASE_URL,
            course_id="550",
            cmid=777,
        )
    ambiguous = fixture("quiz_attempt.html").replace(
        "</form>",
        "<div class='que essay'><div class='filemanager'>"
        "<button class='fp-btn-add'>+</button></div></div></form>",
    )
    with pytest.raises(MoodleMarkupError, match="exactly one essay"):
        parse_attempt_page(
            ambiguous,
            f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
        )


def test_bound_quiz_attempt_selects_only_its_exact_continue_link() -> None:
    html = fixture("quiz_view.html").replace(
        "</body>",
        f"""
        <a href="{BASE_URL}/mod/quiz/attempt.php?attempt=123&amp;cmid=777">Continue 123</a>
        <a href="{BASE_URL}/mod/quiz/attempt.php?attempt=456&amp;cmid=777">Continue 456</a>
        </body>
        """,
    )

    launch = parse_quiz_view(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        expected_attempt_id="123",
    )

    assert launch.kind == "LINK"
    assert "attempt=123" in launch.target_url
    with pytest.raises(QuizAttemptNotActive, match="no longer active"):
        parse_quiz_view(
            html,
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            expected_attempt_id="999",
        )


def test_bound_quiz_attempt_accepts_only_explicit_post_continuation_form() -> None:
    continuation_html = fixture("quiz_view.html").replace(
        "Начать тестирование",
        "Продолжить текущую попытку",
    )

    launch = parse_quiz_view(
        continuation_html,
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        expected_attempt_id="123",
    )

    assert launch.kind == "FORM"
    assert launch.target_url == f"{BASE_URL}/mod/quiz/startattempt.php"
    assert launch.trigger_text == "Продолжить текущую попытку"

    with pytest.raises(QuizAttemptNotActive, match="no longer active"):
        parse_quiz_view(
            fixture("quiz_view.html"),
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            expected_attempt_id="123",
        )


def test_expected_quiz_attempt_identity_must_be_complete() -> None:
    payload = request(finalize=False).model_dump(mode="json")

    with pytest.raises(ValidationError, match="attempt identity is incomplete"):
        QuizEssaySyncRequest.model_validate({**payload, "expected_attempt_id": "123"})
    with pytest.raises(ValidationError, match="attempt identity is incomplete"):
        QuizEssaySyncRequest.model_validate({**payload, "expected_question_slot": "1"})


@pytest.mark.asyncio
async def test_bound_completed_quiz_attempt_never_starts_a_new_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=False,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    activated: list[str] = []

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/view.php?id=777"

    async def goto(page: FakePage, url: str) -> str:
        page.url = url
        # The form proves another attempt could be started.  It must not be
        # selected once the local session is bound to attempt 123.
        return fixture("quiz_view.html")

    async def authenticated(*_args: object) -> None:
        return None

    async def activate(*_args: object) -> None:
        activated.append("activated")

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    monkeypatch.setattr(service, "_activate_quiz_launch", activate)

    with pytest.raises(MoodleAttemptFinalized, match="already finalized"):
        await service._open_real_quiz_attempt(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
        )
    assert activated == []


@pytest.mark.asyncio
async def test_bound_quiz_attempt_disappearing_before_click_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=False,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    view_html = fixture("quiz_view.html").replace(
        "</body>",
        '<a href="/mod/quiz/attempt.php?attempt=123&amp;cmid=777">Continue</a></body>',
    )

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/view.php?id=777"

    async def goto(page: FakePage, url: str) -> str:
        page.url = url
        return view_html

    async def authenticated(*_args: object) -> None:
        return None

    async def vanished(*_args: object) -> None:
        raise MoodleProtocolError("Moodle quiz continue trigger changed")

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    monkeypatch.setattr(service, "_activate_quiz_launch", vanished)

    with pytest.raises(MoodleAttemptFinalized, match="stopped being available"):
        await service._open_real_quiz_attempt(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
        )


@pytest.mark.asyncio
async def test_bound_post_continuation_form_keeps_exact_attempt_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=False,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    view_html = fixture("quiz_view.html").replace(
        "Начать тестирование",
        "Продолжить текущую попытку",
    )
    activated: list[str] = []

    class FakeLocator:
        @property
        def first(self) -> FakeLocator:
            return self

        async def wait_for(self, **_kwargs: object) -> None:
            return None

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/view.php?id=777"

        async def wait_for_load_state(self, _state: str) -> None:
            return None

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator()

        async def content(self) -> str:
            return fixture("quiz_attempt.html")

    async def goto(page: FakePage, url: str) -> str:
        page.url = url
        return view_html

    async def authenticated(*_args: object) -> None:
        return None

    async def activate(page: FakePage, launch: object) -> None:
        activated.append(getattr(launch, "kind", ""))
        page.url = f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777"

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    monkeypatch.setattr(service, "_activate_quiz_launch", activate)

    attempt = await service._open_real_quiz_attempt(
        FakePage(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        payload,
    )

    assert activated == ["FORM"]
    assert attempt.attempt_id == "123"
    assert attempt.question_slot == "1"


@pytest.mark.asyncio
async def test_bound_quiz_attempt_identity_change_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=False,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    view_html = fixture("quiz_view.html").replace(
        "Начать тестирование",
        "Продолжить текущую попытку",
    )

    class FakeLocator:
        @property
        def first(self) -> FakeLocator:
            return self

        async def wait_for(self, **_kwargs: object) -> None:
            return None

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/view.php?id=777"

        async def wait_for_load_state(self, _state: str) -> None:
            return None

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator()

        async def content(self) -> str:
            return fixture("quiz_attempt.html")

    async def goto(page: FakePage, url: str) -> str:
        page.url = url
        return view_html

    async def authenticated(*_args: object) -> None:
        return None

    async def activate(page: FakePage, _launch: object) -> None:
        page.url = f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=777"

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    monkeypatch.setattr(service, "_activate_quiz_launch", activate)
    monkeypatch.setattr(
        "moodle_browser.service.parse_attempt_page",
        lambda *_args, **_kwargs: QuizAttempt("123", "2", ()),
    )

    with pytest.raises(MoodleAttemptFinalized, match="no longer be edited safely"):
        await service._open_real_quiz_attempt(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
        )


def test_summary_and_final_page_require_real_attempt_identifiers() -> None:
    summary = parse_summary_page(
        fixture("quiz_summary.html"),
        f"{BASE_URL}/mod/quiz/summary.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        attempt_id="123",
    )
    assert summary.trigger_text == "Отправить всё и завершить тест"
    with pytest.raises(MoodleMarkupError, match="identifiers changed"):
        parse_summary_page(
            fixture("quiz_summary.html"),
            f"{BASE_URL}/mod/quiz/summary.php?attempt=123&cmid=778",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            attempt_id="123",
        )
    review_html = fixture("quiz_summary.html").replace(
        "page-mod-quiz-summary", "page-mod-quiz-review"
    )
    validate_final_page(
        review_html,
        f"{BASE_URL}/mod/quiz/review.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        attempt_id="123",
    )
    with pytest.raises(MoodleMarkupError, match="attempt id changed"):
        validate_final_page(
            review_html,
            f"{BASE_URL}/mod/quiz/review.php?attempt=124&cmid=777",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            attempt_id="123",
        )

    active_view = fixture("quiz_view.html").replace(
        "Начать тестирование", "Продолжить текущую попытку"
    )
    with pytest.raises(MoodleMarkupError, match="still active"):
        validate_final_page(
            active_view,
            f"{BASE_URL}/mod/quiz/view.php?id=777",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            attempt_id="123",
        )

    bare_view = fixture("quiz_view.html")
    with pytest.raises(MoodleMarkupError, match="did not confirm"):
        validate_final_page(
            bare_view,
            f"{BASE_URL}/mod/quiz/view.php?id=777",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            attempt_id="123",
        )

    completed_view = bare_view.replace(
        "</body>",
        '<table class="generaltable"><tr><td><a '
        'href="/mod/quiz/review.php?attempt=123&amp;cmid=777">Review</a>'
        "</td></tr></table></body>",
    )
    validate_final_page(
        completed_view,
        f"{BASE_URL}/mod/quiz/view.php?id=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        attempt_id="123",
    )

    embargoed_completed_view = bare_view.replace(
        "Начать тестирование",
        "Пройти тест заново",
    ).replace(
        "</body>",
        """
        <table class="table generaltable generalbox quizreviewsummary mb-0">
          <caption>Резюме попытки 1</caption>
          <tr><th>Состояние</th><td>Завершены</td></tr>
          <tr><th>Завершен</th><td>2 сент. 2026, 17:17</td></tr>
        </table></body>
        """,
    )
    with pytest.raises(MoodleMarkupError, match="did not confirm"):
        validate_final_page(
            embargoed_completed_view,
            f"{BASE_URL}/mod/quiz/view.php?id=777",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            attempt_id="123",
        )
    validate_final_page(
        embargoed_completed_view,
        f"{BASE_URL}/mod/quiz/view.php?id=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        attempt_id="123",
        allow_causal_completed_view=True,
    )
    with pytest.raises(MoodleMarkupError, match="did not confirm"):
        validate_final_page(
            embargoed_completed_view.replace("Завершены", "В процессе"),
            f"{BASE_URL}/mod/quiz/view.php?id=777",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
            attempt_id="123",
            allow_causal_completed_view=True,
        )


@pytest.mark.asyncio
async def test_moodle_52_finalize_submits_exact_validated_form_without_modal_race(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    events: list[str] = []

    class Element:
        def __init__(
            self,
            name: str,
            text: str = "",
            *,
            value: str = "",
            method: str = "",
        ) -> None:
            self.name = name
            self.text = text
            self.value = value
            self.method = method

        async def text_content(self) -> str:
            return self.text

        async def get_attribute(self, name: str) -> str | None:
            if name == "value":
                return self.value or None
            if name == "method":
                return self.method or None
            return None

        async def evaluate(self, expression: str) -> None:
            assert expression == "form => HTMLFormElement.prototype.submit.call(form)"
            events.append(f"submit:{self.name}")
            page.url = f"{BASE_URL}/mod/quiz/review.php?attempt=123&cmid=777"

    class Collection:
        def __init__(self, items: list[Element]) -> None:
            self.items = items

        @property
        def first(self) -> Element:
            return self.items[0]

        async def count(self) -> int:
            return len(self.items)

        async def text_content(self) -> str:
            return await self.first.text_content()

        async def get_attribute(self, name: str) -> str | None:
            return await self.first.get_attribute(name)

        async def evaluate(self, expression: str) -> None:
            await self.first.evaluate(expression)

    class FakePage:
        def __init__(self) -> None:
            self.url = f"{BASE_URL}/mod/quiz/summary.php?attempt=123&cmid=777"
            self.trigger = Element("summary-trigger", "Отправить всё и завершить тест")

        def locator(self, selector: str) -> Collection:
            if selector == FINALIZE_FORM_SELECTOR:
                return Collection([Element("form", method="post")])
            if selector == FINALIZE_ATTEMPT_SELECTOR:
                return Collection([Element("attempt", value="123")])
            if selector == FINALIZE_CMID_SELECTOR:
                return Collection([Element("cmid", value="777")])
            if selector == FINALIZE_FINISH_SELECTOR:
                return Collection([Element("finishattempt", value="1")])
            if selector == FINALIZE_SESSKEY_SELECTOR:
                return Collection([Element("sesskey", value="opaque")])
            if selector == FINALIZE_TIMEUP_SELECTOR:
                return Collection([Element("timeup", value="0")])
            if selector == FINALIZE_TRIGGER_SELECTOR:
                return Collection([self.trigger])
            raise AssertionError(selector)

        async def wait_for_load_state(self, _state: str) -> None:
            return None

        async def content(self) -> str:
            return fixture("quiz_summary.html").replace(
                "page-mod-quiz-summary", "page-mod-quiz-review"
            )

    async def authenticated(*_args: object) -> tuple[bool, BrowserStorageState]:
        return True, BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_authenticated", authenticated)
    page = FakePage()
    await service._finalize_quiz_attempt(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        request(finalize=True),
        QuizSummary("123", "Отправить всё и завершить тест"),
    )

    assert events == ["submit:form"]


@pytest.mark.asyncio
async def test_moodle_52_finalize_accepts_direct_embargoed_completed_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    events: list[str] = []

    class Element:
        def __init__(self, *, value: str | None = None, method: str | None = None) -> None:
            self.value = value
            self.method = method

        async def text_content(self) -> str:
            return "Отправить всё и завершить тест"

        async def get_attribute(self, name: str) -> str | None:
            if name == "value":
                return self.value
            if name == "method":
                return self.method
            return None

        async def evaluate(self, expression: str) -> None:
            assert expression == "form => HTMLFormElement.prototype.submit.call(form)"
            events.append("submit:finish-form")
            page.url = f"{BASE_URL}/mod/quiz/view.php?id=777"

    class Collection:
        def __init__(
            self,
            count: int = 1,
            *,
            value: str | None = None,
            method: str | None = None,
        ) -> None:
            self._count = count
            self.element = Element(value=value, method=method)

        async def count(self) -> int:
            return self._count

        async def text_content(self) -> str:
            return await self.element.text_content()

        async def get_attribute(self, name: str) -> None:
            return await self.element.get_attribute(name)

        async def evaluate(self, expression: str) -> None:
            await self.element.evaluate(expression)

    class FakePage:
        def __init__(self) -> None:
            self.url = f"{BASE_URL}/mod/quiz/summary.php?attempt=123&cmid=777"
            self.content_calls = 0

        def locator(self, selector: str) -> Collection:
            assert selector in {
                FINALIZE_FORM_SELECTOR,
                FINALIZE_ATTEMPT_SELECTOR,
                FINALIZE_CMID_SELECTOR,
                FINALIZE_FINISH_SELECTOR,
                FINALIZE_SESSKEY_SELECTOR,
                FINALIZE_TIMEUP_SELECTOR,
                FINALIZE_TRIGGER_SELECTOR,
            }
            if selector == FINALIZE_ATTEMPT_SELECTOR:
                return Collection(value="123")
            if selector == FINALIZE_CMID_SELECTOR:
                return Collection(value="777")
            if selector == FINALIZE_FINISH_SELECTOR:
                return Collection(value="1")
            if selector == FINALIZE_SESSKEY_SELECTOR:
                return Collection(value="opaque")
            if selector == FINALIZE_TIMEUP_SELECTOR:
                return Collection(value="0")
            if selector == FINALIZE_FORM_SELECTOR:
                return Collection(method="post")
            return Collection()

        async def content(self) -> str:
            self.content_calls += 1
            if self.content_calls == 1:
                return "<html><body></body></html>"
            return (
                fixture("quiz_view.html")
                .replace("Начать тестирование", "Пройти тест заново")
                .replace(
                    "</body>",
                    """
                <table class="table quizreviewsummary">
                  <caption>Резюме попытки 4</caption>
                  <tr><th>Состояние</th><td>Завершены</td></tr>
                  <tr><th>Завершен</th><td>2 сент. 2026, 17:17</td></tr>
                </table>
                <table class="table quizreviewsummary">
                  <caption>Резюме попытки 3</caption>
                  <tr><th>Состояние</th><td>Завершены</td></tr>
                  <tr><th>Завершен</th><td>2 сент. 2026, 17:10</td></tr>
                </table>
                <table class="table quizreviewsummary">
                  <caption>Резюме попытки 2</caption>
                  <tr><th>Состояние</th><td>Завершены</td></tr>
                  <tr><th>Завершен</th><td>2 сент. 2026, 17:05</td></tr>
                </table>
                <table class="table quizreviewsummary">
                  <caption>Резюме попытки 1</caption>
                  <tr><th>Состояние</th><td>Завершены</td></tr>
                  <tr><th>Завершен</th><td>2 сент. 2026, 16:55</td></tr>
                </table></body>
                """,
                )
            )

    async def authenticated(_context: object, html: str) -> tuple[bool, BrowserStorageState]:
        return (
            'href="/login/logout.php' in html,
            BrowserStorageState.model_validate(storage_state()),
        )

    monkeypatch.setattr(service, "_authenticated", authenticated)
    page = FakePage()
    await service._finalize_quiz_attempt(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        request(finalize=True),
        QuizSummary("123", "Отправить всё и завершить тест"),
    )

    assert events == ["submit:finish-form"]
    assert page.content_calls == 2


@pytest.mark.asyncio
async def test_finalize_transition_is_not_treated_as_expired_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/processattempt.php"

        async def content(self) -> str:
            raise AssertionError("transitional content must not be inspected")

    async def authenticated(*_args: object) -> None:
        raise AssertionError("transitional content must not be authenticated")

    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    assert not await service._quiz_final_page_is_confirmed(
        FakePage(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        request(finalize=True),
        "123",
        allow_causal_completed_view=True,
    )


@pytest.mark.asyncio
async def test_finalize_stable_url_with_transitional_dom_keeps_valid_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    state = BrowserStorageState.model_validate(storage_state())

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/view.php?id=777"

        async def content(self) -> str:
            return "<html><body></body></html>"

    async def transitional(*_args: object) -> tuple[bool, BrowserStorageState]:
        return False, state

    monkeypatch.setattr(service, "_authenticated", transitional)
    assert not await service._quiz_final_page_is_confirmed(
        FakePage(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        request(finalize=True),
        "123",
        allow_causal_completed_view=True,
        transient_document=True,
    )
    with pytest.raises(MoodleProtocolError, match="not authenticated"):
        await service._quiz_final_page_is_confirmed(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            request(finalize=True),
            "123",
            allow_causal_completed_view=True,
        )


@pytest.mark.asyncio
async def test_finalize_transitional_dom_without_session_is_expired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    empty_state = BrowserStorageState.model_validate({"cookies": [], "origins": []})

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/view.php?id=777"

        async def content(self) -> str:
            return "<html><body></body></html>"

    async def expired(*_args: object) -> tuple[bool, BrowserStorageState]:
        return False, empty_state

    monkeypatch.setattr(service, "_authenticated", expired)
    with pytest.raises(MoodleSessionExpired):
        await service._quiz_final_page_is_confirmed(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            request(finalize=True),
            "123",
            allow_causal_completed_view=True,
            transient_document=True,
        )


def test_artifact_contract_rejects_paths_bad_base64_and_digest_mismatch() -> None:
    payload = request(finalize=False).model_dump(mode="json")
    for filename in ("../solution.cpp", "dir/solution.cpp", "solution.cpp."):
        changed = {**payload, "artifact": {**payload["artifact"], "filename": filename}}
        with pytest.raises(ValidationError):
            QuizEssaySyncRequest.model_validate(changed)
    changed = {**payload, "artifact": {**payload["artifact"], "content_base64": "%%%="}}
    with pytest.raises(ValidationError):
        QuizEssaySyncRequest.model_validate(changed)

    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    mismatched = QuizEssaySyncRequest.model_validate(
        {**payload, "artifact": {**payload["artifact"], "sha256": "0" * 64}}
    )
    with pytest.raises(MoodleContractError, match="digest"):
        service._decode_artifact(mismatched)


def test_artifact_contract_accepts_empty_source() -> None:
    payload = request(finalize=False).model_dump(mode="json")
    empty = QuizEssaySyncRequest.model_validate(
        {
            **payload,
            "artifact": {
                **payload["artifact"],
                "content_base64": "",
                "sha256": hashlib.sha256(b"").hexdigest(),
            },
        }
    )

    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    assert service._decode_artifact(empty) == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finalize", "expected_status", "expected_tail"),
    [
        (False, "DRAFT_SAVED", "return"),
        (True, "FINALIZED", "finalize"),
    ],
)
async def test_quiz_workflow_branches_only_after_draft_save_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    finalize: bool,
    expected_status: str,
    expected_tail: str,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(finalize=finalize)
    artifact = service._decode_artifact(payload)
    events: list[str] = []
    attempt = QuizAttempt("123", "1", ())
    summary = QuizSummary("123", "finish")

    async def open_attempt(*_args: object) -> QuizAttempt:
        events.append("open")
        return attempt

    async def upload(*_args: object, **_kwargs: object) -> None:
        events.append("upload")

    async def save(*_args: object) -> QuizSummary:
        events.append("save")
        return summary

    async def return_attempt(*_args: object) -> None:
        events.append("return")

    async def finalize_attempt(*_args: object) -> None:
        events.append("finalize")

    async def state(*_args: object) -> BrowserStorageState:
        events.append("state")
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_replace_quiz_attachment", upload)
    monkeypatch.setattr(service, "_save_quiz_answer", save)
    monkeypatch.setattr(service, "_return_to_quiz_attempt", return_attempt)
    monkeypatch.setattr(service, "_finalize_quiz_attempt", finalize_attempt)
    monkeypatch.setattr(service, "_state", state)

    response = await service._execute_quiz_essay_sync(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        payload,
        artifact,
    )
    assert response.status == expected_status
    assert events == ["open", "upload", "save", expected_tail, "state"]
    assert response.receipt.sha256 == payload.artifact.sha256


@pytest.mark.asyncio
async def test_terminal_quiz_retry_resumes_finalization_without_uploading_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=True,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    attempt = QuizAttempt("123", "1", ())
    events: list[str] = []

    class FakeContext:
        async def new_page(self) -> object:
            return object()

        async def close(self) -> None:
            events.append("close")

    @asynccontextmanager
    async def operation(**_kwargs: object):  # type: ignore[no-untyped-def]
        yield object()

    async def new_context(*_args: object, **_kwargs: object) -> FakeContext:
        return FakeContext()

    async def open_attempt(*_args: object) -> QuizAttempt:
        events.append("open")
        return attempt

    async def upload(*_args: object, **_kwargs: object) -> None:
        events.append("upload")

    async def save(*_args: object) -> QuizSummary:
        events.append("save")
        return QuizSummary("123", "finish")

    async def fail_finalize(*_args: object) -> None:
        events.append("finalize-timeout")
        raise BrowserUnavailable("Moodle final submission timed out")

    async def resume(
        _context: object,
        _page: object,
        request_payload: QuizEssaySyncRequest,
        artifact: bytes,
        *,
        attempt_id: str,
        question_slot: str,
    ) -> QuizEssaySyncResponse:
        events.append("resume-finalize")
        return QuizEssaySyncResponse.model_validate(
            {
                "status": "FINALIZED",
                "receipt": {
                    "course_id": request_payload.course_id,
                    "cmid": request_payload.cmid,
                    "attempt_id": attempt_id,
                    "question_slot": question_slot,
                    "filename": request_payload.artifact.filename,
                    "sha256": request_payload.artifact.sha256,
                    "size_bytes": len(artifact),
                    "idempotency_key": request_payload.idempotency_key,
                },
                "storage_state": storage_state(),
            }
        )

    monkeypatch.setattr(service, "_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_replace_quiz_attachment", upload)
    monkeypatch.setattr(service, "_save_quiz_answer", save)
    monkeypatch.setattr(service, "_finalize_quiz_attempt", fail_finalize)
    monkeypatch.setattr(service, "_resume_quiz_essay_finalization", resume)

    with pytest.raises(BrowserUnavailable, match="timed out"):
        await service.sync_quiz_essay(payload)

    response = await service.sync_quiz_essay(payload)

    assert response.status == "FINALIZED"
    assert events == [
        "open",
        "upload",
        "save",
        "finalize-timeout",
        "close",
        "resume-finalize",
        "close",
    ]
    assert payload.idempotency_key not in service._quiz_sync_progress

    replay = await service.sync_quiz_essay(payload)
    assert replay.status == "FINALIZED"
    assert events.count("upload") == 1


@pytest.mark.asyncio
async def test_resume_quiz_finalization_uses_the_saved_attempt_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=True,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    artifact = service._decode_artifact(payload)
    events: list[str] = []

    class FakePage:
        url = ""

    page = FakePage()

    async def goto(_page: object, url: str) -> str:
        page.url = url
        events.append("summary")
        return fixture("quiz_summary.html")

    async def final_page(*_args: object, **_kwargs: object) -> bool:
        return False

    async def authenticated(*_args: object) -> None:
        events.append("authenticated")

    async def finalize(
        _page: object,
        _context: object,
        _request: QuizEssaySyncRequest,
        summary: QuizSummary,
    ) -> None:
        assert summary.attempt_id == "123"
        events.append("finalize")

    async def state(*_args: object) -> BrowserStorageState:
        events.append("state")
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_quiz_final_page_is_confirmed", final_page)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    monkeypatch.setattr(service, "_finalize_quiz_attempt", finalize)
    monkeypatch.setattr(service, "_state", state)

    response = await service._resume_quiz_essay_finalization(
        object(),  # type: ignore[arg-type]
        page,  # type: ignore[arg-type]
        payload,
        artifact,
        attempt_id="123",
        question_slot="1",
    )

    assert response.status == "FINALIZED"
    assert response.receipt.attempt_id == "123"
    assert events == ["summary", "authenticated", "finalize", "state"]


@pytest.mark.asyncio
async def test_quiz_workflow_writes_online_text_when_that_transport_was_proven(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(finalize=False).model_copy(update={"answer_transport": "ESSAY_ONLINE_TEXT"})
    artifact = service._decode_artifact(payload)
    events: list[str] = []
    attempt = QuizAttempt(
        "123",
        "1",
        (),
        ("ESSAY_ONLINE_TEXT",),
        "q123:1_answer",
    )

    async def open_attempt(*_args: object) -> QuizAttempt:
        events.append("open")
        return attempt

    async def online_text(*_args: object) -> None:
        events.append("online-text")

    async def upload(*_args: object, **_kwargs: object) -> None:
        events.append("unexpected-upload")

    async def save(*_args: object) -> QuizSummary:
        events.append("save")
        return QuizSummary("123", "")

    async def return_attempt(*_args: object) -> None:
        events.append("return")

    async def state(*_args: object) -> BrowserStorageState:
        events.append("state")
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_replace_quiz_online_text", online_text)
    monkeypatch.setattr(service, "_replace_quiz_attachment", upload)
    monkeypatch.setattr(service, "_save_quiz_answer", save)
    monkeypatch.setattr(service, "_return_to_quiz_attempt", return_attempt)
    monkeypatch.setattr(service, "_state", state)

    response = await service._execute_quiz_essay_sync(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        payload,
        artifact,
    )

    assert response.status == "DRAFT_SAVED"
    assert events == ["open", "online-text", "save", "return", "state"]


@pytest.mark.asyncio
async def test_quiz_prepare_returns_concrete_random_question_and_prefers_attachment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = QuizEssayPrepareRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "549",
            "cmid": 777,
            "storage_state": storage_state(),
        }
    )
    attempt = QuizAttempt(
        "141716",
        "1",
        (),
        ("ESSAY_ONLINE_TEXT", "ESSAY_ATTACHMENT"),
        "q141716:1_answer",
        (),
        "Реализовать класс Vector3D.",
        6_332,
    )

    class FakeContext:
        async def new_page(self) -> object:
            return object()

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def operation(**_kwargs: object):  # type: ignore[no-untyped-def]
        yield object()

    async def new_context(*_args: object, **_kwargs: object) -> FakeContext:
        return FakeContext()

    async def open_attempt(*_args: object) -> QuizAttempt:
        return attempt

    async def state(*_args: object) -> BrowserStorageState:
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_state", state)

    response = await service.prepare_quiz_essay(payload)

    assert response.status == "READY"
    assert response.preparation.attempt_id == "141716"
    assert response.preparation.question_slot == "1"
    assert response.preparation.question_text == "Реализовать класс Vector3D."
    assert response.preparation.answer_transport == "ESSAY_ATTACHMENT"
    assert response.preparation.available_answer_transports == [
        "ESSAY_ONLINE_TEXT",
        "ESSAY_ATTACHMENT",
    ]
    assert response.preparation.remaining_seconds == 6_332


@pytest.mark.asyncio
async def test_quiz_prepare_rejects_attempt_without_rendered_question_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = QuizEssayPrepareRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "549",
            "cmid": 777,
            "storage_state": storage_state(),
        }
    )

    class FakeContext:
        async def new_page(self) -> object:
            return object()

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def operation(**_kwargs: object):  # type: ignore[no-untyped-def]
        yield object()

    async def new_context(*_args: object, **_kwargs: object) -> FakeContext:
        return FakeContext()

    async def open_attempt(*_args: object) -> QuizAttempt:
        return QuizAttempt("141716", "1", ())

    monkeypatch.setattr(service, "_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)

    with pytest.raises(MoodleProtocolError, match="question text"):
        await service.prepare_quiz_essay(payload)


class _FakeArtifactResponse:
    def __init__(self, url: str, content: bytes, *, status: int = 200) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-length": str(len(content))}
        self._content = content
        self.disposed = False

    async def body(self) -> bytes:
        return self._content

    async def dispose(self) -> None:
        self.disposed = True


class _FakeArtifactRequest:
    def __init__(self, response: _FakeArtifactResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> _FakeArtifactResponse:
        self.calls.append((url, kwargs))
        return self.response


class _FakeArtifactPage:
    def __init__(self, request_context: _FakeArtifactRequest) -> None:
        self.context = type("FakeContext", (), {"request": request_context})()


@pytest.mark.asyncio
async def test_quiz_managed_overwrite_verifies_remote_bytes_when_url_is_exposed() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    content = b"int main() { return 0; }\n"
    digest = hashlib.sha256(content).hexdigest()
    url = f"{BASE_URL}/draftfile.php/5/user/draft/321/main.cpp?forcedownload=1"
    response = _FakeArtifactResponse(url, content)
    request_context = _FakeArtifactRequest(response)

    verified = await service._verify_quiz_managed_file_if_exposed(
        _FakeArtifactPage(request_context),  # type: ignore[arg-type]
        filename="main.cpp",
        expected_sha256=digest,
        attachment_urls=(("main.cpp", url),),
    )

    assert verified is True
    assert len(request_context.calls) == 1
    assert response.disposed is True

    changed = _FakeArtifactResponse(url, b"changed outside the connector\n")
    with pytest.raises(MoodleProtocolError, match="changed after"):
        await service._verify_quiz_managed_file_if_exposed(
            _FakeArtifactPage(_FakeArtifactRequest(changed)),  # type: ignore[arg-type]
            filename="main.cpp",
            expected_sha256=digest,
            attachment_urls=(("main.cpp", url),),
        )
    assert changed.disposed is True


@pytest.mark.asyncio
async def test_quiz_managed_overwrite_uses_receipt_when_lazy_manager_has_no_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    clicks: list[str] = []

    class FakeLocator:
        def __init__(self, name: str, *, count: int = 1) -> None:
            self.name = name
            self._count = count

        @property
        def first(self) -> FakeLocator:
            return self

        async def count(self) -> int:
            return self._count

        async def click(self) -> None:
            clicks.append(self.name)

        async def set_input_files(self, _payload: object) -> None:
            return None

        async def wait_for(self, **_kwargs: object) -> None:
            return None

        async def text_content(self) -> str:
            return "main.cpp"

        def get_by_text(self, _text: object, **_kwargs: object) -> FakeLocator:
            # Reproduce the newer Moodle lazy manager: no existing filename is
            # visible before upload, although the server asks to overwrite it.
            return FakeLocator("lazy-file-label", count=0)

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator("lazy-file-metadata", count=0)

    class FakePage:
        def __init__(self) -> None:
            self.manager = FakeLocator("manager")

        def locator(self, selector: str) -> FakeLocator:
            if selector == FILEMANAGER_SELECTOR:
                return self.manager
            if selector == ".fp-dlg-butoverwrite:visible":
                return FakeLocator("overwrite")
            return FakeLocator(selector)

        def get_by_text(self, _text: object, **_kwargs: object) -> FakeLocator:
            return FakeLocator("repository")

    async def unique(locator: FakeLocator, **_kwargs: object) -> FakeLocator:
        return locator

    monkeypatch.setattr(service, "_wait_for_unique_locator", unique)
    content = b"int main() { return 1; }\n"
    await service._replace_quiz_attachment(
        FakePage(),  # type: ignore[arg-type]
        "main.cpp",
        content,
        replace_existing=False,
        previous_managed_filename="main.cpp",
        previous_managed_sha256=hashlib.sha256(b"previous\n").hexdigest(),
    )

    assert "overwrite" in clicks


@pytest.mark.asyncio
async def test_quiz_lazy_overwrite_without_exact_receipt_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    overwrite_clicked = False

    class FakeLocator:
        def __init__(self, *, count: int = 1, overwrite: bool = False) -> None:
            self._count = count
            self.overwrite = overwrite

        @property
        def first(self) -> FakeLocator:
            return self

        async def count(self) -> int:
            return self._count

        async def click(self) -> None:
            nonlocal overwrite_clicked
            if self.overwrite:
                overwrite_clicked = True

        async def set_input_files(self, _payload: object) -> None:
            return None

        async def wait_for(self, **_kwargs: object) -> None:
            return None

        async def text_content(self) -> str:
            return "main.cpp"

        def get_by_text(self, _text: object, **_kwargs: object) -> FakeLocator:
            return FakeLocator(count=0)

        def locator(self, _selector: str) -> FakeLocator:
            return FakeLocator(count=0)

    class FakePage:
        def __init__(self) -> None:
            self.manager = FakeLocator()

        def locator(self, selector: str) -> FakeLocator:
            if selector == FILEMANAGER_SELECTOR:
                return self.manager
            return FakeLocator(overwrite=selector == ".fp-dlg-butoverwrite:visible")

        def get_by_text(self, _text: object, **_kwargs: object) -> FakeLocator:
            return FakeLocator()

    async def unique(locator: FakeLocator, **_kwargs: object) -> FakeLocator:
        return locator

    monkeypatch.setattr(service, "_wait_for_unique_locator", unique)
    with pytest.raises(MoodleProtocolError, match="without connector ownership"):
        await service._replace_quiz_attachment(
            FakePage(),  # type: ignore[arg-type]
            "main.cpp",
            b"int main() {}\n",
            replace_existing=False,
        )
    assert overwrite_clicked is False


@pytest.mark.asyncio
async def test_quiz_idempotency_replay_keeps_the_callers_newer_storage_state() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        finalize=False,
        expected_attempt_id="123",
        expected_question_slot="1",
    )
    artifact = service._decode_artifact(payload)
    fingerprint = canonical_hash(
        {
            "course_id": payload.course_id,
            "cmid": payload.cmid,
            "answer_transport": payload.answer_transport,
            "filename": payload.artifact.filename,
            "sha256": payload.artifact.sha256,
            "size_bytes": len(artifact),
            "finalize": payload.finalize,
            "expected_attempt_id": "123",
            "expected_question_slot": "1",
        }
    )
    old_state = storage_state()
    old_state["cookies"][0]["value"] = "older-session"
    cached = QuizEssaySyncResponse.model_validate(
        {
            "status": "DRAFT_SAVED",
            "receipt": {
                "course_id": payload.course_id,
                "cmid": payload.cmid,
                "attempt_id": "123",
                "question_slot": "1",
                "filename": payload.artifact.filename,
                "sha256": payload.artifact.sha256,
                "size_bytes": len(artifact),
                "idempotency_key": payload.idempotency_key,
            },
            "storage_state": old_state,
        }
    )
    service._quiz_sync_cache[payload.idempotency_key] = (fingerprint, cached)

    new_state = storage_state()
    new_state["cookies"][0]["value"] = "newer-session"
    replay = payload.model_copy(
        update={"storage_state": BrowserStorageState.model_validate(new_state)}
    )
    response = await service.sync_quiz_essay(replay)

    assert response.receipt == cached.receipt
    assert response.storage_state.cookies[0].value == "newer-session"

    rebound = QuizEssaySyncRequest.model_validate(
        {
            **replay.model_dump(mode="json"),
            "expected_attempt_id": "124",
            "expected_question_slot": "1",
        }
    )
    with pytest.raises(IdempotencyConflict, match="reused"):
        await service.sync_quiz_essay(rebound)


@pytest.mark.asyncio
async def test_top_format_crawl_visits_only_rebuilt_bounded_section_urls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(
        Settings(shared_secret=SHARED_SECRET, max_course_section_pages=12)
    )
    main_html = fixture("course_top_main.html")
    pages = {
        f"{BASE_URL}/course/view.php?id=549&section=1": fixture("course_top_section_1.html"),
        f"{BASE_URL}/course/view.php?id=549&section=2": fixture("course_top_section_2.html"),
    }
    visited: list[str] = []

    class FakePage:
        url = ""

    page = FakePage()

    async def goto(fake_page: FakePage, url: str) -> str:
        visited.append(url)
        fake_page.url = url
        return pages[url]

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    result = await service._crawl_course_section_pages(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        "549",
        main_html,
        parse_course_page(main_html, BASE_URL, "549"),
    )
    assert visited == list(pages)
    assert sum(len(section["activities"]) for section in result["sections"]) == 2


@pytest.mark.asyncio
async def test_moodle_52_crawl_visits_section_record_pages_and_discovers_quiz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(
        Settings(shared_secret=SHARED_SECRET, max_course_section_pages=12)
    )
    main_html = fixture("course_moodle52_main.html")
    pages = {
        f"{BASE_URL}/course/section.php?id=6952": main_html,
        f"{BASE_URL}/course/section.php?id=6962": fixture("course_moodle52_section_6962.html"),
    }
    visited: list[str] = []

    class FakePage:
        url = ""

    page = FakePage()

    async def goto(fake_page: FakePage, url: str) -> str:
        visited.append(url)
        fake_page.url = url
        return pages[url]

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    result = await service._crawl_course_section_pages(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        "549",
        main_html,
        parse_course_page(main_html, BASE_URL, "549"),
    )

    assert visited == list(pages)
    discovered = {
        activity["cmid"]: activity["name"]
        for section in result["sections"]
        for activity in section["activities"]
    }
    assert discovered[30354] == "Самостоятельная работа №1"
