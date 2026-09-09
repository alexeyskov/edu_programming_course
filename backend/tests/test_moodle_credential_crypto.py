from __future__ import annotations

import uuid

import pytest

from app.core.config import Settings
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    MAX_BROWSER_STATE_JSON_BYTES,
    CredentialDecryptionError,
    decrypt_moodle_browser_state,
    decrypt_moodle_credential,
    encrypt_moodle_browser_state,
    encrypt_moodle_credential,
)


def _settings(key: str = "moodle-encryption-key-with-at-least-thirty-two-characters") -> Settings:
    return Settings(
        debug=True,
        secret_key="app-secret-with-at-least-thirty-two-characters",
        moodle_credential_encryption_key=key,
    )


def test_moodle_credential_round_trip_is_bound_to_database_identity() -> None:
    connection_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encrypted = encrypt_moodle_credential(
        "0123456789abcdef0123456789abcdef",
        _settings(),
        connection_id=connection_id,
        principal_id=principal_id,
    )

    assert "0123456789abcdef" not in encrypted
    assert (
        decrypt_moodle_credential(
            encrypted,
            _settings(),
            connection_id=connection_id,
            principal_id=principal_id,
        )
        == "0123456789abcdef0123456789abcdef"
    )
    with pytest.raises(CredentialDecryptionError):
        decrypt_moodle_credential(
            encrypted,
            _settings(),
            connection_id=connection_id,
            principal_id=uuid.uuid4(),
        )


def test_moodle_credential_rejects_tampering_and_wrong_key() -> None:
    connection_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encrypted = encrypt_moodle_credential(
        "secret-token",
        _settings(),
        connection_id=connection_id,
        principal_id=principal_id,
    )
    replacement = "A" if encrypted[-2] != "A" else "B"
    tampered = encrypted[:-2] + replacement + encrypted[-1]

    for value, settings in (
        (tampered, _settings()),
        (encrypted, _settings("different-key-with-at-least-thirty-two-characters")),
    ):
        with pytest.raises(CredentialDecryptionError):
            decrypt_moodle_credential(
                value,
                settings,
                connection_id=connection_id,
                principal_id=principal_id,
            )


def test_moodle_browser_state_round_trip_is_json_and_identity_bound() -> None:
    connection_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    state = {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "secret-cookie",
                "domain": "edu.mmcs.sfedu.ru",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [
            {
                "origin": "https://edu.mmcs.sfedu.ru",
                "localStorage": [{"name": "locale", "value": "русский"}],
            }
        ],
    }

    encrypted = encrypt_moodle_browser_state(
        state,
        _settings(),
        connection_id=connection_id,
        principal_id=principal_id,
    )

    assert "MoodleSession" not in encrypted
    assert "secret-cookie" not in encrypted
    assert (
        decrypt_moodle_browser_state(
            encrypted,
            _settings(),
            connection_id=connection_id,
            principal_id=principal_id,
        )
        == state
    )
    with pytest.raises(CredentialDecryptionError):
        decrypt_moodle_browser_state(
            encrypted,
            _settings(),
            connection_id=connection_id,
            principal_id=uuid.uuid4(),
        )


def test_moodle_browser_state_rejects_invalid_or_oversized_json() -> None:
    connection_id = uuid.uuid4()
    principal_id = uuid.uuid4()

    with pytest.raises(ValueError, match="JSON object"):
        encrypt_moodle_browser_state(  # type: ignore[arg-type]
            [],
            _settings(),
            connection_id=connection_id,
            principal_id=principal_id,
        )
    with pytest.raises(ValueError, match="valid JSON"):
        encrypt_moodle_browser_state(
            {"cookies": {object()}},
            _settings(),
            connection_id=connection_id,
            principal_id=principal_id,
        )
    with pytest.raises(ValueError, match="storage limit"):
        encrypt_moodle_browser_state(
            {"value": "x" * MAX_BROWSER_STATE_JSON_BYTES},
            _settings(),
            connection_id=connection_id,
            principal_id=principal_id,
        )


def test_moodle_browser_state_rejects_encrypted_non_object_json() -> None:
    connection_id = uuid.uuid4()
    principal_id = uuid.uuid4()
    encrypted = encrypt_moodle_credential(
        "[]",
        _settings(),
        connection_id=connection_id,
        principal_id=principal_id,
        kind=BROWSER_STATE_CREDENTIAL_KIND,
    )

    with pytest.raises(CredentialDecryptionError, match="JSON object"):
        decrypt_moodle_browser_state(
            encrypted,
            _settings(),
            connection_id=connection_id,
            principal_id=principal_id,
        )
