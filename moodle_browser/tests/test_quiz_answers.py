from __future__ import annotations

import base64
import hashlib
import logging
from contextlib import asynccontextmanager
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from moodle_browser.config import Settings
from moodle_browser.models import (
    BrowserStorageState,
    QuizAnswersSyncRequest,
    QuizEssayPrepareRequest,
)
from moodle_browser.parsers import MoodleMarkupError, parse_quiz_question_summary
from moodle_browser.quiz import (
    QuizAttempt,
    QuizSummary,
    parse_attempt_page,
    parse_attempt_questions,
    quiz_attempt_navigation,
)
from moodle_browser.service import (
    BrowserUnavailable,
    IdempotencyConflict,
    MoodleAttemptFinalized,
    MoodleBrowserService,
    MoodleContractError,
    MoodleProtocolError,
    PlaywrightTimeoutError,
)

BASE_URL = "https://edu.mmcs.sfedu.ru"


def storage_state() -> BrowserStorageState:
    return BrowserStorageState.model_validate(
        {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "test-session",
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


def request(
    *, finalize: bool = True, slots: tuple[str, ...] = ("1", "2")
) -> QuizAnswersSyncRequest:
    return QuizAnswersSyncRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "508",
            "cmid": 31529,
            "expected_attempt_id": "123",
            "finalize": finalize,
            "idempotency_key": "batch:123:revision1",
            "storage_state": storage_state(),
            "answers": [
                {
                    "question_slot": slot,
                    "answer_transport": "ESSAY_ONLINE_TEXT",
                    "artifact": {
                        "filename": "main.cpp",
                        "sha256": hashlib.sha256(f"answer {slot}".encode()).hexdigest(),
                        "content_base64": base64.b64encode(f"answer {slot}".encode()).decode(),
                    },
                }
                for slot in slots
            ],
        }
    )


def question_html(slot: str) -> str:
    return f"""
    <div class="que essay" id="question-123-{slot}" data-slot="{slot}">
      <div class="info"><div class="grade">Балл: 5,00</div></div>
      <div class="qtext">Задача {slot}</div>
      <textarea name="q123:{slot}_answer"></textarea>
    </div>
    """


def page_html(slots: tuple[str, ...], *, paginated: bool = False) -> str:
    navigation = "".join(
        f'<a class="qnbutton" id="quiznavbutton{slot}" '
        'href="/mod/quiz/attempt.php?attempt=123&amp;cmid=31529'
        f"&amp;page={int(slot) - 1 if paginated else 0}"
        f'#question-123-{slot}">{slot}</a>'
        for slot in ("1", "2")
    )
    return f"""
    <body class="course-508"><a href="/course/view.php?id=508">Course</a>
      {navigation}<form>{"".join(question_html(slot) for slot in slots)}</form>
    </body>
    """


def test_two_questions_have_independent_slots_text_controls_and_scales() -> None:
    markup = page_html(("1", "2"))
    url = f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=31529"
    questions = parse_attempt_questions(
        markup,
        url,
        base_url=BASE_URL,
        course_id="508",
        cmid=31529,
    )
    assert [
        (q.question_slot, q.question_text, q.online_text_control_name, q.question_max_mark)
        for q in questions
    ] == [
        ("1", "Задача 1", "q123:1_answer", 5),
        ("2", "Задача 2", "q123:2_answer", 5),
    ]
    assert quiz_attempt_navigation(markup, base_url=BASE_URL, cmid=31529, attempt_id="123") == {
        "1": 0,
        "2": 0,
    }
    with pytest.raises(MoodleMarkupError, match="exactly one"):
        parse_attempt_page(markup, url, base_url=BASE_URL, course_id="508", cmid=31529)


@pytest.mark.parametrize("fragment", ["attempt=999", "cmid=999", "page=32"])
def test_question_navigation_rejects_foreign_attempt_activity_and_unbounded_page(
    fragment: str,
) -> None:
    markup = page_html(("1", "2"))
    key = fragment.split("=")[0]
    old = {"attempt": "123", "cmid": "31529", "page": "0"}[key]
    markup = markup.replace(f"{key}={old}", fragment)
    with pytest.raises(MoodleMarkupError):
        quiz_attempt_navigation(markup, base_url=BASE_URL, cmid=31529, attempt_id="123")


def test_duplicate_slots_and_partial_receipts_are_rejected() -> None:
    with pytest.raises(ValidationError, match="unique"):
        request(slots=("1", "1"))
    data = request().model_dump()
    data["answers"][0]["previous_managed_filename"] = "main.cpp"
    with pytest.raises(ValidationError, match="receipt is incomplete"):
        QuizAnswersSyncRequest.model_validate(data)


