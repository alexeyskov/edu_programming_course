from __future__ import annotations

import hashlib
import secrets

from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError

from app.core.config import Settings

_password_hash = PasswordHash.recommended()


def hash_admin_token(token: str) -> str:
    if len(token) < 20:
        raise ValueError("Administrator token must contain at least 20 characters")
    return _password_hash.hash(token)


def hash_teacher_token(token: str) -> str:
    if len(token) < 8:
        raise ValueError("Teacher token must contain at least 8 characters")
    return _password_hash.hash(token)


def verify_teacher_token_hash(token: str, encoded_hash: str) -> bool:
    if not token or not encoded_hash:
        return False
    try:
        return _password_hash.verify(token, encoded_hash)
    except (UnknownHashError, ValueError, TypeError):
        return False


def verify_admin_token(token: str, settings: Settings) -> bool:
    if not token:
        return False
    if settings.admin_token_hash:
        try:
            return _password_hash.verify(token, settings.admin_token_hash)
        except (UnknownHashError, ValueError, TypeError):
            return False
    development_token = settings.admin_token.get_secret_value()
    return bool(settings.debug and development_token) and secrets.compare_digest(
        token, development_token
    )


def generate_opaque_secret(byte_count: int = 32) -> str:
    return secrets.token_urlsafe(byte_count)


def hash_opaque_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def verify_opaque_secret(value: str, expected_hash: str) -> bool:
    return secrets.compare_digest(hash_opaque_secret(value), expected_hash)
