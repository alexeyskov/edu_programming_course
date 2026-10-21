from __future__ import annotations

import pytest
from conftest import BASE_URL, auth_headers, encoded, storage_state

from moodle_browser.config import Settings
from moodle_browser.models import BrowserStorageState
from moodle_browser.parsers import has_login_markup
from moodle_browser.service import BrowserUnavailable, MoodleBrowserService, MoodleSessionExpired


@pytest.mark.parametrize(
    "markup",
    [
        '<form action="/login/index.php"><input name="username"></form>',
        '<form action="https://edu.mmcs.sfedu.ru/login/index.php?testsession=1">'
        '<input name="password" type="password"></form>',
    ],
)
def test_only_a_same_origin_login_form_proves_login_is_required(markup):
    assert has_login_markup(markup, BASE_URL)


@pytest.mark.parametrize(
    "markup",
    [
        '<h1>Service temporarily unavailable</h1>',
        '<a href="/login/index.php">Log in</a>',
        '<form action="/mod/quiz/report.php"><input name="username"></form>',
        '<form action="https://elsewhere.test/login/index.php"><input name="password"></form>',
        '<form action="/login/index.php"><input name="search"></form>',
        '<pre>&lt;form action="/login/index.php"&gt;&lt;input name="username"&gt;</pre>',
    ],
)
def test_a_menu_or_username_field_is_not_evidence_of_an_expired_login(markup):
    assert not has_login_markup(markup, BASE_URL)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "markup",
    [
        '<h1>Too many requests</h1>',
        '<h1>Service unavailable</h1>',
        '<main><form action="/mod/quiz/report.php"><input name="username"></form></main>',
        '<main>Standalone page without authenticated identity evidence</main>',
    ],
)
async def test_unknown_page_does_not_expire_existing_session(monkeypatch, markup):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    state = BrowserStorageState.model_validate(storage_state())

    async def current_state(_context):
        return state

    monkeypatch.setattr(service, "_state", current_state)
    with pytest.raises(BrowserUnavailable, match="did not confirm"):
        await service._require_authenticated_page(object(), markup)
    assert state.cookies[0].value == "opaque-session-value"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_cookie", [True, False])
async def test_real_login_failure_still_requires_reauthentication(monkeypatch, missing_cookie):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    state = BrowserStorageState.model_validate(
        {"cookies": [], "origins": []} if missing_cookie else storage_state()
    )

    async def current_state(_context):
        return state

    monkeypatch.setattr(service, "_state", current_state)
    markup = (
        '<h1>No cookie</h1>' if missing_cookie else
        '<form action="/login/index.php"><input name="username"><input name="password"></form>'
    )
    with pytest.raises(MoodleSessionExpired):
        await service._require_authenticated_page(object(), markup)


@pytest.mark.asyncio
async def test_authenticated_page_is_still_accepted(monkeypatch):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    async def current_state(_context):
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_state", current_state)
    await service._require_authenticated_page(object(), '<a href="/login/logout.php">Logout</a>')


def test_unknown_page_is_a_retryable_rpc_failure_not_invalid_credentials(client, monkeypatch):
    async def fail(_request):
        raise BrowserUnavailable("Moodle page did not confirm authenticated session")

    monkeypatch.setattr(client.app.state.moodle_browser_service, "login", fail)
    body = encoded({
        "schema_version": "1.0", "base_url": BASE_URL,
        "username": "teacher", "password": "not-returned", "allowed_course_ids": [],
    })
    response = client.post(
        "/internal/v1/moodle/login", content=body, headers=auth_headers(body)
    )
    assert response.status_code == 503
    assert response.json() == {"detail": "Moodle browser is unavailable"}
    assert "not-returned" not in response.text
