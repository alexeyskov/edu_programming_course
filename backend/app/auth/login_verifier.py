from __future__ import annotations

from dataclasses import dataclass

from fastapi import Response

from app.core.config import Settings
from app.core.security import generate_opaque_secret, hash_opaque_secret, verify_opaque_secret


@dataclass(frozen=True, slots=True)
class LoginSecrets:
    state: str
    state_hash: str
    verifier: str
    verifier_hash: str


def create_login_secrets() -> LoginSecrets:
    state = generate_opaque_secret()
    verifier = generate_opaque_secret()
    return LoginSecrets(
        state=state,
        state_hash=hash_opaque_secret(state),
        verifier=verifier,
        verifier_hash=hash_opaque_secret(verifier),
    )


def verify_login_verifier(verifier: str | None, expected_hash: str) -> bool:
    return bool(verifier) and verify_opaque_secret(verifier or "", expected_hash)


def set_login_verifier_cookie(response: Response, verifier: str, settings: Settings) -> None:
    production = not settings.debug
    response.set_cookie(
        settings.login_verifier_cookie_name,
        verifier,
        max_age=settings.login_verifier_ttl_seconds,
        httponly=True,
        secure=production or settings.cookie_secure,
        samesite="none" if production else "lax",
        path=f"{settings.api_prefix}/auth/moodle/callback",
    )


def clear_login_verifier_cookie(response: Response, settings: Settings) -> None:
    production = not settings.debug
    response.delete_cookie(
        settings.login_verifier_cookie_name,
        httponly=True,
        secure=production or settings.cookie_secure,
        samesite="none" if production else "lax",
        path=f"{settings.api_prefix}/auth/moodle/callback",
    )
