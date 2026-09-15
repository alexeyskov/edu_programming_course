from __future__ import annotations

import asyncio
import os

import pytest
from playwright.async_api import async_playwright

from moodle_browser.assets import PublicAssetCache, individual_amd_url
from moodle_browser.config import Settings
from moodle_browser.service import MoodleBrowserService

BASE = "https://edu.mmcs.sfedu.ru"
STYLE = BASE + "/theme/styles.php/classic/123_122/all"
PUBLIC_HEADERS = {
    "content-type": "text/css; charset=utf-8",
    "cache-control": "public, max-age=3600",
}


@pytest.mark.parametrize(
    "module",
    [
        "core/first.js",
        "core_form/changechecker.js",
        "core_question/question_engine.js",
        "core/local/repository.js",
    ],
)
def test_only_named_modules_use_moodles_small_response(module):
    assert individual_amd_url(f"{BASE}/lib/requirejs.php/123/{module}", base_url=BASE) == (
        f"{BASE}/lib/requirejs.php/-1/{module}"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://elsewhere.example/lib/requirejs.php/123/core/first.js",
        "http://edu.mmcs.sfedu.ru/lib/requirejs.php/123/core/first.js",
        "https://user:password@edu.mmcs.sfedu.ru/lib/requirejs.php/123/core/first.js",
        BASE + "/lib/requirejs.php/-1/core/first.js",
        BASE + "/lib/requirejs.php/0/core/first.js",
        BASE + "/lib/requirejs.php/99999999999/core/first.js",
        BASE + "/lib/requirejs.php/123/core/first-lazy.js",
        BASE + "/lib/requirejs.php/123/core/first.js?sesskey=private",
        BASE + "/lib/requirejs.php/123/core/first.js#fragment",
        BASE + "/lib/requirejs.php/123/core/../first.js",
        BASE + "/lib/requirejs.php/123/core/%2e%2e/first.js",
        BASE + "/lib/requirejs.php/123/core//first.js",
        BASE + "/pluginfile.php/123/student/answer.js",
        BASE + "/repository/repository_ajax.php?action=upload",
        BASE + "/mod/quiz/attempt.php?attempt=123",
    ],
)
def test_private_unknown_and_already_individual_resources_are_untouched(url):
    assert individual_amd_url(url, base_url=BASE) is None


@pytest.mark.asyncio
async def test_context_rewrites_only_public_script_requests_and_keeps_origin_guard():
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
        async def new_context(self, **_options):
            return Context()

    class Request:
        method = "GET"

        def __init__(self, url, resource_type):
            self.url, self.resource_type = url, resource_type

    class Route:
        continued = None
        aborted = False

        async def continue_(self, **kwargs):
            self.continued = kwargs

        async def abort(self, _reason):
            self.aborted = True

    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    context = await service._new_context(Browser())
    script = BASE + "/lib/requirejs.php/123/core/first.js"
    for resource_type in ("script", "document", "fetch", "xhr"):
        route = Route()
        await context.handler(route, Request(script, resource_type))
        assert route.continued == (
            {"url": BASE + "/lib/requirejs.php/-1/core/first.js"}
            if resource_type == "script"
            else {}
        )
        assert not route.aborted
    route = Route()
    await context.handler(
        route, Request(script.replace(BASE, "https://foreign.example"), "script")
    )
    assert route.aborted and route.continued is None


@pytest.mark.parametrize(
    "url",
    [
        BASE + "/mod/quiz/attempt.php?attempt=123",
        BASE + "/pluginfile.php/123/submission/main.cpp",
        BASE + "/repository/repository_ajax.php?action=upload",
        STYLE + "?sesskey=private",
        STYLE.replace(BASE, "https://foreign.example"),
        BASE + "/theme/styles.php/classic/-1/all",
        BASE + "/lib/javascript.php/99999999999/lib/form/filemanager.js",
    ],
)
def test_public_cache_rejects_personal_unversioned_and_foreign_urls(url):
    cache = PublicAssetCache(BASE)
    cache.put(url, status=200, headers=PUBLIC_HEADERS, body=b"test")
    assert cache.get(url, "stylesheet", "GET") is None
    assert cache._size == 0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"cache-control": "max-age=300"},
        {"cache-control": "public, no-store, max-age=300"},
        {"cache-control": 'public, private="X-User", max-age=300'},
        {"cache-control": "public, no-cache, max-age=300"},
        {"cache-control": "public, max-age=0"},
        {"cache-control": "public, max-age=bad"},
        {"vary": "Cookie"},
        {"vary": "Accept-Encoding, Authorization"},
        {"vary": "*"},
        {"set-cookie": "MoodleSession=private"},
        {"content-type": "text/html"},
    ],
)
def test_public_cache_requires_explicitly_public_independent_successful_response(headers):
    cache = PublicAssetCache(BASE)
    effective = {**PUBLIC_HEADERS, **headers} if headers else {}
    cache.put(STYLE, status=200, headers=effective, body=b"test")
    assert cache.get(STYLE, "stylesheet", "GET") is None


def test_public_cache_is_bounded_versioned_and_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("moodle_browser.assets.time.monotonic", lambda: now[0])
    cache = PublicAssetCache(BASE, max_bytes=8, max_asset_bytes=4, max_entries=2)
    urls = [STYLE.replace("123_122", str(rev)) for rev in (123, 124, 125)]
    for index, url in enumerate(urls):
        cache.put(url, status=200, headers=PUBLIC_HEADERS, body=str(index).encode() * 4)
    assert cache._size == 8
    assert cache.get(urls[0], "stylesheet", "GET") is None
    assert cache.get(urls[1], "stylesheet", "POST") is None
    assert cache.get(urls[1], "document", "GET") is None
    assert cache.get(urls[1], "stylesheet", "GET").body == b"1111"
    cache.put(urls[1], status=500, headers=PUBLIC_HEADERS, body=b"bad")
    cache.put(urls[1], status=200, headers=PUBLIC_HEADERS, body=b"oversize")
    assert cache.get(urls[1], "stylesheet", "GET").body == b"1111"
    now[0] += 301
    assert cache.get(urls[1], "stylesheet", "GET") is None
    assert cache.get(urls[2], "stylesheet", "GET") is None
    assert cache._size == 0


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
async def test_versioned_theme_is_downloaded_once_without_sharing_documents_or_cookies():
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    downloads = []
    documents = []

    async def fixture(route):
        request = route.request
        if request.url == STYLE:
            if service._public_asset_cache.get(STYLE, request.resource_type, request.method):
                await route.fallback()  # Exercise the production cache hit.
                return
            downloads.append(STYLE)
            await route.fulfill(headers=PUBLIC_HEADERS, body="body { color: rgb(1, 2, 3); }")
        elif request.url == BASE + "/fixture":
            documents.append(request.url)
            await route.fulfill(
                content_type="text/html",
                body=(
                    f'<html><head><link rel="stylesheet" href="{STYLE}"></head>'
                    '<body>Fixture</body></html>'
                ),
            )
        else:
            await route.abort()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            for _ in range(2):
                context = await service._new_context(browser, server_rendered=True)
                await context.route("**/*", fixture)
                page = await context.new_page()
                await service._goto(page, BASE + "/fixture")
                assert (
                    await page.evaluate("getComputedStyle(document.body).color") == "rgb(1, 2, 3)"
                )

                async def captured():
                    while service._public_asset_cache.get(STYLE, "stylesheet", "GET") is None:
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(captured(), timeout=1)
                await context.close()
            assert len(downloads) == 1
            assert len(documents) == 2
            assert list(service._public_asset_cache._entries) == [STYLE]
        finally:
            await browser.close()
