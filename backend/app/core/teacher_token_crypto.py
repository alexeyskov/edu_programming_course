from __future__ import annotations

import base64
import hashlib
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

_CONTEXT = b"sfedu-mmcs:teacher-access-token:v1\0"
_PREFIX = "aesgcm-v1:"
_KIND = "TeacherAccessToken"
_MAX_TOKEN_BYTES = 512
_MAX_ENCRYPTED_CHARS = len(_PREFIX) + 4 * ((_MAX_TOKEN_BYTES + 28 + 2) // 3)


class TeacherTokenDecryptionError(ValueError):
    """A recoverable teacher token cannot be opened with the server key."""


def _key(settings: Settings) -> bytes:
    material = settings.secret_key.get_secret_value()
    if len(material) < 32:
        raise ValueError("Application secret key must contain at least 32 characters")
    return hashlib.sha256(_CONTEXT + material.encode("utf-8")).digest()


def _associated_data(token_id: uuid.UUID) -> bytes:
    return f"v1\0{_KIND}\0{token_id}".encode("ascii")


def encrypt_teacher_token(value: str, settings: Settings, *, token_id: uuid.UUID) -> str:
    """Encrypt one teacher token with token-row-bound authenticated data."""

    if not value:
        raise ValueError("Teacher token must not be empty")
    encoded = value.encode("utf-8")
    if len(encoded) > _MAX_TOKEN_BYTES:
        raise ValueError("Teacher token exceeds the storage limit")
    nonce = os.urandom(12)
    encrypted = AESGCM(_key(settings)).encrypt(nonce, encoded, _associated_data(token_id))
    payload = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
    return f"{_PREFIX}{payload}"


def decrypt_teacher_token(value: str, settings: Settings, *, token_id: uuid.UUID) -> str:
    """Decrypt and authenticate one recoverable teacher token."""

    if len(value) > _MAX_ENCRYPTED_CHARS or not value.startswith(_PREFIX):
        raise TeacherTokenDecryptionError("Stored teacher token has an unsupported format")
    try:
        raw = base64.b64decode(
            value.removeprefix(_PREFIX).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        if len(raw) < 29:
            raise ValueError("encrypted value is too short")
        nonce, ciphertext = raw[:12], raw[12:]
        decrypted = AESGCM(_key(settings)).decrypt(
            nonce,
            ciphertext,
            _associated_data(token_id),
        )
        result = decrypted.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise TeacherTokenDecryptionError("Stored teacher token cannot be decrypted") from exc
    if not result or len(result.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise TeacherTokenDecryptionError("Stored teacher token is invalid")
    return result


__all__ = [
    "TeacherTokenDecryptionError",
    "decrypt_teacher_token",
    "encrypt_teacher_token",
]
