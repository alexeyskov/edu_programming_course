from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from moodle_browser.config import Settings
from moodle_browser.models import LoginRequest
from moodle_browser.service import (
    MoodleBrowserService,
    MoodleCredentialsRejected,
    MoodleProtocolError,
)

BASE_URL = "https://edu.mmcs.sfedu.ru"


@pytest.mark.asyncio
@pytest.mark.parametrize("login_only", [False, True])
async def test_script_execution_is_enabled_by_default_and_origin_guard_is_always_kept(login_only):
    class Context:
        def on(self, _event, _handler):
            pass

        def set_default_timeout(self, _timeout):
            pass

        def set_default_navigation_timeout(self, _timeout):
            pass

        async def route(self, pattern, handler):
            assert pattern == "**/*"
            self.handler = handler

    class Browser:
        async def new_context(self, **options):
            self.options = options
            return Context()

    class Request:
        method = "GET"

        def __init__(self, origin, resource_type):
            self.url = origin + "/resource"
            self.resource_type = resource_type

    class Route:
        aborted = False
        continued = False

        async def abort(self, reason):
            assert reason == "blockedbyclient"
            self.aborted = True

        async def continue_(self):
            self.continued = True

    service = MoodleBrowserService(
        Settings(shared_secret=b"test-login-navigation-secret-32-bytes")
    )
    browser = Browser()
    kwargs = {"html_only": True} if login_only else {}
    context = await service._new_context(browser, **kwargs)
    assert browser.options["java_script_enabled"] is not login_only
    assert browser.options["service_workers"] == "block"
    for origin in (BASE_URL, "https://foreign.example"):
        resource_types = ("document", "fetch", "xhr", "image", "font", "script", "stylesheet")
        for resource_type in resource_types:
            route = Route()
            await context.handler(route, Request(origin, resource_type))
            allowed = origin == BASE_URL and (
                not login_only or resource_type in {"document", "fetch", "xhr"}
            )
            assert route.continued is allowed
            assert route.aborted is not allowed


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("html_only", [False, True])
async def test_html_only_context_disables_scripts_but_editor_context_keeps_them(html_only):
    service = MoodleBrowserService(
        Settings(shared_secret=b"test-login-navigation-secret-32-bytes")
    )

    async def respond(route):
        await route.fulfill(
            status=200, content_type="text/html",
            body="<html><body><script>window.editorInitialized = true;</script></body></html>",
        )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await service._new_context(browser, html_only=html_only)
            page = await context.new_page()
            await page.route(BASE_URL + "/**", respond)
            await service._goto(page, BASE_URL + "/fixture")
            assert await page.evaluate("window.editorInitialized === true") is not html_only
        finally:
            await browser.close()


