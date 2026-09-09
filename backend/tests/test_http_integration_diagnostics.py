from __future__ import annotations

import httpx
import pytest

from app.integrations._http import request_json_limited
from app.integrations.errors import IntegrationTimeout, IntegrationUnavailable

_SECRET_URL = "https://external.example.test/private?token=url-secret"


async def _request(client: httpx.AsyncClient) -> object:
    return await request_json_limited(
        client,
        "GET",
        _SECRET_URL,
        timeout_seconds=1,
        response_limit=1024,
        headers={"Authorization": "Bearer header-secret"},
    )


@pytest.mark.asyncio
async def test_http_status_diagnostic_contains_only_numeric_status() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"response-body-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationUnavailable) as caught:
            await _request(client)

    assert str(caught.value) == "External HTTP request returned status 503"
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert "external.example" not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_request_error_diagnostic_contains_only_safe_exception_name() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(
            "connection detail with response-body-secret and url-secret",
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationUnavailable) as caught:
            await _request(client)

    assert str(caught.value) == "External HTTP request failed: ConnectError"
    assert isinstance(caught.value.__cause__, httpx.ConnectError)
    assert "external.example" not in str(caught.value)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_timeout_remains_a_distinct_safe_error() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout with url-secret", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationTimeout) as caught:
            await _request(client)

    assert str(caught.value) == "External service timed out"
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)
    assert "secret" not in str(caught.value)
