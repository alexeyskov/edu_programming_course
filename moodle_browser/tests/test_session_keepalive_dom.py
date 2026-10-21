"""Read-only session renewal against isolated Chromium routes, never Moodle."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest
from conftest import BASE_URL, storage_state
from playwright.async_api import async_playwright

from moodle_browser.config import Settings
from moodle_browser.models import SessionKeepaliveRequest
from moodle_browser.service import MoodleBrowserService

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium",
    ),
]


async def test_real_profile_get_renews_cookie_without_writes(monkeypatch):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
            requests = []

            @asynccontextmanager
            async def operation(**_kwargs):
                yield browser

            create_context = service._new_context

            async def new_context(*args, **kwargs):
                context = await create_context(*args, **kwargs)

                async def route(request_route):
                    request = request_route.request
                    requests.append((request.method, request.url))
                    assert request.method == "GET"
                    assert request.url == BASE_URL + "/user/profile.php"
                    assert "MoodleSession=opaque-session-value" in request.headers["cookie"]
                    await request_route.fulfill(
                        content_type="text/html",
                        headers={"Set-Cookie": "MoodleSession=renewed; Path=/; Secure; HttpOnly"},
                        body='<a href="/login/logout.php">Logout</a><div class="usermenu">'
                             '<a href="/user/profile.php?id=42">'
                             '<span class="usertext">Student</span></a></div>',
                    )

                await context.route("**/*", route)
                return context

            monkeypatch.setattr(service, "_course_discovery_operation", operation)
            monkeypatch.setattr(service, "_new_context", new_context)
            result = await service.keepalive(SessionKeepaliveRequest.model_validate({
                "schema_version": "1.0", "base_url": BASE_URL,
                "actor_external_subject": "42", "storage_state": storage_state(),
            }))
            assert result.external_subject == "42"
            assert result.storage_state.cookies[0].value == "renewed"
            assert requests == [("GET", BASE_URL + "/user/profile.php")]
        finally:
            await browser.close()