# Every Moodle request is fulfilled locally; no real credentials or external
# writes. Blocking theme scripts reproduce both parser-blocking and deferred
# script failures which used to consume the whole navigation deadline.
@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    ["success", "same_url_success", "rejected", "identity_changed", "missing_session"],
)
async def test_native_login_ignores_stalled_scripts_but_keeps_authentication_checks(outcome):
    release_scripts = asyncio.Event()
    script_requests = []
    submitted = []
    probed_courses = []
    authenticated = False

    def document(body):
        return (
            '<!doctype html><html lang="ru"><head>'
            '<script src="/slow-theme.js"></script>'
            '<script defer src="/slow-deferred.js"></script>'
            '<script type="module" src="/slow-module.js"></script>'
            '<link rel="stylesheet" href="/slow-theme.css">'
            '</head><body><img src="/slow-logo.png">' + body + '</body></html>'
        )

    # Both changing and unchanged POST URLs are valid Moodle login outcomes.
    # Use a native POST response rather than an HTTP redirect fixture: Chromium
    # does not re-intercept every URL in a fulfilled HTTP redirect chain.
    action = "/my/" if outcome in {"success", "identity_changed"} else "/login/index.php"
    login_html = document(f"""
        <form action="{action}" method="post">
          <input type="hidden" name="logintoken" value="server-issued-form-token">
          <input id="username" name="username">
          <input id="password" name="password" type="password">
          <button id="loginbtn" type="submit">Log in</button>
        </form>
    """)

    def identity_html(subject="42", extra=""):
        return document(f"""
          <div class="usermenu">
            <a href="/user/profile.php?id={subject}"><span class="usertext">Test User</span></a>
            <a href="/login/logout.php?sesskey=fixture-only">Logout</a>
          </div>
          <a class="coursename" href="/course/view.php?id=549">Test course</a>
          {extra}
        """)

    async def respond(route):
        nonlocal authenticated
        request = route.request
        parsed = urlsplit(request.url)
        if request.resource_type in {"image", "stylesheet"}:
            # Exercise the production login resource filter beneath this
            # fixture; those paths must never reach the network.
            await route.fallback()
            return
        if parsed.path.startswith("/slow-"):
            script_requests.append(request.url)
            await release_scripts.wait()
            with suppress(PlaywrightError):
                await route.fulfill(status=200, content_type="text/javascript", body="")
            return
        if request.method == "POST":
            assert parsed.path == action
            submitted.append(parse_qs(request.post_data))
            # The old document is still fully loaded until this response arrives.
            await asyncio.sleep(0.1)
            if outcome == "rejected":
                await route.fulfill(status=200, content_type="text/html", body=login_html)
                return
            authenticated = True
            headers = {} if outcome == "missing_session" else {
                "Set-Cookie": (
                    "MoodleSession=fixture-session; Path=/; Secure; HttpOnly; SameSite=Lax"
                )
            }
            await route.fulfill(
                status=200, content_type="text/html", body=identity_html(), headers=headers
            )
            return
        if parsed.path == "/login/index.php":
            await route.fulfill(status=200, content_type="text/html", body=login_html)
            return
        if parsed.path in {"/my/", "/user/profile.php"}:
            assert authenticated
            subject = "77" if outcome == "identity_changed" and parsed.query else "42"
            await route.fulfill(status=200, content_type="text/html", body=identity_html(subject))
            return
        if parsed.path == "/course/view.php":
            assert authenticated
            probed_courses.append(parse_qs(parsed.query)["id"][0])
            await route.fulfill(
                status=200, content_type="text/html",
                body=identity_html(extra='<a href="/course/edit.php?id=549">Settings</a>'),
            )
            return
        await route.abort()

    class FixtureService(MoodleBrowserService):
        async def _new_context(self, browser, **kwargs):
            assert kwargs["html_only"] is True
            context = await super()._new_context(browser, **kwargs)
            await context.route(BASE_URL + "/**", respond)
            return context

    service = FixtureService(Settings(
        shared_secret=b"test-login-navigation-secret-32-bytes",
        navigation_timeout_ms=2_000,
    ))
    request = LoginRequest(
        schema_version="1.0", base_url=BASE_URL,
        username="fixture-user", password="fixture-password",
        allowed_course_ids=["549"],
    )
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        service._browser = browser
        try:
            if outcome in {"rejected", "missing_session"}:
                with pytest.raises(MoodleCredentialsRejected):
                    await asyncio.wait_for(service.login(request), timeout=8)
            elif outcome == "identity_changed":
                with pytest.raises(MoodleProtocolError, match="identity changed"):
                    await asyncio.wait_for(service.login(request), timeout=8)
            else:
                result = await asyncio.wait_for(service.login(request), timeout=8)
                assert result.identity.external_subject == "42"
                assert [
                    (course.external_id, course.role) for course in result.identity.courses
                ] == [("549", "TEACHER")]
                assert probed_courses == ["549"]
                assert any(
                    cookie.name == "MoodleSession" for cookie in result.storage_state.cookies
                )
            assert submitted == [{
                "username": ["fixture-user"], "password": ["fixture-password"],
                "logintoken": ["server-issued-form-token"],
            }]
            assert script_requests == []
            assert browser.contexts == []
        finally:
            release_scripts.set()
            await browser.close()
