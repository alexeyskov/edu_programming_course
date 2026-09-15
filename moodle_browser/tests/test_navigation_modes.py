from __future__ import annotations

import asyncio
import os
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState, QuizEssayPrepareRequest
from moodle_browser.service import MoodleBrowserService

BASE = "https://edu.mmcs.sfedu.ru"


@pytest.mark.asyncio
@pytest.mark.parametrize("html_only,styled", [(False, False), (True, False), (False, True)])
async def test_reader_modes_keep_only_needed_assets(html_only, styled):
    class Context:
        def on(self, _event, _handler):
            pass

        def set_default_timeout(self, _value):
            pass

        def set_default_navigation_timeout(self, _value):
            pass

        async def route(self, _pattern, handler):
            self.handler = handler

    class Browser:
        async def new_context(self, **options):
            self.options = options
            return Context()

    class Request:
        method = "GET"
        url = BASE + "/fixture"

        def __init__(self, resource_type):
            self.resource_type = resource_type

    class Route:
        allowed = False

        async def continue_(self):
            self.allowed = True

        async def abort(self, _reason):
            pass

    browser = Browser()
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    context = await service._new_context(browser, html_only=html_only, server_rendered=styled)
    assert browser.options["java_script_enabled"] is not (html_only or styled)
    for kind in ("document", "fetch", "xhr", "script", "stylesheet", "image", "font", "media"):
        route = Route()
        await context.handler(route, Request(kind))
        assert route.allowed is (
            (not html_only and not styled)
            or kind in {"document", "fetch", "xhr"}
            or (styled and kind == "stylesheet")
        )


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
async def test_static_history_reader_waits_for_styles_but_never_for_theme_scripts():
    scripts = []
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    async def fixture(route):
        path = route.request.url
        if path == BASE + "/fixture":
            await route.fulfill(
                content_type="text/html",
                body="""<!doctype html>
                <link rel="stylesheet" href="/fixture.css">
                <script defer src="/never-finishes.js"></script>
                <div class="qtype_essay_response">  int x;\n    x++;</div>""",
            )
        elif path == BASE + "/fixture.css":
            await asyncio.sleep(0.15)
            await route.fulfill(
                content_type="text/css", body=".qtype_essay_response { white-space: pre; }"
            )
        elif path == BASE + "/never-finishes.js":
            scripts.append(path)
            await route.abort()
        else:
            await route.fallback()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await service._new_context(browser, server_rendered=True)
            await context.route("**/*", fixture)
            page = await context.new_page()
            await asyncio.wait_for(service._goto(page, BASE + "/fixture"), timeout=3)
            assert await page.locator(".qtype_essay_response").inner_text() == "  int x;\n    x++;"
            assert scripts == []
        finally:
            await browser.close()


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
async def test_editor_document_does_not_wait_for_unrelated_deferred_scripts():
    release = asyncio.Event()
    started = asyncio.Event()
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32, navigation_timeout_ms=1_000))

    async def fixture(route):
        if route.request.url == BASE + "/fixture":
            await route.fulfill(
                content_type="text/html",
                body="""<!doctype html>
                <script defer src="/slow-notifications.js"></script>
                <form id="answer"><textarea name="answer">saved code</textarea></form>""",
            )
        elif route.request.url == BASE + "/slow-notifications.js":
            started.set()
            await release.wait()
            await route.fulfill(content_type="text/javascript", body="window.moduleReady = true;")
        else:
            await route.abort()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await service._new_context(browser)
            await context.route("**/*", fixture)
            page = await context.new_page()
            markup = await asyncio.wait_for(service._goto(page, BASE + "/fixture"), timeout=2)
            await asyncio.wait_for(started.wait(), timeout=1)
            assert '<form id="answer">' in markup
            assert await page.evaluate("document.readyState") == "interactive"
            assert await page.locator("textarea").input_value() == "saved code"
            # Native editor scripts are still enabled and load normally; only
            # navigation is independent of unrelated deferred module readiness.
            release.set()
            await page.wait_for_load_state("domcontentloaded")
            assert await page.evaluate("window.moduleReady") is True
        finally:
            release.set()
            await browser.close()


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_html_only_preparation_starts_or_resumes_two_questions_with_native_forms(resume):
    requests = []
    script_requests = []
    identity = '<a href="/login/logout.php?sesskey=fixture">Logout</a>'

    def document(body):
        return (
            '<!doctype html><html><head><meta charset="utf-8"><script src="/stalled.js"></script>'
            '<link rel="stylesheet" href="/stalled.css"></head>'
            '<body class="course-549">' + identity + body + "</body></html>"
        )

    # The fixture's submit-button formaction models Moodle's server redirect
    # after preflight without ever following a redirect onto the real network.
    preflight = """<form id="mod_quiz_preflight_form" method="post"
        action="/mod/quiz/startattempt.php">
        <input type="hidden" name="cmid" value="777">
        <input type="hidden" name="sesskey" value="fixture">
        <input type="submit" name="submitbutton" value="Начать попытку"
          formaction="/mod/quiz/attempt.php?attempt=123&amp;cmid=777&amp;page=0">
        <input type="submit" name="cancel" value="Отмена"></form>"""

    async def fixture(route):
        request = route.request
        path = urlsplit(request.url).path
        if request.resource_type != "document":
            script_requests.append(request.resource_type)
            await route.fallback()  # The real html-only filter blocks these.
            return
        requests.append((request.method, path))
        if path == "/mod/quiz/view.php":
            body = (
                '<a href="/mod/quiz/attempt.php?attempt=123&amp;cmid=777&amp;page=0">'
                "Продолжить текущую попытку</a>"
                if resume
                else '<form action="/mod/quiz/startattempt.php" method="post">'
                '<input type="hidden" name="cmid" value="777">'
                '<input type="hidden" name="sesskey" value="fixture">'
                '<button type="submit">Пройти тест</button></form>'
                + preflight.replace("<form id=", '<form style="display:none" id=')
            )
        elif path == "/mod/quiz/startattempt.php":
            assert request.method == "POST" and not resume
            assert parse_qs(request.post_data)["sesskey"] == ["fixture"]
            body = preflight  # Native server-side preflight, no theme JavaScript.
        elif path == "/mod/quiz/attempt.php":
            slot = str(int(parse_qs(urlsplit(request.url).query)["page"][0]) + 1)
            body = (
                "".join(
                    f'<a class="qnbutton" id="quiznavbutton{index}" '
                    f'href="/mod/quiz/attempt.php?attempt=123&amp;cmid=777&amp;page={index - 1}"'
                    f">{index}</a>"
                    for index in (1, 2)
                )
                + f'''<form action="/mod/quiz/processattempt.php" method="post">
                <div class="que essay" id="question-123-{slot}" data-slot="{slot}">
                  <div class="qtext">Задача {slot}</div>
                  <textarea name="q123:{slot}_answer"></textarea>
                </div></form>'''
            )
        else:
            raise AssertionError(path)
        await route.fulfill(content_type="text/html", body=document(body))

    class FixtureService(MoodleBrowserService):
        async def _new_context(self, browser, **kwargs):
            assert kwargs["html_only"] is True
            context = await super()._new_context(browser, **kwargs)
            await context.route("**/*", fixture)
            return context

    service = FixtureService(Settings(shared_secret=b"x" * 32, navigation_timeout_ms=2_000))
    state = BrowserStorageState.model_validate(
        {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "fixture",
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
    request = QuizEssayPrepareRequest(
        schema_version="1.0",
        base_url=BASE,
        course_id="549",
        cmid=777,
        storage_state=state,
        expected_attempt_id="123" if resume else None,
        expected_question_slot="1" if resume else None,
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        service._browser = browser
        try:
            result = await asyncio.wait_for(service.prepare_quiz_essay(request), timeout=8)
            assert result.preparation.attempt_id == "123"
            assert [(q.question_slot, q.question_text) for q in result.preparation.questions] == [
                ("1", "Задача 1"),
                ("2", "Задача 2"),
            ]
            assert [path for method, path in requests if method == "POST"] == (
                [] if resume else ["/mod/quiz/startattempt.php", "/mod/quiz/attempt.php"]
            )
            assert "script" not in script_requests
        finally:
            await browser.close()
