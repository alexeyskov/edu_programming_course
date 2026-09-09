from __future__ import annotations

import base64
import hashlib
import json
import os
import uuid
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import Settings

_CONTEXT = b"sfedu-mmcs:moodle-credential:v1\0"
_PREFIX = "aesgcm-v1:"
BROWSER_STATE_CREDENTIAL_KIND = "BROWSER_STATE_V1"
# Playwright state is normally only a few KiB. The explicit ceiling prevents a
# corrupt database row from turning JSON decoding into an unbounded allocation.
MAX_BROWSER_STATE_JSON_BYTES = 1024 * 1024
_MAX_BROWSER_STATE_ENCRYPTED_CHARS = len(_PREFIX) + 4 * (
    (MAX_BROWSER_STATE_JSON_BYTES + 28 + 2) // 3
)


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


class CredentialDecryptionError(ValueError):
    """A stored credential cannot be opened with the configured server key."""


def _key(settings: Settings) -> bytes:
    configured = settings.moodle_credential_encryption_key.get_secret_value()
    material = configured or settings.secret_key.get_secret_value()
    if len(material) < 32:
        raise ValueError("Moodle credential encryption key must contain at least 32 characters")
    return hashlib.sha256(_CONTEXT + material.encode("utf-8")).digest()


def _associated_data(*, connection_id: uuid.UUID, principal_id: uuid.UUID, kind: str) -> bytes:
    return f"v1\0{connection_id}\0{principal_id}\0{kind}".encode()


def encrypt_moodle_credential(
    value: str,
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
    kind: str = "MOBILE_TOKEN",
) -> str:
    if not value:
        raise ValueError("Moodle credential must not be empty")
    nonce = os.urandom(12)
    encrypted = AESGCM(_key(settings)).encrypt(
        nonce,
        value.encode("utf-8"),
        _associated_data(connection_id=connection_id, principal_id=principal_id, kind=kind),
    )
    payload = base64.urlsafe_b64encode(nonce + encrypted).decode("ascii")
    return f"{_PREFIX}{payload}"


def decrypt_moodle_credential(
    value: str,
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
    kind: str = "MOBILE_TOKEN",
) -> str:
    if not value.startswith(_PREFIX):
        raise CredentialDecryptionError("Stored Moodle credential has an unsupported format")
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
            _associated_data(connection_id=connection_id, principal_id=principal_id, kind=kind),
        )
        result = decrypted.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError, UnicodeEncodeError, ValueError) as exc:
        raise CredentialDecryptionError("Stored Moodle credential cannot be decrypted") from exc
    if not result:
        raise CredentialDecryptionError("Stored Moodle credential is empty")
    return result


def encrypt_moodle_browser_state(
    state: dict[str, Any],
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> str:
    """Serialize and encrypt one bounded Playwright ``storage_state`` object."""

    if not isinstance(state, dict):
        raise ValueError("Moodle browser state must be a JSON object")
    try:
        serialized = json.dumps(
            state,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        serialized_bytes = serialized.encode("utf-8")
    except (RecursionError, TypeError, UnicodeEncodeError, ValueError) as exc:
        raise ValueError("Moodle browser state must contain valid JSON values") from exc
    if len(serialized_bytes) > MAX_BROWSER_STATE_JSON_BYTES:
        raise ValueError("Moodle browser state exceeds the storage limit")
    return encrypt_moodle_credential(
        serialized,
        settings,
        connection_id=connection_id,
        principal_id=principal_id,
        kind=BROWSER_STATE_CREDENTIAL_KIND,
    )


def decrypt_moodle_browser_state(
    value: str,
    settings: Settings,
    *,
    connection_id: uuid.UUID,
    principal_id: uuid.UUID,
) -> dict[str, Any]:
    """Decrypt a bounded Playwright ``storage_state`` object."""

    if len(value) > _MAX_BROWSER_STATE_ENCRYPTED_CHARS:
        raise CredentialDecryptionError("Stored Moodle browser state exceeds the storage limit")
    serialized = decrypt_moodle_credential(
        value,
        settings,
        connection_id=connection_id,
        principal_id=principal_id,
        kind=BROWSER_STATE_CREDENTIAL_KIND,
    )
    try:
        if len(serialized.encode("utf-8")) > MAX_BROWSER_STATE_JSON_BYTES:
            raise ValueError("decrypted value is too large")
        state = json.loads(serialized, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, UnicodeEncodeError, ValueError) as exc:
        raise CredentialDecryptionError("Stored Moodle browser state is not valid JSON") from exc
    if not isinstance(state, dict):
        raise CredentialDecryptionError("Stored Moodle browser state is not a JSON object")
    return state


__all__ = [
    "BROWSER_STATE_CREDENTIAL_KIND",
    "CredentialDecryptionError",
    "MAX_BROWSER_STATE_JSON_BYTES",
    "decrypt_moodle_browser_state",
    "decrypt_moodle_credential",
    "encrypt_moodle_browser_state",
    "encrypt_moodle_credential",
]
