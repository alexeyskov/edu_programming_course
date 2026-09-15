"""Keep Moodle's RequireJS requests small without changing its configuration."""

from __future__ import annotations

import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from urllib.parse import urlsplit

_AMD_SCRIPT = re.compile(
    r"/lib/requirejs\.php/([1-9][0-9]{0,10})/"
    r"([a-z][a-z0-9_]*(?:/[A-Za-z0-9_-]+)+\.js)"
)
_VERSIONED_ASSET = re.compile(
    r"(?:/theme/styles\.php/[a-z][a-z0-9_]*/([1-9][0-9]{0,10}(?:_[1-9][0-9]{0,10})?)/(?:all|editor)"
    r"|/lib/javascript\.php/([1-9][0-9]{0,10})/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_.-]+\.js)"
)


def _public_asset_path(url: str, base_url: str) -> bool:
    parsed, origin = urlsplit(url), urlsplit(base_url)
    if (
        (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc)
        or parsed.query
        or parsed.fragment
    ):
        return False
    match = _VERSIONED_ASSET.fullmatch(parsed.path)
    if match is None:
        return False
    revision = match.group(1) or match.group(2)
    return all(int(value) < time.time() + 3600 for value in revision.split("_"))


@dataclass(frozen=True)
class PublicAsset:
    body: bytes
    content_type: str
    expires_at: float


class PublicAssetCache:
    """Bounded cache of versioned, explicitly public CSS/core JavaScript only.

    Playwright routing disables the browser HTTP cache. Preserve the equivalent
    safe subset so each question navigation does not re-download the same theme.
    Nothing is fetched separately: bodies come from completed browser responses.
    Documents, answers, AJAX, cookies and user-dependent resources never qualify.
    """

    def __init__(
        self,
        base_url: str,
        *,
        max_bytes: int = 16 * 1024 * 1024,
        max_asset_bytes: int = 2 * 1024 * 1024,
        max_entries: int = 128,
    ):
        self.base_url = base_url
        self.max_bytes = max_bytes
        self.max_asset_bytes = max_asset_bytes
        self.max_entries = max_entries
        self._entries: OrderedDict[str, PublicAsset] = OrderedDict()
        self._size = 0

    def candidate(self, url: str, resource_type: str, method: str) -> bool:
        return (
            method == "GET"
            and resource_type in {"script", "stylesheet"}
            and _public_asset_path(url, self.base_url)
        )

    def get(self, url: str, resource_type: str, method: str) -> PublicAsset | None:
        if not self.candidate(url, resource_type, method):
            return None
        entry = self._entries.get(url)
        if entry is not None:
            if entry.expires_at <= time.monotonic():
                self._size -= len(self._entries.pop(url).body)
                return None
            self._entries.move_to_end(url)
        return entry

    def put(self, url: str, *, status: int, headers: dict[str, str], body: bytes) -> None:
        ttl = self.response_ttl(url, status=status, headers=headers)
        if ttl is None or not body or len(body) > min(self.max_bytes, self.max_asset_bytes):
            return
        previous = self._entries.pop(url, None)
        if previous is not None:
            self._size -= len(previous.body)
        while self._entries and (
            self._size + len(body) > self.max_bytes or len(self._entries) >= self.max_entries
        ):
            _key, removed = self._entries.popitem(last=False)
            self._size -= len(removed.body)
        self._entries[url] = PublicAsset(body, headers["content-type"], time.monotonic() + ttl)
        self._size += len(body)

    def response_ttl(self, url: str, *, status: int, headers: dict[str, str]) -> int | None:
        if status != 200 or not _public_asset_path(url, self.base_url) or "set-cookie" in headers:
            return None
        directives = {item.strip().lower() for item in headers.get("cache-control", "").split(",")}
        if "public" not in directives or any(
            item.split("=", 1)[0] in {"private", "no-store", "no-cache"} for item in directives
        ):
            return None
        vary = {
            item.strip().lower() for item in headers.get("vary", "").split(",") if item.strip()
        }
        if vary - {"accept-encoding"}:
            return None
        mime = headers.get("content-type", "").split(";")[0].strip().lower()
        if mime not in {"text/css", "text/javascript", "application/javascript"}:
            return None
        ages = [
            item.removeprefix("max-age=") for item in directives if item.startswith("max-age=")
        ]
        if len(ages) != 1 or not re.fullmatch(r"[0-9]{1,10}", ages[0]) or int(ages[0]) <= 0:
            return None
        # Revision changes invalidate the URL; additionally bound freshness to
        # five minutes even if Moodle advertises a multi-month max-age.
        return min(int(ages[0]), 300)


def individual_amd_url(url: str, *, base_url: str) -> str | None:
    """Select Moodle's native per-module representation of an AMD script.

    With a production revision, requirejs.php returns ALL non-lazy AMD modules
    for every module URL. Concurrent initialization can download that same
    multi-megabyte bundle several times. Browser routing also disables its
    ordinary HTTP cache. Revision -1 is Moodle's supported per-file response;
    it still serves the installed, built module, with its named AMD definition.
    No script contents, configuration, user pages or session state are changed.
    Lazy modules already have a per-file response and stay intact.

    Contract: moodle/MOODLE_502_STABLE/public/lib/requirejs.php, production-mode
    bundle branch and the per-module fallback below it (also present in 4.x/5.0).
    """
    target = urlsplit(url)
    origin = urlsplit(base_url)
    if (
        (target.scheme, target.netloc) != (origin.scheme, origin.netloc)
        or target.query
        or target.fragment
    ):
        return None
    match = _AMD_SCRIPT.fullmatch(target.path)
    if match is None:
        return None
    revision, module = match.groups()
    if int(revision) >= time.time() + 3600 or "-lazy.js" in module:
        return None
    return f"{base_url}/lib/requirejs.php/-1/{module}"