def test_live_navigation_supports_current_fragment_and_next_page_empty_fragment() -> None:
    markup = (
        page_html(("1",), paginated=True)
        .replace(
            "/mod/quiz/attempt.php?attempt=123&amp;cmid=31529&amp;page=0#question-123-1",
            "#question-123-1",
        )
        .replace("page=1#question-123-2", "page=1#")
    )
    assert quiz_attempt_navigation(
        markup,
        base_url=BASE_URL,
        cmid=31529,
        attempt_id="123",
        current_url=f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=31529",
    ) == {"1": 0, "2": 1}


def test_disabled_question_navigation_cannot_hide_unvisited_questions() -> None:
    markup = page_html(("1",), paginated=True).replace(
        '<a class="qnbutton" id="quiznavbutton2"',
        '<span class="qnbutton" id="quiznavbutton2"',
    )
    with pytest.raises(MoodleMarkupError, match="not freely available"):
        quiz_attempt_navigation(markup, base_url=BASE_URL, cmid=31529, attempt_id="123")


@pytest.mark.asyncio
async def test_collector_visits_and_confirms_every_question_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    class Page:
        url = f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=31529&page=0"

    page = Page()
    visited: list[int] = []

    async def goto(page: Page, url: str) -> str:
        page.url = url
        number = int(parse_qs(urlsplit(url).query)["page"][0])
        visited.append(number)
        return page_html((str(number + 1),), paginated=True)

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    prepare = QuizEssayPrepareRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "508",
            "cmid": 31529,
            "expected_attempt_id": "123",
            "expected_question_slot": "1",
            "storage_state": storage_state(),
        }
    )
    questions = await service._collect_quiz_attempt_questions(
        page,
        object(),
        prepare,
        page_html(("1",), paginated=True),  # type: ignore[arg-type]
    )
    assert [(q.question_slot, q.page) for q in questions] == [("1", 0), ("2", 1)]
    assert visited == [1, 0]


def test_discovery_preserves_each_essay_reference_for_confirmation() -> None:
    markup = '<body class="course-508"><input name="cmid" value="31529"><ul class="slots">'
    for slot in ("1", "2"):
        markup += (
            f'<li class="slot qtype_essay" data-slot="{slot}">'
            '<a href="/question/bank/editquestion/question.php?'
            f'cmid=31529&amp;id={slot}">Edit</a></li>'
        )
    markup += "</ul></body>"
    summary = parse_quiz_question_summary(markup, course_id="508", cmid=31529, base_url=BASE_URL)
    assert summary["import_supported"] is False  # Each response format still needs confirmation.
    assert [q["question_slot"] for q in summary["_questions"]] == ["1", "2"]
    assert [q["_essay_question_id"] for q in summary["_questions"]] == ["1", "2"]


@pytest.mark.asyncio
async def test_bound_delivery_reads_other_page_metadata_without_relaunching_or_reloading(
    monkeypatch: pytest.MonkeyPatch,
):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    visited = []
    metadata_reads = []

    class Page:
        url = ""

    page = Page()

    async def goto(_page, url):
        assert url.endswith("attempt=123&cmid=31529&page=0")
        visited.append(url)
        page.url = url
        return page_html(("1",), paginated=True)

    async def metadata(_page, url):
        assert url.endswith("attempt=123&cmid=31529&page=1")
        metadata_reads.append(url)
        return page_html(("2",), paginated=True)

    async def authenticated(*_args):
        pass

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_read_quiz_metadata_page", metadata)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    preparation = QuizEssayPrepareRequest(
        schema_version="1.0", base_url=BASE_URL, course_id="508", cmid=31529,
        expected_attempt_id="123", expected_question_slot="1", storage_state=storage_state(),
    )
    result = await service._open_bound_quiz_attempt(page, object(), preparation)
    assert [(q.question_slot, q.page) for q in result.questions] == [("1", 0), ("2", 1)]
    assert len(visited) == len(metadata_reads) == 1
    assert page.url == visited[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["attempt", "activity", "course", "preview"])
