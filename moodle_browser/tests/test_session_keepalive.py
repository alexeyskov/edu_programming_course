from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import BASE_URL, auth_headers, encoded, storage_state

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState, SessionKeepaliveRequest
from moodle_browser.service import (
    BrowserUnavailable,
    MoodleBrowserService,
    MoodleProtocolError,
    MoodleSessionExpired,
)

PROFILE = '<a href="/login/logout.php">Logout</a><div class="usermenu">' \
          '<a href="/user/profile.php?id=42"><span class="usertext">Teacher</span></a></div>'


def body():
    return {"schema_version": "1.0", "base_url": BASE_URL,
            "actor_external_subject": "42", "storage_state": storage_state()}


def test_keepalive_is_signed_and_binds_a_single_user(client):
    payload = encoded(body())
    assert client.post("/internal/v1/moodle/session/keepalive", content=payload).status_code == 401
    response = client.post(
        "/internal/v1/moodle/session/keepalive", content=payload, headers=auth_headers(payload),
    )
    assert response.status_code == 200
    assert response.json()["external_subject"] == "42"


@pytest.mark.asyncio
@pytest.mark.parametrize("profile,expected", [
    (PROFILE, None),
    (PROFILE.replace("id=42", "id=43"), MoodleProtocolError),
    ('<form action="/login/index.php"><input name="password"></form>', MoodleSessionExpired),
    ("<h1>Temporarily unavailable</h1>", BrowserUnavailable),
])
async def test_keepalive_only_reads_current_profile_and_proves_identity(
    monkeypatch, profile, expected,
):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    visits = []
    closed = []

    @asynccontextmanager
    async def operation(**kwargs):
        assert kwargs == {"foreground": False}
        yield object()

    async def close():
        closed.append(True)

    async def new_page():
        return SimpleNamespace(url="")

    context = SimpleNamespace(new_page=new_page, close=close)

    async def new_context(_browser, **kwargs):
        assert kwargs["html_only"] is True
        return context

    async def goto(page, url):
        visits.append(url)
        page.url = url
        return profile

    async def state(_context):
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_course_discovery_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_state", state)
    if expected:
        with pytest.raises(expected):
            await service.keepalive(SessionKeepaliveRequest.model_validate(body()))
    else:
        result = await service.keepalive(SessionKeepaliveRequest.model_validate(body()))
        assert result.external_subject == "42"
    assert visits == [BASE_URL + "/user/profile.php"]
    assert closed == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_html,error", [
    (PROFILE, None),
    ('<form action="/login/index.php"><input name="username"></form>', MoodleSessionExpired),
    ("<h1>Temporary error</h1>", BrowserUnavailable),
])
async def test_standalone_assignment_detail_confirms_session_on_ordinary_page(
    monkeypatch, probe_html, error,
):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    detail_url = BASE_URL + "/mod/assign/view.php?id=777&action=grader&userid=77"
    detail_html = (Path(__file__).parent / "fixtures/assign_grader_historical.html").read_text()
    detail_page = SimpleNamespace(url=detail_url)
    visits, closed = [], []

    async def close():
        closed.append(True)

    async def new_page():
        return SimpleNamespace(url="", close=close)

    context = SimpleNamespace(new_page=new_page)

    async def goto(page, url):
        visits.append(url)
        page.url = url
        return detail_html if page is detail_page else probe_html

    async def state(_context):
        return BrowserStorageState.model_validate(storage_state())

    async def inner_text(_page):
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_state", state)
    monkeypatch.setattr(service, "_historical_assignment_inner_text", inner_text)
    arguments = dict(module="assign", course_id="549", cmid=777,
                     item={"attempt_id": "user-77-attempt-0", "user_id": "77"},
                     detail_url=detail_url)
    if error:
        with pytest.raises(error):
            await service._historical_detail(detail_page, context, **arguments)
    else:
        detail, warning = await service._historical_detail(detail_page, context, **arguments)
        assert warning is None
        assert "int main()" in detail["responses"][0]["answer_text"]
    assert visits == [detail_url, BASE_URL + "/my/"]
    assert closed == [True]
