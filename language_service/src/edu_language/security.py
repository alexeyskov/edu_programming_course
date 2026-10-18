from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Mapping


def sign_body(secret: bytes, timestamp: str, nonce: str, body: bytes) -> str:
    payload = f"{timestamp}\n{nonce}\nPOST\n/v1/query\n{hashlib.sha256(body).hexdigest()}".encode(
        "ascii"
    )
    return "v1=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()


def signed_headers(
    secret: bytes,
    body: bytes,
    *,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    stamp = str(int(time.time()) if timestamp is None else timestamp)
    token = nonce or secrets.token_hex(16)
    return {
        "X-Code-Intelligence-Timestamp": stamp,
        "X-Code-Intelligence-Nonce": token,
        "X-Code-Intelligence-Signature": sign_body(secret, stamp, token, body),
    }


class RequestAuthenticator:
    def __init__(self, secret: bytes):
        self.secret = secret
        self.nonces: dict[str, float] = {}

    def verify(self, headers: Mapping[str, str], body: bytes) -> None:
        stamp = headers.get("x-code-intelligence-timestamp", "")
        nonce = headers.get("x-code-intelligence-nonce", "")
        signature = headers.get("x-code-intelligence-signature", "")
        now = time.time()
        if (
            not stamp.isascii()
            or not stamp.isdigit()
            or len(stamp) > 12
            or abs(now - int(stamp)) > 60
        ):
            raise ValueError("invalid authentication timestamp")
        if not re.fullmatch(r"[A-Za-z0-9._-]{16,128}", nonce):
            raise ValueError("invalid nonce")
        if not re.fullmatch(r"v1=[a-f0-9]{64}", signature):
            raise ValueError("invalid authentication signature")
        if not hmac.compare_digest(
            signature, sign_body(self.secret, stamp, nonce, body)
        ):
            raise ValueError("invalid authentication signature")
        self.nonces = {
            key: used for key, used in self.nonces.items() if now - used < 300
        }
        if nonce in self.nonces or len(self.nonces) >= 100000:
            raise ValueError("replayed request or authentication capacity exceeded")
        self.nonces[nonce] = now
