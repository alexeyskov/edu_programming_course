from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from .errors import (
    IntegrationProtocolError,
    IntegrationResponseTooLarge,
    IntegrationTimeout,
    IntegrationUnavailable,
)


def secret_value(value: object) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value or "")


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_hex(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


async def read_limited(chunks: AsyncIterator[bytes], limit: int) -> bytes:
    if limit <= 0:
        raise ValueError("limit must be positive")
    body = bytearray()
    async for chunk in chunks:
        if len(body) + len(chunk) > limit:
            raise IntegrationResponseTooLarge("External response exceeds the configured limit")
        body.extend(chunk)
    return bytes(body)


async def request_json_limited(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    timeout_seconds: float,
    response_limit: int,
    headers: Mapping[str, str] | None = None,
    content: bytes | None = None,
    data: Mapping[str, Any] | None = None,
) -> Any:
    try:
        async with client.stream(
            method,
            url,
            headers=headers,
            content=content,
            data=data,
            timeout=httpx.Timeout(timeout_seconds),
        ) as response:
            response.raise_for_status()
            raw = await read_limited(response.aiter_bytes(), response_limit)
    except IntegrationResponseTooLarge:
        raise
    except httpx.TimeoutException as exc:
        raise IntegrationTimeout() from exc
    except httpx.HTTPStatusError as exc:
        # Keep the status useful for diagnostics while excluding the request URL,
        # response body, headers and httpx's original (URL-bearing) message.
        raise IntegrationUnavailable(
            f"External HTTP request returned status {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        # Concrete httpx exception names are safe and distinguish DNS/connect/
        # protocol failures without copying a potentially secret-bearing message.
        raise IntegrationUnavailable(f"External HTTP request failed: {type(exc).__name__}") from exc

    def reject_non_finite(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        return json.loads(raw.decode("utf-8"), parse_constant=reject_non_finite)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise IntegrationProtocolError("External service returned invalid JSON") from exc
