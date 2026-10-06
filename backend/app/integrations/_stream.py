"""Bounded SSE transport. Never log or propagate provider bodies/credentials."""

from __future__ import annotations

import asyncio
import codecs
import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from .errors import (
    IntegrationProtocolError,
    IntegrationResponseTooLarge,
    IntegrationTimeout,
    IntegrationUnavailable,
)


async def stream_json_events(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: Mapping[str, str],
    content: bytes,
    timeout_seconds: float,
    response_limit: int,
    event_limit: int = 1024 * 1024,
    idle_timeout_seconds: float = 60,
) -> AsyncIterator[dict[str, Any] | None]:
    try:
        async with (
            asyncio.timeout(timeout_seconds),
            client.stream(
                "POST",
                url,
                headers=headers,
                content=content,
                timeout=httpx.Timeout(
                    timeout_seconds, read=min(timeout_seconds, idle_timeout_seconds)
                ),
            ) as response,
        ):
            response.raise_for_status()
            if "text/event-stream" not in response.headers.get("content-type", "").lower():
                raise IntegrationProtocolError("AI provider did not return an event stream")
            decoder = codecs.getincrementaldecoder("utf-8")()
            pending = ""
            data: list[str] = []
            received = 0
            event_bytes = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > response_limit:
                    raise IntegrationResponseTooLarge("AI stream exceeds the configured limit")
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    event_bytes += len(line.encode("utf-8")) + 1
                    if event_bytes > event_limit:
                        raise IntegrationResponseTooLarge(
                            "AI stream event exceeds the configured limit"
                        )
                    line = line.removesuffix("\r")
                    if line.startswith("data:"):
                        data.append(line[5:].removeprefix(" "))
                    elif not line:
                        event_bytes = 0
                        if data:
                            raw = "\n".join(data)
                            data.clear()
                            if raw == "[DONE]":
                                yield None
                                return
                            value = json.loads(raw)
                            if not isinstance(value, dict):
                                raise IntegrationProtocolError("AI stream event is not an object")
                            yield value
                    # SSE comments, event names and IDs do not contain answer text.
                if event_bytes + len(pending.encode("utf-8")) > event_limit:
                    raise IntegrationResponseTooLarge(
                        "AI stream event exceeds the configured limit"
                    )
            decoder.decode(b"", final=True)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrationProtocolError("AI provider returned an invalid stream") from exc
    except (httpx.TimeoutException, TimeoutError) as exc:
        raise IntegrationTimeout() from exc
    except httpx.HTTPStatusError as exc:
        raise IntegrationUnavailable(
            f"External HTTP request returned status {exc.response.status_code}"
        ) from exc
    except httpx.RequestError as exc:
        raise IntegrationUnavailable(f"External HTTP request failed: {type(exc).__name__}") from exc
