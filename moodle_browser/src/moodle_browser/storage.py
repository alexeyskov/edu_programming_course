from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlsplit

from .models import BrowserStorageState


class InvalidStorageState(ValueError):
    pass


def _bounded_text(value: object, *, maximum: int, field: str) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise InvalidStorageState(f"browser storage {field} is invalid")
    return value


def sanitize_storage_state(
    value: BrowserStorageState | dict[str, Any],
    *,
    base_url: str,
    maximum_bytes: int,
    reject_foreign: bool,
) -> BrowserStorageState:
    """Bound and retain only state scoped to the configured exact origin."""

    raw = value.model_dump(mode="json") if isinstance(value, BrowserStorageState) else value
    if not isinstance(raw, dict) or set(raw) - {"cookies", "origins"}:
        raise InvalidStorageState("browser storage state has an invalid shape")
    cookies = raw.get("cookies", [])
    origins = raw.get("origins", [])
    if not isinstance(cookies, list) or len(cookies) > 256:
        raise InvalidStorageState("browser cookie collection is invalid")
    if not isinstance(origins, list) or len(origins) > 4:
        raise InvalidStorageState("browser origin collection is invalid")
    host = (urlsplit(base_url).hostname or "").lower()

    clean_cookies: list[dict[str, Any]] = []
    for cookie in cookies:
        if not isinstance(cookie, dict):
            raise InvalidStorageState("browser cookie is invalid")
        domain = _bounded_text(cookie.get("domain"), maximum=253, field="cookie domain")
        if domain.lstrip(".").lower() != host:
            if reject_foreign:
                raise InvalidStorageState("browser state contains a foreign cookie domain")
            continue
        name = _bounded_text(cookie.get("name"), maximum=256, field="cookie name")
        value_text = _bounded_text(cookie.get("value", ""), maximum=16_384, field="cookie value")
        if not name:
            raise InvalidStorageState("browser cookie name is empty")
        path = _bounded_text(cookie.get("path", "/"), maximum=2_048, field="cookie path")
        if not path.startswith("/") or "\r" in path or "\n" in path:
            raise InvalidStorageState("browser cookie path is invalid")
        expires = cookie.get("expires", -1)
        if isinstance(expires, bool) or not isinstance(expires, int | float):
            raise InvalidStorageState("browser cookie expiry is invalid")
        expires = float(expires)
        if not math.isfinite(expires) or expires < -1 or expires > 4_102_444_800:
            raise InvalidStorageState("browser cookie expiry is invalid")
        same_site = cookie.get("sameSite", "Lax")
        if same_site not in {"Strict", "Lax", "None"}:
            raise InvalidStorageState("browser cookie SameSite value is invalid")
        clean_cookies.append(
            {
                "name": name,
                "value": value_text,
                "domain": domain.lower(),
                "path": path,
                "expires": expires,
                "httpOnly": bool(cookie.get("httpOnly", False)),
                "secure": bool(cookie.get("secure", True)),
                "sameSite": same_site,
            }
        )

    clean_origins: list[dict[str, Any]] = []
    for origin_state in origins:
        if not isinstance(origin_state, dict):
            raise InvalidStorageState("browser origin state is invalid")
        origin = _bounded_text(origin_state.get("origin"), maximum=2_048, field="origin").rstrip(
            "/"
        )
        if origin != base_url:
            if reject_foreign:
                raise InvalidStorageState("browser state contains a foreign origin")
            continue
        entries = origin_state.get("localStorage", [])
        if not isinstance(entries, list) or len(entries) > 128:
            raise InvalidStorageState("browser localStorage collection is invalid")
        clean_entries: list[dict[str, str]] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) - {"name", "value"}:
                raise InvalidStorageState("browser localStorage entry is invalid")
            name = _bounded_text(entry.get("name"), maximum=1_024, field="localStorage name")
            entry_value = _bounded_text(
                entry.get("value", ""), maximum=32_768, field="localStorage value"
            )
            if not name:
                raise InvalidStorageState("browser localStorage name is empty")
            clean_entries.append({"name": name, "value": entry_value})
        clean_origins.append({"origin": base_url, "localStorage": clean_entries})

    result = BrowserStorageState.model_validate(
        {"cookies": clean_cookies, "origins": clean_origins}
    )
    encoded = json.dumps(
        result.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(encoded) > maximum_bytes:
        raise InvalidStorageState("browser storage state exceeds the configured limit")
    return result


def has_moodle_session(state: BrowserStorageState) -> bool:
    return any(cookie.name == "MoodleSession" and bool(cookie.value) for cookie in state.cookies)
