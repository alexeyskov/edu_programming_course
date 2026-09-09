from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_moodle_browser_default_request_limit_fits_four_mibibyte_artifact(
    monkeypatch,
) -> None:
    monkeypatch.delenv("MOODLE_BROWSER_REQUEST_BODY_MAX_BYTES", raising=False)
    settings = Settings(
        _env_file=None,
        debug=True,
        secret_key="test-secret-key-" + "x" * 32,
    )

    # Four MiB expands to roughly 5.34 MiB in base64.  Six MiB leaves room for
    # the signed envelope and the bounded Playwright storage state.
    assert settings.moodle_browser_request_body_max_bytes == 6 * 1024 * 1024


def test_moodle_browser_settings_are_parsed_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("MOODLE_BROWSER_SERVICE_URL", "HTTP://Moodle-Browser:8083/")
    monkeypatch.setenv("MOODLE_BROWSER_SHARED_SECRET", "s" * 32)
    monkeypatch.setenv("MOODLE_BROWSER_HTTP_TIMEOUT_SECONDS", "91.5")
    monkeypatch.setenv("MOODLE_BROWSER_REQUEST_BODY_MAX_BYTES", "1048576")
    monkeypatch.setenv("MOODLE_BROWSER_MAX_RESPONSE_BYTES", "4194304")
    monkeypatch.setenv("MOODLE_BROWSER_STORAGE_STATE_MAX_BYTES", "262144")

    settings = Settings(
        _env_file=None,
        debug=False,
        secret_key="production-secret-key-" + "x" * 32,
    )

    assert settings.moodle_browser_service_url == "http://moodle-browser:8083"
    assert settings.moodle_browser_shared_secret.get_secret_value() == "s" * 32
    assert settings.moodle_browser_http_timeout_seconds == 91.5
    assert settings.moodle_browser_request_body_max_bytes == 1_048_576
    assert settings.moodle_browser_max_response_bytes == 4_194_304
    assert settings.moodle_browser_storage_state_max_bytes == 262_144


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {"moodle_browser_service_url": "http://moodle-browser:8083"},
            "must be configured together",
        ),
        (
            {
                "moodle_browser_service_url": "http://moodle-browser:8083",
                "moodle_browser_shared_secret": "short",
            },
            "at least 32 bytes",
        ),
        (
            {
                "moodle_browser_service_url": "http://moodle-browser:8083/path",
                "moodle_browser_shared_secret": "s" * 32,
            },
            r"exact HTTP\(S\) origin",
        ),
        (
            {
                "moodle_browser_request_body_max_bytes": 16 * 1024,
                "moodle_browser_storage_state_max_bytes": 32 * 1024,
            },
            "must not exceed",
        ),
    ],
)
def test_moodle_browser_production_configuration_fails_closed(
    overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            _env_file=None,
            debug=False,
            secret_key="production-secret-key-" + "x" * 32,
            **overrides,
        )
