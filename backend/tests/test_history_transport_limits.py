from __future__ import annotations

import base64
import threading

import httpx
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.integrations import _http
from app.integrations.errors import IntegrationProtocolError, IntegrationResponseTooLarge
from app.integrations.moodle_browser import MoodleBrowserClient


def settings(**overrides):
    return Settings(
        _env_file=None,
        debug=True,
        secret_key="test-secret-key-" + "x" * 32,
        moodle_base_url="https://moodle.example.test",
        moodle_browser_service_url="http://moodle-browser:8083",
        moodle_browser_shared_secret="s" * 32,
        **overrides,
    )


async def test_history_response_over_old_four_mib_cap_does_not_relax_login_limit():
    encoded = base64.b64encode(b"x" * (4 * 1024 * 1024 + 1)).decode("ascii")

    async def handler(_request):
        return httpx.Response(200, json={"content_base64": encoded})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = MoodleBrowserClient(settings(), client)
        result = await connector._call("discover_historical_submissions", {})
        assert result["content_base64"] == encoded
        with pytest.raises(IntegrationResponseTooLarge):
            await connector._call("login", {})


async def test_configured_history_transport_cap_is_enforced():
    async def handler(_request):
        return httpx.Response(200, json={"content": "x" * (32 * 1024)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = MoodleBrowserClient(
            settings(moodle_browser_history_max_response_bytes=16 * 1024), client
        )
        with pytest.raises(IntegrationResponseTooLarge):
            await connector._call("discover_historical_submissions", {})


@pytest.mark.parametrize("limit", [0, 160 * 1024 * 1024 + 1])
def test_history_transport_limit_is_bounded(limit):
    with pytest.raises(ValidationError):
        settings(moodle_browser_history_max_response_bytes=limit)


async def test_history_json_parsing_is_offloaded_and_still_rejects_nonfinite(monkeypatch):
    loop_thread = threading.get_ident()
    parser_threads = []
    original_loads = _http.json.loads

    def loads(*args, **kwargs):
        parser_threads.append(threading.get_ident())
        return original_loads(*args, **kwargs)

    monkeypatch.setattr(_http.json, "loads", loads)

    async def handler(_request):
        return httpx.Response(200, content=b'{"grade": NaN}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        connector = MoodleBrowserClient(settings(), client)
        with pytest.raises(IntegrationProtocolError):
            await connector._call("discover_historical_submissions", {})
    assert parser_threads and all(thread != loop_thread for thread in parser_threads)
