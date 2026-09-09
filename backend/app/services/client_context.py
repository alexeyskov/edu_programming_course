from __future__ import annotations

import ipaddress
import re

from fastapi import Request

_BROWSER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Microsoft Edge", re.compile(r"\bEdg(?:A|iOS)?/([0-9.]+)", re.IGNORECASE)),
    ("Яндекс Браузер", re.compile(r"\bYaBrowser/([0-9.]+)", re.IGNORECASE)),
    ("Opera", re.compile(r"\b(?:OPR|Opera)/([0-9.]+)", re.IGNORECASE)),
    ("Firefox", re.compile(r"\b(?:Firefox|FxiOS)/([0-9.]+)", re.IGNORECASE)),
    ("Google Chrome", re.compile(r"\b(?:Chrome|CriOS)/([0-9.]+)", re.IGNORECASE)),
    ("Chromium", re.compile(r"\bChromium/([0-9.]+)", re.IGNORECASE)),
)

_WINDOWS_VERSIONS = {
    "10.0": "Windows 10/11",
    "6.3": "Windows 8.1",
    "6.2": "Windows 8",
    "6.1": "Windows 7",
}

_CLIENT_CONTEXT_FIELDS = {
    "ip_address": 64,
    "browser": 80,
    "browser_version": 32,
    "operating_system": 80,
    "device_type": 16,
}


def _bounded_text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def _client_ip(request: Request) -> str:
    """Return the address already normalized by Uvicorn's trusted-proxy layer.

    Reading X-Forwarded-For here would let a direct client forge the audit trail.
    Deployment config decides which proxy may rewrite request.client before this
    function runs.
    """

    host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).compressed
    except ValueError:
        return ""


def _browser(user_agent: str) -> tuple[str, str]:
    for name, pattern in _BROWSER_PATTERNS:
        match = pattern.search(user_agent)
        if match:
            return name, match.group(1)[:32]
    if "Safari/" in user_agent:
        match = re.search(r"\bVersion/([0-9.]+)", user_agent, re.IGNORECASE)
        return "Safari", match.group(1)[:32] if match else ""
    return "", ""


def _operating_system(user_agent: str) -> str:
    windows = re.search(r"\bWindows NT ([0-9.]+)", user_agent, re.IGNORECASE)
    if windows:
        version = windows.group(1)
        return _WINDOWS_VERSIONS.get(version, f"Windows NT {version}")[:80]
    android = re.search(r"\bAndroid ([0-9.]+)", user_agent, re.IGNORECASE)
    if android:
        return f"Android {android.group(1)}"[:80]
    ios = re.search(r"(?:CPU (?:iPhone )?OS|iPhone OS) ([0-9_]+)", user_agent, re.IGNORECASE)
    if ios:
        return f"iOS {ios.group(1).replace('_', '.')}"[:80]
    macos = re.search(r"\bMac OS X ([0-9_]+)", user_agent, re.IGNORECASE)
    if macos:
        return f"macOS {macos.group(1).replace('_', '.')}"[:80]
    chrome_os = re.search(r"\bCrOS [^;)]+ ([0-9.]+)", user_agent, re.IGNORECASE)
    if chrome_os:
        return f"ChromeOS {chrome_os.group(1)}"[:80]
    if re.search(r"\bLinux\b", user_agent, re.IGNORECASE):
        return "Linux"
    return ""


def _device_type(user_agent: str) -> str:
    lowered = user_agent.casefold()
    if re.search(r"bot|crawler|spider|slurp", lowered):
        return "BOT"
    if (
        "ipad" in lowered
        or "tablet" in lowered
        or ("android" in lowered and "mobile" not in lowered)
    ):
        return "TABLET"
    if any(token in lowered for token in ("mobile", "iphone", "ipod")):
        return "MOBILE"
    return "DESKTOP" if user_agent else ""


def normalize_client_context(value: object) -> dict[str, str]:
    """Keep only the bounded, documented audit fields from persisted JSON."""

    if not isinstance(value, dict):
        return {}
    normalized_context = {
        field: normalized
        for field, limit in _CLIENT_CONTEXT_FIELDS.items()
        if (normalized := _bounded_text(value.get(field), limit))
    }
    address = normalized_context.get("ip_address")
    if address:
        try:
            normalized_context["ip_address"] = ipaddress.ip_address(address).compressed
        except ValueError:
            normalized_context.pop("ip_address", None)
    return normalized_context


def client_context_from_request(request: Request) -> dict[str, str]:
    """Build a minimal client audit record without browser fingerprinting."""

    user_agent = _bounded_text(request.headers.get("user-agent", ""), 512)
    browser, browser_version = _browser(user_agent)
    return normalize_client_context(
        {
            "ip_address": _client_ip(request),
            "browser": browser,
            "browser_version": browser_version,
            "operating_system": _operating_system(user_agent),
            "device_type": _device_type(user_agent),
        }
    )


def client_context_for_hash(value: object) -> dict[str, str]:
    """Return a stable JSON object for inclusion in the edit-event hash chain."""

    return dict(normalize_client_context(value))


__all__ = [
    "client_context_for_hash",
    "client_context_from_request",
    "normalize_client_context",
]
