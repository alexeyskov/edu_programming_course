from __future__ import annotations

import asyncio
import logging
import os
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from moodle_browser.config import Settings
from moodle_browser.service import (
    BrowserNavigationUnavailable,
    MoodleBrowserService,
    MoodleProtocolError,
)

BASE = "https://edu.mmcs.sfedu.ru"
PRIVATE = "private-session-secret"


class Page:
    def __init__(self, mode="interactive", *, status=200, redirect=None, failure=None):
        self.context = SimpleNamespace(_eduprog_navigation_mode=mode)
        self.url = redirect or BASE + "/mod/quiz/attempt.php?attempt=123&sesskey=" + PRIVATE
        self.status = status
        self.failure = failure
        self.ready = False
        self.content_read = False

    async def goto(self, _url, *, wait_until):
        assert wait_until == "commit"
        if self.failure is not None:
            raise self.failure
        return SimpleNamespace(status=self.status)

    async def wait_for_function(self, *_args, **_kwargs):
        self.ready = True

        class Handle:
            async def dispose(self):
                pass

        return Handle()

    async def wait_for_load_state(self, mode):
        assert mode == self.context._eduprog_navigation_mode
        self.ready = True

    async def content(self):
        assert self.ready
        self.content_read = True
        return "<form>complete document</form>"


def service():
    return MoodleBrowserService(Settings(shared_secret=b"x" * 32, navigation_timeout_ms=1000))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "load", "domcontentloaded"])
async def test_ready_document_is_required_in_every_mode_and_logs_omit_private_url(mode, caplog):
    caplog.set_level(logging.INFO, logger="uvicorn.error.moodle_browser")
    page = Page(mode)
    assert await service()._goto(page, page.url) == "<form>complete document</form>"
    assert page.content_read and page.ready
    assert "Moodle navigation response target=quiz_attempt" in caplog.text
    assert "Moodle navigation ready target=quiz_attempt" in caplog.text
    assert PRIVATE not in caplog.text and "sesskey" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "load", "domcontentloaded"])
async def test_http_failure_does_not_wait_for_error_pages_scripts_or_styles(mode):
    page = Page(mode, status=503)
    with pytest.raises(BrowserNavigationUnavailable) as caught:
        await service()._goto(page, page.url)
    assert caught.value.diagnostic_code == "MOODLE_HTTP_ERROR"
    assert "HTTP_503" in str(caught.value)
    assert not page.ready and not page.content_read


@pytest.mark.asyncio
async def test_foreign_redirect_is_rejected_before_reading_or_running_page_controls():
    page = Page(redirect="https://foreign.example/private")
    with pytest.raises(MoodleProtocolError, match="outside"):
        await service()._goto(page, BASE + "/mod/quiz/attempt.php?attempt=123")
    assert not page.ready and not page.content_read


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["response", "document", "content"])
async def test_timeout_keeps_the_exact_phase_without_exposing_playwright_call_logs(phase, caplog):
    async def fail(*_args, **_kwargs):
        raise PlaywrightTimeoutError(f"Timeout loading https://host/?sesskey={PRIVATE}")

    page = Page()
    if phase == "response":
        page.goto = fail
    elif phase == "document":
        page.wait_for_function = fail
    else:
        page.content = fail
    with pytest.raises(BrowserNavigationUnavailable) as caught:
        await service()._goto(page, page.url)
    assert f"phase={phase}" in str(caught.value)
    assert caught.value.diagnostic_code == (
        "MOODLE_RESPONSE_TIMEOUT" if phase == "response" else "MOODLE_DOCUMENT_TIMEOUT"
    )
    assert PRIVATE not in caplog.text and PRIVATE not in str(caught.value)
    assert "sesskey" not in caplog.text


@pytest.mark.parametrize("marker,code", [
    ("ERR_NAME_NOT_RESOLVED", "MOODLE_DNS_ERROR"),
    ("ERR_CONNECTION_REFUSED", "MOODLE_CONNECTION_ERROR"),
    ("ERR_CONNECTION_RESET", "MOODLE_CONNECTION_ERROR"),
    ("ERR_CERT_AUTHORITY_INVALID", "MOODLE_TLS_ERROR"),
    ("ERR_SSL_PROTOCOL_ERROR", "MOODLE_TLS_ERROR"),
    (PRIVATE, "MOODLE_NAVIGATION_ERROR"),
])
def test_network_diagnostics_are_allowlisted(marker, code):
    error = BrowserNavigationUnavailable(
        "response", PlaywrightError(f"net::{marker} at https://host/?sesskey={PRIVATE}")
    )
    assert error.diagnostic_code == code
    assert PRIVATE not in str(error) and "sesskey" not in str(error)


@pytest.mark.asyncio
async def test_headers_and_document_share_one_navigation_budget():
    page = Page()
    original_goto = page.goto

    async def delayed_headers(*args, **kwargs):
        await asyncio.sleep(0.65)
        return await original_goto(*args, **kwargs)

    async def stalled_document(*_args, **_kwargs):
        await asyncio.Event().wait()

    page.goto = delayed_headers
    page.wait_for_function = stalled_document
    started = asyncio.get_running_loop().time()
    with pytest.raises(BrowserNavigationUnavailable) as caught:
        await service()._goto(page, page.url)
    elapsed = asyncio.get_running_loop().time() - started
    assert 0.9 <= elapsed < 1.5  # not an additional full timeout after headers
    assert caught.value.diagnostic_code == "MOODLE_DOCUMENT_TIMEOUT"
    assert not page.content_read


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["interactive", "html_only", "server_rendered"])
async def test_real_browser_error_response_is_rejected_before_stalled_assets(mode):
    release = asyncio.Event()

    async def fixture(route):
        if route.request.url == BASE + "/fixture":
            await route.fulfill(status=503, content_type="text/html", body="""
                <html><head><link rel="stylesheet" href="/stalled.css">
                <script src="/stalled.js"></script></head><body>Error</body></html>
            """)
        else:
            await release.wait()
            await route.abort()

    instance = service()
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await instance._new_context(
                browser, html_only=mode == "html_only", server_rendered=mode == "server_rendered"
            )
            await context.route("**/*", fixture)
            page = await context.new_page()
            with pytest.raises(BrowserNavigationUnavailable) as caught:
                await asyncio.wait_for(instance._goto(page, BASE + "/fixture"), timeout=0.9)
            assert caught.value.diagnostic_code == "MOODLE_HTTP_ERROR"
        finally:
            release.set()
            await browser.close()
