from __future__ import annotations

import pytest

from moodle_browser.storage import InvalidStorageState, has_moodle_session, sanitize_storage_state

BASE_URL = "https://edu.mmcs.sfedu.ru"


def test_output_state_drops_foreign_origins_and_cookie_domains() -> None:
    state = sanitize_storage_state(
        {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": "opaque",
                    "domain": ".edu.mmcs.sfedu.ru",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                },
                {
                    "name": "foreign",
                    "value": "drop",
                    "domain": ".mmcs.sfedu.ru",
                    "path": "/",
                    "expires": -1,
                    "sameSite": "Lax",
                },
            ],
            "origins": [
                {"origin": BASE_URL, "localStorage": [{"name": "x", "value": "y"}]},
                {
                    "origin": "https://evil.example",
                    "localStorage": [{"name": "secret", "value": "drop"}],
                },
            ],
        },
        base_url=BASE_URL,
        maximum_bytes=262_144,
        reject_foreign=False,
    )
    assert has_moodle_session(state)
    assert [cookie.name for cookie in state.cookies] == ["MoodleSession"]
    assert [origin.origin for origin in state.origins] == [BASE_URL]


def test_input_state_rejects_foreign_scope_and_oversized_data() -> None:
    with pytest.raises(InvalidStorageState, match="foreign cookie"):
        sanitize_storage_state(
            {
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": "opaque",
                        "domain": "evil.example",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            base_url=BASE_URL,
            maximum_bytes=262_144,
            reject_foreign=True,
        )
    with pytest.raises(InvalidStorageState, match="exceeds"):
        sanitize_storage_state(
            {
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": "x" * 10_000,
                        "domain": "edu.mmcs.sfedu.ru",
                        "path": "/",
                    }
                ],
                "origins": [],
            },
            base_url=BASE_URL,
            maximum_bytes=100,
            reject_foreign=True,
        )