async def test_bound_delivery_rejects_changed_identity_before_writing(monkeypatch, fault):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    markup = page_html(("1", "2"))

    class Page:
        url = ""

    async def goto(page, url):
        page.url = url.replace("attempt=123", "attempt=999") if fault == "attempt" else url
        if fault == "activity":
            page.url = url.replace("cmid=31529", "cmid=999")
        if fault == "course":
            return markup.replace("508", "999")
        if fault == "preview":
            return markup.replace(
                '<body class="course-508">',
                '<body class="course-508"><div class="quizpreview">Preview</div>',
            )
        return markup

    async def authenticated(*_args):
        pass

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    preparation = QuizEssayPrepareRequest(
        schema_version="1.0", base_url=BASE_URL, course_id="508", cmid=31529,
        expected_attempt_id="123", expected_question_slot="1", storage_state=storage_state(),
    )
    with pytest.raises((MoodleProtocolError, MoodleMarkupError)):
        await service._open_bound_quiz_attempt(Page(), object(), preparation)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["redirect", "url", "oversized", "empty", "server"])
async def test_metadata_read_refuses_redirects_changed_urls_and_unbounded_responses(fault):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    target = f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=31529&page=1"
    result = {"status": 200, "url": target, "html": "<html></html>"}
    if fault == "redirect":
        result["status"] = 0  # fetch redirect:manual returns an opaque redirect.
    elif fault == "url":
        result["url"] = "https://foreign.test/"
    elif fault == "oversized":
        result["tooLarge"] = True
    elif fault == "empty":
        result.pop("html")
    else:
        result["status"] = 503

    class Page:
        async def evaluate(self, script, payload):
            assert 'redirect: "manual"' in script
            assert payload["url"] == target and payload["maxBytes"] == 2 * 1024 * 1024
            return result

    with pytest.raises((MoodleProtocolError, BrowserUnavailable)):
        await service._read_quiz_metadata_page(Page(), target)


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse", [False, True])
async def test_question_reload_waits_for_scoped_live_filemanager_before_verifying(
    monkeypatch: pytest.MonkeyPatch,
    reuse: bool,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    item = service._quiz_answer_request(request(finalize=False), "1").model_copy(
        update={"answer_transport": "ESSAY_ATTACHMENT"}
    )
    question = QuizAttempt("123", "1", (), ("ESSAY_ATTACHMENT",))
    shell = (
        '<body class="course-508"><div class="que essay" id="question-123-1">'
        '<div class="qtext">Задача 1</div><div class="filemanager w-100 fm-loading">'
        '<a class="fp-btn-add" href="#">Добавить</a>'
        '<div class="filemanager-container"></div></div></div></body>'
    )
    loaded = shell.replace("fm-loading", "fm-loaded fm-nomkdir").replace(
        '<div class="filemanager-container"></div>',
        '<div class="filemanager-container"><a href="#">▶</a><a href="#">'
        '<span class="fp-filename">main.cpp</span></a></div>',
    )

    class Locator:
        async def wait_for(self, **_kwargs: object) -> None:
            return None

    class Page:
        url = f"{BASE_URL}/mod/quiz/attempt.php?attempt=123&cmid=31529&page=0"
        ready = False

        def locator(self, selector: str) -> Locator:
            assert "[id$='-1']" in selector
            return Locator()

        async def wait_for_function(self, script: str, *, arg: str, timeout: int) -> None:
            assert "[id$='-1']" in arg
            assert "managers.length !== 1" in script
            assert "classes.contains('fm-loaded')" in script
            assert "!classes.contains('fm-loading')" in script
            assert "!classes.contains('fm-updating')" in script
            self.ready = True

        async def content(self) -> str:
            return loaded if self.ready else shell

    navigations: list[str] = []
    async def goto(page: Page, url: str) -> str:
        navigations.append(url)
        page.url = url
        return shell

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    page = Page()
    returned = await service._read_quiz_question(
        page, object(), item, question, reuse_current_page=reuse
    )
    assert len(navigations) == (0 if reuse else 1)
    assert returned.existing_filenames == ("main.cpp",)
    assert returned.attachment_urls == ()  # Live Moodle exposes only href="#" here.

    async def unavailable_target(*_args: object, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr(service, "_quiz_attachment_download_url", unavailable_target)
    assert not await service._quiz_answer_matches(page, item, returned, b"answer 1")
    assert await service._quiz_answer_matches(
        page, item, returned, b"answer 1", allow_named_attachment=True
    )
    assert not await service._quiz_answer_matches(
        page, item, returned, b"answer 1", allow_named_attachment=True,
        require_verified_match=True,
    )


@pytest.mark.asyncio
async def test_post_save_verification_keeps_precise_attachment_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    item = service._quiz_answer_request(request(), "1").model_copy(
        update={"answer_transport": "ESSAY_ATTACHMENT"}
    )
    question = QuizAttempt(
        "123",
        "1",
        ("main.cpp",),
        ("ESSAY_ATTACHMENT",),
        attachment_urls=(("main.cpp", f"{BASE_URL}/draftfile.php/123/main.cpp"),),
    )

    async def verify(*_args: object, **_kwargs: object) -> bool:
        raise MoodleProtocolError("Moodle managed artifact could not be downloaded safely")

    monkeypatch.setattr(service, "_verify_quiz_managed_file_if_exposed", verify)
    assert not await service._quiz_answer_matches(object(), item, question, b"answer 1")
    with pytest.raises(MoodleProtocolError, match="could not be downloaded safely"):
        await service._quiz_answer_matches(
            object(), item, question, b"answer 1", require_verified_match=True
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_filename", [False, True])
async def test_lazy_attachment_target_is_read_only_scoped_and_download_is_cancelled(
    wrong_filename: bool,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    clicks: list[str] = []
    cancelled: list[bool] = []
    visible = False
    target = f"{BASE_URL}/draftfile.php/216602/user/draft/221149629/main.cpp"

    class Download:
        url = target
        suggested_filename = "wrong.cpp" if wrong_filename else "main.cpp"

        async def cancel(self) -> None:
            cancelled.append(True)

    class Locator:
        def __init__(self, kind: str):
            self.kind = kind

        def locator(self, selector: str) -> Locator:
            allowed = {
                ("manager", ".fp-filename:visible"): "label",
                ("dialogue", ".fp-file-download"): "download",
                ("dialogue", ".fp-file-cancel"): "cancel",
            }
            return Locator(allowed[(self.kind, selector)])

        def filter(self, *, has_text):  # type: ignore[no-untyped-def]
            assert has_text.fullmatch("main.cpp") and not has_text.fullmatch("other.cpp")
            return self

        def nth(self, index: int) -> Locator:
            assert index == 0
            return self

        async def count(self) -> int:
            return int(visible) if self.kind == "dialogue" else 1

        async def is_visible(self) -> bool:
            return visible

        async def click(self) -> None:
            nonlocal visible
            clicks.append(self.kind)
            if self.kind == "label":
                visible = True
            elif self.kind == "cancel":
                visible = False

        async def wait_for(self, *, state: str, timeout: int) -> None:
            assert state == "hidden" and not visible

    class Pending:
        @property
        def value(self):  # type: ignore[no-untyped-def]
            async def result() -> Download:
                return Download()

            return result()

    class Page:
        def locator(self, selector: str) -> Locator:
            if selector == ".moodle-dialogue:visible":
                return Locator("dialogue")
            assert "[id$='-2']" in selector and selector.endswith(" .filemanager")
            return Locator("manager")

        @asynccontextmanager
        async def expect_download(self, *, timeout: int):  # type: ignore[no-untyped-def]
            assert timeout == service.settings.navigation_timeout_ms
            yield Pending()

    if wrong_filename:
        with pytest.raises(MoodleProtocolError, match="download filename changed"):
            await service._quiz_attachment_download_url(
                Page(), question_slot="2", filename="main.cpp"
            )
    else:
        assert (
            await service._quiz_attachment_download_url(
                Page(), question_slot="2", filename="main.cpp"
            )
            == target
        )
    assert clicks == ["label", "download", "cancel"]
    assert cancelled == [True] and not visible


@pytest.mark.asyncio
async def test_cold_retry_verifies_lazy_attachment_hash_without_ownership_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    item = service._quiz_answer_request(request(), "1").model_copy(
        update={"answer_transport": "ESSAY_ATTACHMENT"}
    )
    question = QuizAttempt("123", "1", ("main.cpp",), ("ESSAY_ATTACHMENT",))
    target = f"{BASE_URL}/draftfile.php/123/user/draft/456/main.cpp"
    checks: list[str] = []

    async def read_target(_page, *, question_slot: str, filename: str) -> str:  # type: ignore[no-untyped-def]
        assert question_slot == "1" and filename == "main.cpp"
        return target

    async def verify(_page, *, filename, expected_sha256, attachment_urls):  # type: ignore[no-untyped-def]
        assert filename == "main.cpp" and attachment_urls == (("main.cpp", target),)
        checks.append(expected_sha256)
        return True

    monkeypatch.setattr(service, "_quiz_attachment_download_url", read_target)
    monkeypatch.setattr(service, "_verify_quiz_managed_file_if_exposed", verify)
    assert await service._quiz_answer_matches(object(), item, question, b"answer 1")
    assert checks == [item.artifact.sha256]


@pytest.mark.asyncio
async def test_replacement_checks_previous_hash_even_when_download_url_is_lazy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    previous_sha = hashlib.sha256(b"previous code").hexdigest()
    target = f"{BASE_URL}/draftfile.php/123/user/draft/456/main.cpp"

    class Locator:
        @property
        def first(self) -> Locator:
            return self

        async def count(self) -> int:
            return 1

        def get_by_text(self, text: str, *, exact: bool) -> Locator:
            assert text == "main.cpp" and exact
            return self

        def filter(self, *, visible: bool) -> Locator:
            assert visible
            return self

        def locator(self, selector: str) -> Locator:
            assert selector.startswith('[data-filename="main.cpp"]')
            return self

        async def click(self) -> None:
            pytest.fail("No upload or overwrite is allowed after a remote hash change")

    class Page:
        def locator(self, selector: str) -> Locator:
            assert "[id$='-1']" in selector
            return Locator()

    async def read_target(*_args: object, **kwargs: object) -> str:
        assert kwargs == {"question_slot": "1", "filename": "main.cpp"}
        return target

    async def verify(*_args: object, **kwargs: object) -> bool:
        assert kwargs["expected_sha256"] == previous_sha
        assert kwargs["attachment_urls"] == (("main.cpp", target),)
        raise MoodleProtocolError("Moodle managed artifact changed")

    monkeypatch.setattr(service, "_quiz_attachment_download_url", read_target)
    monkeypatch.setattr(service, "_verify_quiz_managed_file_if_exposed", verify)
    with pytest.raises(MoodleProtocolError, match="artifact changed"):
        await service._replace_quiz_attachment(
            Page(),
            "main.cpp",
            b"new code",
            replace_existing=True,
            existing_filenames=("main.cpp",),
            previous_managed_filename="main.cpp",
            previous_managed_sha256=previous_sha,
            question_slot="1",
        )


@pytest.fixture
def batch_service(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    questions = tuple(
        QuizAttempt(
            "123",
            slot,
            (),
            ("ESSAY_ONLINE_TEXT",),
            f"q123:{slot}_answer",
            question_text=f"Задача {slot}",
        )
        for slot in ("1", "2")
    )
    saved: dict[str, bytes] = {}
    writes: list[str] = []
    finishes: list[dict[str, bytes]] = []
    failures: set[str] = set()

    class Page:
        url = ""

    class Context:
        async def new_page(self) -> Page:
            return Page()

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def operation(**_kwargs: object):  # type: ignore[no-untyped-def]
        yield object()

    async def new_context(*_args: object, **_kwargs: object) -> Context:
        return Context()

    async def open_attempt(*_args: object) -> QuizAttempt:
        return replace(questions[0], questions=questions)

    async def read_question(_page, _context, _request, question, **_kwargs):  # type: ignore[no-untyped-def]
        return question

    async def matches(_page, _request, question, artifact, **_kwargs):  # type: ignore[no-untyped-def]
        return saved.get(question.question_slot) == artifact

    async def write(_page, question, artifact, *, question_scoped=False):  # type: ignore[no-untyped-def]
        assert question_scoped
        if question.question_slot in failures:
            failures.remove(question.question_slot)
            raise BrowserUnavailable("temporary upload failure")
        writes.append(question.question_slot)
        saved[question.question_slot] = artifact

    async def finish(*_args: object) -> None:
        assert set(saved) == {"1", "2"}
        finishes.append(dict(saved))
        if "finish" in failures:
            failures.remove("finish")
            raise BrowserUnavailable("confirmation timeout")

    async def noop(*_args: object, **_kwargs: object) -> None:
        return None

    async def goto(page: Page, url: str) -> str:
        page.url = url
        return "<html>summary</html>"

    async def state(*_args: object) -> BrowserStorageState:
        return storage_state()

    monkeypatch.setattr(service, "_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_open_bound_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_read_quiz_question", read_question)
    monkeypatch.setattr(service, "_quiz_answer_matches", matches)
    monkeypatch.setattr(service, "_replace_quiz_online_text", write)
    monkeypatch.setattr(service, "_save_quiz_question_page", noop)
    monkeypatch.setattr(service, "_finalize_quiz_attempt", finish)
    monkeypatch.setattr(service, "_resume_quiz_answers_finalization", noop)
    monkeypatch.setattr(service, "_require_authenticated_page", noop)
    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_state", state)
    monkeypatch.setattr(
        "moodle_browser.service.parse_summary_page",
        lambda *_a, **_kw: QuizSummary("123", "Finish"),
    )
    return service, saved, writes, finishes, failures


@pytest.mark.asyncio
async def test_batch_saves_both_independent_answers_before_finishing(batch_service):  # type: ignore[no-untyped-def]
    service, saved, writes, finishes, _failures = batch_service
    response = await service.sync_quiz_answers(request())
    assert saved == {"1": b"answer 1", "2": b"answer 2"}
    assert writes == ["1", "2"] and finishes == [saved]
    assert response.status == "FINALIZED"
    assert [receipt.question_slot for receipt in response.receipts] == ["1", "2"]
    assert [receipt.filename for receipt in response.receipts] == ["main.cpp", "main.cpp"]
    await service.sync_quiz_answers(request())
    assert writes == ["1", "2"] and len(finishes) == 1


@pytest.mark.asyncio
async def test_delivery_timings_expose_stages_but_not_answers_or_credentials(
    batch_service, caplog,
):
    service, *_ = batch_service
    with caplog.at_level(logging.INFO, logger="uvicorn.error.moodle_browser"):
        await service.sync_quiz_answers(request())
    for stage in ("queued", "browser_ready", "contract_ready", "page_saved",
                  "answers_verified", "finalized", "delivered"):
        assert f"stage={stage} " in caplog.text
    assert "attempt=123 cmid=31529" in caplog.text
    assert "elapsed_ms=" in caplog.text
    assert "answer 1" not in caplog.text and "answer 2" not in caplog.text
    assert "test-session" not in caplog.text and "batch:123:revision1" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("finalize", [False, True])
async def test_batch_saves_each_page_once_and_verifies_each_answer_once(
    batch_service, monkeypatch: pytest.MonkeyPatch, finalize: bool,
):
    service, saved, writes, _finishes, _failures = batch_service
    page_saves = []
    verified = []
    reads = []
    questions = tuple(
        QuizAttempt("123", str(slot), (), ("ESSAY_ONLINE_TEXT",),
                    f"q123:{slot}_answer", page=0 if slot < 3 else 1)
        for slot in (1, 2, 3)
    )

    async def open_attempt(*_args):
        return replace(questions[0], questions=questions)

    async def read(_page, _context, _request, question, **kwargs):
        reads.append((question.question_slot, kwargs.get("reuse_current_page")))
        return question

    async def matches(_page, _request, question, artifact, **kwargs):
        if kwargs.get("require_verified_match"):
            verified.append(question.question_slot)
        return saved.get(question.question_slot) == artifact

    async def save(_page, _context, _request, question):
        # All answers on this page must be filled before leaving its form.
        assert {q.question_slot for q in questions if q.page == question.page} <= set(saved)
        page_saves.append(question.page)

    async def finish(*_args):
        assert verified == ["1", "2", "3"]

    monkeypatch.setattr(service, "_open_bound_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_read_quiz_question", read)
    monkeypatch.setattr(service, "_quiz_answer_matches", matches)
    monkeypatch.setattr(service, "_save_quiz_question_page", save)
    monkeypatch.setattr(service, "_finalize_quiz_attempt", finish)
    await service.sync_quiz_answers(request(finalize=finalize, slots=("1", "2", "3")))
    assert page_saves == [0, 1]
    assert writes == verified == ["1", "2", "3"]
    assert reads.count(("1", False)) == reads.count(("3", False)) == 1
    assert ("2", False) not in reads  # No reload between edits/verifications on the same page.


@pytest.mark.asyncio
async def test_lost_second_answer_prevents_finalization_after_optimized_page_save(
    batch_service, monkeypatch: pytest.MonkeyPatch,
):
    service, saved, _writes, finishes, _failures = batch_service

    async def matches(_page, _request, question, artifact, **kwargs):
        if kwargs.get("require_verified_match") and question.question_slot == "2":
            return False
        return saved.get(question.question_slot) == artifact

    monkeypatch.setattr(service, "_quiz_answer_matches", matches)
    with pytest.raises(MoodleProtocolError, match="changed before finalization"):
        await service.sync_quiz_answers(request())
    assert not finishes


@pytest.mark.asyncio
async def test_preparation_returns_all_questions_and_legacy_first_question(batch_service):  # type: ignore[no-untyped-def]
    service, *_ = batch_service
    response = await service.prepare_quiz_essay(
        QuizEssayPrepareRequest.model_validate(
            {
                "schema_version": "1.0",
                "base_url": BASE_URL,
                "course_id": "508",
                "cmid": 31529,
                "expected_attempt_id": "123",
                "expected_question_slot": "1",
                "storage_state": storage_state(),
            }
        )
    )
    assert response.preparation.question_slot == "1"
    assert response.preparation.question_text == "Задача 1"
    assert [q.question_slot for q in response.preparation.questions] == ["1", "2"]
    assert [q.question_text for q in response.preparation.questions] == ["Задача 1", "Задача 2"]


@pytest.mark.asyncio
async def test_missing_final_answer_fails_before_any_write(batch_service):  # type: ignore[no-untyped-def]
    service, saved, writes, finishes, _failures = batch_service
    with pytest.raises(MoodleContractError, match="missing or foreign slots"):
        await service.sync_quiz_answers(request(slots=("1",)))
    assert not saved and not writes and not finishes


@pytest.mark.asyncio
async def test_checkpoint_subset_never_finalizes(batch_service):  # type: ignore[no-untyped-def]
    service, saved, writes, finishes, _failures = batch_service
    response = await service.sync_quiz_answers(request(finalize=False, slots=("2",)))
    assert response.status == "DRAFT_SAVED"
    assert saved == {"2": b"answer 2"} and writes == ["2"] and not finishes


@pytest.mark.asyncio
async def test_partial_failure_retries_without_finishing_or_rewriting_first_answer(batch_service):  # type: ignore[no-untyped-def]
    service, saved, writes, finishes, failures = batch_service
    failures.add("2")
    with pytest.raises(BrowserUnavailable):
        await service.sync_quiz_answers(request())
    assert saved == {"1": b"answer 1"} and not finishes
    response = await service.sync_quiz_answers(request())
    assert response.status == "FINALIZED"
    assert writes == ["1", "2"] and len(finishes) == 1


@pytest.mark.asyncio
async def test_loading_timeout_is_retryable_and_never_reported_as_a_missing_answer(
    monkeypatch: pytest.MonkeyPatch,
    batch_service,  # type: ignore[no-untyped-def]
):
    service, _saved, writes, finishes, _failures = batch_service

    async def timed_out(*_args: object, **_kwargs: object) -> None:
        raise PlaywrightTimeoutError("file manager still loading")

    monkeypatch.setattr(service, "_read_quiz_question", timed_out)
    with pytest.raises(BrowserUnavailable, match="synchronization was interrupted"):
        await service.sync_quiz_answers(request())
    assert writes == [] and finishes == []


@pytest.mark.asyncio
async def test_final_confirmation_retry_does_not_reupload_any_answer(batch_service):  # type: ignore[no-untyped-def]
    service, _saved, writes, finishes, failures = batch_service
    failures.add("finish")
    with pytest.raises(BrowserUnavailable):
        await service.sync_quiz_answers(request())
    assert writes == ["1", "2"] and len(finishes) == 1
    assert (await service.sync_quiz_answers(request())).status == "FINALIZED"
    assert writes == ["1", "2"] and len(finishes) == 1


@pytest.mark.asyncio
async def test_pending_finalization_rechecks_other_slot_and_never_overwrites_changed_answer(
    batch_service,  # type: ignore[no-untyped-def]
):
    service, saved, writes, finishes, _failures = batch_service
    saved.update({"1": b"answer 1", "2": b"externally changed second answer"})
    bundle = request()
    per_slot = {
        answer.question_slot: service._quiz_answer_request(bundle, answer.question_slot)
        for answer in bundle.answers
    }
    artifacts = {slot: service._decode_artifact(item) for slot, item in per_slot.items()}
    with pytest.raises(MoodleProtocolError, match="changed before finalization retry"):
        await MoodleBrowserService._resume_quiz_answers_finalization(
            service, object(), object(), bundle, per_slot, artifacts
        )
    assert writes == [] and finishes == []
    assert saved["2"] == b"externally changed second answer"


@pytest.mark.asyncio
async def test_bundle_idempotency_key_cannot_be_reused_for_another_slot_set(batch_service):  # type: ignore[no-untyped-def]
    service, *_ = batch_service
    await service.sync_quiz_answers(request())
    with pytest.raises(IdempotencyConflict):
        await service.sync_quiz_answers(request(slots=("1",)))


def terminal_review(*, attachments: bool = False) -> str:
    content = (
        '<body class="course-508"><a href="/course/view.php?id=508">Course</a>'
        '<a href="/user/profile.php?id=77">Student</a>'
        '<table class="quizreviewsummary"><tr><th>Состояние</th><td>Завершены</td></tr></table>'
    )
    for slot in ("1", "2"):
        answer = (
            f'<a href="/pluginfile.php/508/question/response_attachments/{slot}/main.cpp">'
            'main.cpp</a>'
            if attachments
            else f"<pre>\nanswer {slot}\r\n</pre>"
        )
        content += (
            f'<div class="que essay" id="question-123-{slot}">'
            f'<div class="qtext">Задача {slot}</div>'
            f'<div class="answer"><div class="qtype_essay_response">{answer}</div></div></div>'
        )
    return content + "</body>"


def finalized_reader(monkeypatch, service, markup):  # type: ignore[no-untyped-def]
    reads: list[str] = []

    async def open_attempt(*_args: object) -> None:
        raise MoodleAttemptFinalized("Moodle attempt already finalized")

    async def goto(page, url):  # type: ignore[no-untyped-def]
        page.url = url
        reads.append(url)
        if "/user/profile.php" in url:
            return (
                '<div class="usermenu"><a href="/user/profile.php?id=77">'
                '<span class="usertext">Student</span></a></div>'
            )
        return markup

    monkeypatch.setattr(service, "_open_real_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_open_bound_quiz_attempt", open_attempt)
    monkeypatch.setattr(service, "_goto", goto)
    return reads


@pytest.mark.asyncio
async def test_restart_reconciles_all_exact_online_answers_without_any_writes(
    batch_service,
    monkeypatch: pytest.MonkeyPatch,
):  # type: ignore[no-untyped-def]
    service, _saved, writes, finishes, _failures = batch_service
    reads = finalized_reader(monkeypatch, service, terminal_review())
    response = await service.sync_quiz_answers(request())
    assert response.status == "FINALIZED"
    assert [receipt.question_slot for receipt in response.receipts] == ["1", "2"]
    assert not writes and not finishes
    assert reads == [
        f"{BASE_URL}/user/profile.php",
        f"{BASE_URL}/mod/quiz/review.php?attempt=123&cmid=31529&showall=1",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["different_answer", "missing_slot", "not_terminal", "embargo"])
async def test_restart_never_confirms_missing_different_or_unreadable_answers(
    batch_service,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
):  # type: ignore[no-untyped-def]
    service, _saved, writes, finishes, _failures = batch_service
    markup = terminal_review()
    if fault == "different_answer":
        markup = markup.replace("answer 2", "different solution")
    elif fault == "missing_slot":
        markup = markup.replace('class="que essay" id="question-123-2"', 'class="hidden-question"')
    elif fault == "not_terminal":
        markup = markup.replace("Завершены", "В процессе")
    else:
        markup = '<body class="course-508">Просмотр попытки пока недоступен</body>'
    finalized_reader(monkeypatch, service, markup)
    with pytest.raises(MoodleProtocolError, match="complete answer bundle could not be verified"):
        await service.sync_quiz_answers(request())
    assert not writes and not finishes


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", [False, True])
async def test_restart_verifies_hash_of_each_question_attachment(
    batch_service,
    monkeypatch: pytest.MonkeyPatch,
    changed: bool,
):  # type: ignore[no-untyped-def]
    service, _saved, writes, finishes, _failures = batch_service
    finalized_reader(monkeypatch, service, terminal_review(attachments=True))
    payload = request().model_dump()
    for answer in payload["answers"]:
        answer["answer_transport"] = "ESSAY_ATTACHMENT"
    payload = QuizAnswersSyncRequest.model_validate(payload)
    checked: list[str] = []

    async def verify(_page, *, filename, expected_sha256, attachment_urls):  # type: ignore[no-untyped-def]
        assert filename == "main.cpp" and len(attachment_urls) == 1
        slot = "1" if "/1/" in attachment_urls[0][1] else "2"
        checked.append(slot)
        remote = f"answer {slot}".encode()
        if changed and slot == "2":
            remote = b"different file"
        if hashlib.sha256(remote).hexdigest() != expected_sha256:
            raise MoodleProtocolError("remote hash mismatch")
        return True

    monkeypatch.setattr(service, "_verify_quiz_managed_file_if_exposed", verify)
    if changed:
        with pytest.raises(
            MoodleProtocolError, match="complete answer bundle could not be verified"
        ):
            await service.sync_quiz_answers(payload)
    else:
        assert (await service.sync_quiz_answers(payload)).status == "FINALIZED"
    assert checked == ["1", "2"] and not writes and not finishes
