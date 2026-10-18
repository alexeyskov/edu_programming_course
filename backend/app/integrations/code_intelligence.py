from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings
from app.integrations._http import canonical_json, request_json_limited, secret_value
from app.integrations.errors import IntegrationConfigurationError, IntegrationProtocolError


class CodeIntelligenceAdapter:
    """Bounded, signed access to the optional isolated clangd service."""

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))

    async def query(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        endpoint = self.settings.code_intelligence_url.rstrip("/")
        parsed = urlsplit(endpoint)
        secret = secret_value(self.settings.code_intelligence_shared_secret)
        if (
            not self.settings.code_intelligence_enabled
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or len(secret.encode("utf-8")) < 32
        ):
            raise IntegrationConfigurationError("Code intelligence is not configured")
        body = canonical_json(payload)
        if len(body) > self.settings.code_intelligence_request_body_max_bytes:
            raise IntegrationProtocolError("Code intelligence request exceeds the size limit")
        timestamp = str(int(self.clock()))
        nonce = self.nonce_factory()
        canonical = (
            f"{timestamp}\n{nonce}\nPOST\n/v1/query\n{hashlib.sha256(body).hexdigest()}"
        ).encode("ascii")
        signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        result = await request_json_limited(
            self.client,
            "POST",
            f"{endpoint}/v1/query",
            timeout_seconds=self.settings.code_intelligence_request_timeout_seconds,
            response_limit=self.settings.code_intelligence_response_body_max_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Code-Intelligence-Timestamp": timestamp,
                "X-Code-Intelligence-Nonce": nonce,
                "X-Code-Intelligence-Signature": f"v1={signature}",
            },
            content=body,
        )
        allowed_paths = {item["path"] for item in payload["files"]}
        if not isinstance(result, dict) or set(result) != {"result", "diagnostics"}:
            raise IntegrationProtocolError("Invalid code intelligence response")
        if not isinstance(result["diagnostics"], list):
            raise IntegrationProtocolError("Invalid code intelligence diagnostics")
        for group in result["diagnostics"]:
            if (
                not isinstance(group, dict)
                or group.get("path") not in allowed_paths
                or not isinstance(group.get("diagnostics"), list)
            ):
                raise IntegrationProtocolError("Invalid code intelligence diagnostic path")
        self._validate_locations(result, allowed_paths)
        return result

    @classmethod
    def _validate_locations(cls, value: Any, allowed_paths: set[str], depth: int = 0) -> None:
        if depth > 30:
            raise IntegrationProtocolError("Code intelligence response is too deeply nested")
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"uri", "targetUri"} and (
                    not isinstance(item, str) or item not in allowed_paths
                ):
                    raise IntegrationProtocolError("Code intelligence returned an external path")
                cls._validate_locations(item, allowed_paths, depth + 1)
        elif isinstance(value, list):
            for item in value:
                cls._validate_locations(item, allowed_paths, depth + 1)
