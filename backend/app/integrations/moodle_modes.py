from __future__ import annotations

from typing import Literal

from app.models.identity import LMSConnection

MoodleAuthMode = Literal["PLUGINLESS", "BRIDGE"]
MoodlePluginlessTransport = Literal["MOBILE_TOKEN", "PLAYWRIGHT"]


def moodle_auth_mode(connection: LMSConnection) -> MoodleAuthMode:
    configured = str((connection.config or {}).get("auth_mode", "PLUGINLESS")).upper()
    return "BRIDGE" if configured == "BRIDGE" else "PLUGINLESS"


def moodle_login_mode(connection: LMSConnection) -> Literal["CREDENTIALS", "REDIRECT"]:
    return "REDIRECT" if moodle_auth_mode(connection) == "BRIDGE" else "CREDENTIALS"


def moodle_pluginless_transport(connection: LMSConnection) -> MoodlePluginlessTransport:
    """Resolve an explicitly configured password-login transport.

    Rows without a marker retain the original mobile-token meaning so the
    resolver never silently reinterprets persisted configuration.  The
    deployment migration materialises ``PLAYWRIGHT`` for existing pluginless
    Moodle rows, while new rows receive it from the bootstrap CLI.
    """

    configured = str((connection.config or {}).get("pluginless_transport", "")).upper()
    return "PLAYWRIGHT" if configured == "PLAYWRIGHT" else "MOBILE_TOKEN"


__all__ = [
    "MoodleAuthMode",
    "MoodlePluginlessTransport",
    "moodle_auth_mode",
    "moodle_login_mode",
    "moodle_pluginless_transport",
]
