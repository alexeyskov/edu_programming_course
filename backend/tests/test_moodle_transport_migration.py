from __future__ import annotations

import importlib

migration = importlib.import_module(
    "app.db.migrations.versions.20260825_0008_default_pluginless_playwright"
)


def test_migration_materialises_playwright_and_preserves_other_config() -> None:
    assert migration._playwright_config(
        "MOODLE",
        {"auth_mode": "PLUGINLESS", "course_prefix": "cpp"},
    ) == {
        "auth_mode": "PLUGINLESS",
        "course_prefix": "cpp",
        "pluginless_transport": "PLAYWRIGHT",
    }
    assert migration._playwright_config("MOODLE", {}) == {"pluginless_transport": "PLAYWRIGHT"}


def test_migration_does_not_override_explicit_or_unrelated_connections() -> None:
    assert (
        migration._playwright_config(
            "MOODLE",
            {"auth_mode": "PLUGINLESS", "pluginless_transport": "MOBILE_TOKEN"},
        )
        is None
    )
    assert (
        migration._playwright_config(
            "MOODLE",
            {"auth_mode": "PLUGINLESS", "pluginless_transport": "PLAYWRIGHT"},
        )
        is None
    )
    assert migration._playwright_config("MOODLE", {"auth_mode": "BRIDGE"}) is None
    assert migration._playwright_config("OTHER", {"auth_mode": "PLUGINLESS"}) is None
    assert migration._playwright_config("MOODLE", None) is None
