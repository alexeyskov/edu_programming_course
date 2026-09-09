from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings

from ._http import canonical_json, request_json_limited, secret_value, sha256_hex
from .errors import IntegrationConfigurationError, IntegrationProtocolError


@dataclass(frozen=True, slots=True)
class AuthorshipResult:
    manifest_hash: str
    probability: Decimal
    confidence: Decimal
    uncertainty: Decimal
    analyzer: str
    model: str
    calibration: dict[str, Any]
    features: dict[str, Any]
    warnings: list[str]
    response_hash: str


class AuthorshipTransport:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        endpoint: str | None = None,
        bearer_token: str | None = None,
        shared_secret: str | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.endpoint = (
            endpoint
            if endpoint is not None
            else str(getattr(settings, "authorship_analyzer_url", ""))
        )
        self.bearer_token = (
            bearer_token
            if bearer_token is not None
            else secret_value(getattr(settings, "authorship_analyzer_token", ""))
        )
        self.shared_secret = (
            shared_secret
            if shared_secret is not None
            else secret_value(getattr(settings, "authorship_analyzer_shared_secret", ""))
        )
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        self.timeout_seconds = float(getattr(settings, "authorship_analyzer_timeout_seconds", 30))
        self.export_limit = int(getattr(settings, "authorship_max_export_bytes", 16 * 1024 * 1024))
        self.response_limit = int(getattr(settings, "authorship_max_response_bytes", 1024 * 1024))

    async def analyze(
        self,
        payload: dict[str, Any],
        *,
        manifest_hash: str,
    ) -> AuthorshipResult:
        endpoint = self._validated_endpoint()
        body = canonical_json(payload)
        if len(body) > self.export_limit:
            raise IntegrationProtocolError("Authorship export exceeds the configured size limit")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.shared_secret:
            timestamp = str(int(self.clock()))
            nonce = self.nonce_factory()
            canonical = f"{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode("ascii")
            signature = hmac.new(
                self.shared_secret.encode("utf-8"), canonical, hashlib.sha256
            ).hexdigest()
            headers.update(
                {
                    "X-Authorship-Timestamp": timestamp,
                    "X-Authorship-Nonce": nonce,
                    "X-Authorship-Signature": f"v1={signature}",
                }
            )
        elif self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        else:
            raise IntegrationConfigurationError(
                "Authorship analyzer authentication is not configured"
            )
        result = await request_json_limited(
            self.client,
            "POST",
            endpoint,
            timeout_seconds=self.timeout_seconds,
            response_limit=self.response_limit,
            headers=headers,
            content=body,
        )
        return self.validate(result, manifest_hash=manifest_hash)

    def validate(self, value: Any, *, manifest_hash: str) -> AuthorshipResult:
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Authorship analyzer response must be an object")
        if value.get("manifest_hash") != manifest_hash:
            raise IntegrationProtocolError(
                "Authorship analyzer response refers to a different manifest"
            )
        analyzer = self._label(value.get("analyzer"), "analyzer")
        model = self._label(value.get("model"), "model")
        calibration = value.get("calibration")
        features = value.get("features")
        warnings = value.get("warnings")
        if not isinstance(calibration, dict):
            raise IntegrationProtocolError("Authorship calibration must be an object")
        calibration_version = calibration.get("version")
        if (
            not isinstance(calibration_version, str)
            or not calibration_version.strip()
            or len(calibration_version.strip()) > 200
        ):
            raise IntegrationProtocolError(
                "Authorship calibration.version must be a non-empty string up to 200 characters"
            )
        calibration = {**calibration, "version": calibration_version.strip()}
        if not isinstance(features, dict):
            raise IntegrationProtocolError("Authorship features must be an object")
        if (
            not isinstance(warnings, list)
            or len(warnings) > 100
            or any(not isinstance(item, str) or len(item) > 1000 for item in warnings)
        ):
            raise IntegrationProtocolError("Authorship warnings have an invalid shape")
        return AuthorshipResult(
            manifest_hash=manifest_hash,
            probability=self._unit_decimal(value.get("probability"), "probability"),
            confidence=self._unit_decimal(value.get("confidence"), "confidence"),
            uncertainty=self._unit_decimal(value.get("uncertainty"), "uncertainty"),
            analyzer=analyzer,
            model=model,
            calibration=calibration,
            features=features,
            warnings=warnings,
            response_hash=self._response_hash(value),
        )

    def _validated_endpoint(self) -> str:
        endpoint = self.endpoint.strip()
        try:
            parsed = urlsplit(endpoint)
        except ValueError as exc:
            raise IntegrationConfigurationError("Authorship analyzer URL is invalid") from exc
        allow_insecure = bool(getattr(self.settings, "authorship_allow_insecure_http", False))
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise IntegrationConfigurationError("Authorship analyzer URL is invalid")
        if parsed.scheme != "https" and not allow_insecure:
            raise IntegrationConfigurationError("Authorship analyzer URL must use HTTPS")
        return endpoint

    @staticmethod
    def _response_hash(value: dict[str, Any]) -> str:
        try:
            return sha256_hex(canonical_json(value))
        except (TypeError, ValueError) as exc:
            raise IntegrationProtocolError(
                "Authorship response contains unsupported JSON values"
            ) from exc

    @staticmethod
    def _label(value: Any, field: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 200:
            raise IntegrationProtocolError(
                f"Authorship {field} must be a non-empty string up to 200 characters"
            )
        return value.strip()

    @staticmethod
    def _unit_decimal(value: Any, field: str) -> Decimal:
        if isinstance(value, bool):
            raise IntegrationProtocolError(f"Authorship {field} must be between 0 and 1")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise IntegrationProtocolError(f"Authorship {field} must be between 0 and 1") from exc
        if not result.is_finite() or result < 0 or result > 1:
            raise IntegrationProtocolError(f"Authorship {field} must be between 0 and 1")
        return result.quantize(Decimal("0.000001"))
