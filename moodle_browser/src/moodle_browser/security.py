from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Mapping

NONCE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{16,128}$")


class AuthenticationError(ValueError):
    pass


class ReplayError(AuthenticationError):
    pass


def canonical_signature_payload(timestamp: str, nonce: str, body: bytes) -> bytes:
    digest = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{nonce}\n{digest}".encode("ascii")


def sign_body(secret: bytes, timestamp: str, nonce: str, body: bytes) -> str:
    signature = hmac.new(
        secret,
        canonical_signature_payload(timestamp, nonce, body),
        hashlib.sha256,
    ).hexdigest()
    return f"v1={signature}"


def signed_headers(
    secret: bytes,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    timestamp_text = str(int(time.time()) if timestamp is None else timestamp)
    nonce_text = nonce or secrets.token_hex(16)
    return {
        "X-Moodle-Timestamp": timestamp_text,
        "X-Moodle-Nonce": nonce_text,
        "X-Moodle-Signature": sign_body(secret, timestamp_text, nonce_text, body),
    }


class RequestAuthenticator:
    def __init__(
        self,
        secret: bytes,
        *,
        clock_skew_seconds: int,
        nonce_ttl_seconds: int,
        max_nonces: int = 10_000,
    ) -> None:
        self._secret = secret
        self._clock_skew = clock_skew_seconds
        self._nonce_ttl = nonce_ttl_seconds
        self._max_nonces = max_nonces
        self._nonces: dict[str, float] = {}
        self._lock = threading.Lock()

    def _consume_nonce(self, nonce: str, now: float) -> None:
        with self._lock:
            cutoff = now - self._nonce_ttl
            expired = [key for key, used_at in self._nonces.items() if used_at < cutoff]
            for key in expired:
                self._nonces.pop(key, None)
            if nonce in self._nonces:
                raise ReplayError("request nonce has already been used")
            if len(self._nonces) >= self._max_nonces:
                # Never evict a still-live nonce: eviction would make an old signed
                # request replayable inside the accepted timestamp window.
                raise AuthenticationError("Moodle browser nonce cache is full")
            self._nonces[nonce] = now

    def verify(
        self,
        headers: Mapping[str, str],
        body: bytes,
        *,
        now: float | None = None,
    ) -> None:
        timestamp = headers.get("x-moodle-timestamp")
        nonce = headers.get("x-moodle-nonce")
        signature = headers.get("x-moodle-signature")
        if timestamp is None or nonce is None or signature is None:
            raise AuthenticationError("missing Moodle browser authentication headers")
        if not NONCE_PATTERN.fullmatch(nonce):
            raise AuthenticationError("invalid Moodle browser nonce")
        try:
            timestamp_value = int(timestamp)
        except ValueError as exc:
            raise AuthenticationError("invalid Moodle browser timestamp") from exc
        current = time.time() if now is None else now
        if abs(current - timestamp_value) > self._clock_skew:
            raise AuthenticationError("Moodle browser timestamp is outside the accepted window")
        expected = sign_body(self._secret, timestamp, nonce, body)
        if not hmac.compare_digest(signature, expected):
            raise AuthenticationError("invalid Moodle browser signature")
        self._consume_nonce(nonce, current)
