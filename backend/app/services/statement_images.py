"""Bounded, read-only Moodle image retrieval; no browser cookies leave the server."""

from __future__ import annotations

import asyncio
import re
import struct
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

import httpx

from app.services.common import DomainError

MAX_IMAGE_BYTES = 2 * 1024 * 1024
_MAX_IMAGE_PIXELS = 16_000_000
_MAX_IMAGE_SIDE = 16_384
_COOKIE_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,256}$")
_MIME_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp"})
_RASTER_SUFFIX = re.compile(r"\.(?:png|gif|jpe?g|webp)$", re.IGNORECASE)
_TEX_RASTER = re.compile(r"^[A-Fa-f0-9]{16,128}\.(?:png|gif|jpe?g)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class StatementImage:
    content: bytes
    media_type: str


def safe_statement_image_url(url: str, base_url: str) -> bool:
    """Allow image-only Moodle routes at the administrator-configured HTTPS origin."""

    try:
        target, origin = urlsplit(url), urlsplit(base_url)
        if (
            len(url) > 4096
            or target.scheme != "https"
            or origin.scheme != "https"
            or not origin.hostname
            or target.hostname != origin.hostname
            or (target.port if target.port is not None else 443)
            != (origin.port if origin.port is not None else 443)
            or target.username is not None
            or target.password is not None
            or origin.username is not None
            or origin.password is not None
            or target.fragment
            or any(ord(char) < 32 or char == "\\" for char in url)
        ):
            return False
        # Decode twice to reject encoded traversal rather than entrusting it
        # to different URL-normalisation rules in an upstream web server.
        path = unquote(unquote(target.path))
        prefix = unquote(origin.path).rstrip("/")
        if any(part in {".", ".."} for part in path.split("/")) or any(
            ord(char) < 32 or char in "\\?#%" for char in path
        ):
            return False
        relative_path = path[len(prefix) :] if path.startswith(prefix + "/") else ""
        if relative_path.startswith("/filter/tex/pix.php/"):
            if _TEX_RASTER.fullmatch(relative_path.removeprefix("/filter/tex/pix.php/")) is None:
                return False
        elif relative_path.startswith(("/pluginfile.php/", "/file.php/")):
            if _RASTER_SUFFIX.search(relative_path) is None:
                return False
        else:
            return False
        # In particular never accept token/sesskey or arbitrary application
        # arguments, even if somebody put them in the source HTML.
        query = parse_qsl(target.query, keep_blank_values=True, max_num_fields=3)
        return len({key for key, _ in query}) == len(query) and all(
            (key == "forcedownload" and value in {"0", "1"})
            or (key in {"rev", "cache"} and re.fullmatch(r"[0-9]{1,20}", value) is not None)
            for key, value in query
        )
    except (TypeError, ValueError):
        return False


def _cookies(storage_state: dict[str, Any] | None, base_url: str) -> httpx.Cookies:
    cookies = httpx.Cookies()
    host = urlsplit(base_url).hostname or ""
    entries = (storage_state or {}).get("cookies", [])
    if not isinstance(entries, list):
        return cookies
    for cookie in entries[:256]:
        if not isinstance(cookie, dict):
            continue
        name, value = cookie.get("name", ""), cookie.get("value", "")
        domain, path = cookie.get("domain", ""), cookie.get("path", "/")
        expires = cookie.get("expires", -1)
        if (
            not isinstance(name, str)
            or _COOKIE_NAME.fullmatch(name) is None
            or not isinstance(value, str)
            or len(value) > 4096
            or any(ord(char) < 32 or char in "\r\n;" for char in value)
            or not isinstance(domain, str)
            or not domain
            or not (host == domain.lstrip(".") or host.endswith("." + domain.lstrip(".")))
            or not isinstance(path, str)
            or not path.startswith("/")
            or any(ord(char) < 32 for char in path)
            or not isinstance(expires, int | float)
            or (expires > 0 and expires <= time.time())
        ):
            continue
        cookies.set(name, value, domain=domain, path=path)
    return cookies


def _image_dimensions(content: bytes, media_type: str) -> tuple[int, int] | None:
    if media_type == "image/png" and content.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(content) >= 24 and content[12:16] == b"IHDR":
            return struct.unpack(">II", content[16:24])
    elif media_type == "image/gif" and content[:6] in {b"GIF87a", b"GIF89a"}:
        if len(content) >= 10:
            return struct.unpack("<HH", content[6:10])
    elif media_type == "image/webp" and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        if len(content) >= 30 and content[12:16] == b"VP8X":
            return (
                int.from_bytes(content[24:27], "little") + 1,
                int.from_bytes(content[27:30], "little") + 1,
            )
        if len(content) >= 30 and content[12:16] == b"VP8 " and content[23:26] == b"\x9d\x01\x2a":
            width, height = struct.unpack("<HH", content[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if len(content) >= 25 and content[12:16] == b"VP8L" and content[20] == 0x2F:
            bits = int.from_bytes(content[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    elif media_type == "image/jpeg" and content.startswith(b"\xff\xd8"):
        position = 2
        while position + 4 <= len(content):
            if content[position] != 0xFF:
                return None
            while position < len(content) and content[position] == 0xFF:
                position += 1
            if position + 3 > len(content):
                return None
            marker = content[position]
            position += 1
            length = int.from_bytes(content[position : position + 2], "big")
            if length < 2 or position + length > len(content):
                return None
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if length < 8:
                    return None
                height, width = struct.unpack(">HH", content[position + 3 : position + 7])
                return width, height
            position += length
    return None


def _image_valid(content: bytes, media_type: str) -> bool:
    dimensions = _image_dimensions(content, media_type)
    return (
        dimensions is not None
        and all(0 < side <= _MAX_IMAGE_SIDE for side in dimensions)
        and dimensions[0] * dimensions[1] <= _MAX_IMAGE_PIXELS
    )


async def fetch_statement_image(
    *,
    url: str,
    base_url: str,
    storage_state: dict[str, Any] | None = None,
) -> StatementImage:
    """GET one image, validating every redirect and bounding time/bytes/dimensions."""

    if not safe_statement_image_url(url, base_url):
        raise DomainError(404, "STATEMENT_IMAGE_NOT_FOUND", "Изображение условия недоступно")
    try:
        async with (
            asyncio.timeout(20),
            httpx.AsyncClient(
                cookies=_cookies(storage_state, base_url),
                trust_env=False,
                follow_redirects=False,
                timeout=httpx.Timeout(15, connect=10),
            ) as client,
        ):
            for _redirect in range(4):
                if not safe_statement_image_url(url, base_url):
                    break
                async with client.stream(
                    "GET",
                    url,
                    headers={
                        "Accept": "image/png,image/jpeg,image/gif,image/webp",
                        "Accept-Encoding": "identity",
                    },
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("location", ""))
                        continue
                    media_type = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    if (
                        response.status_code != 200
                        or media_type not in _MIME_TYPES
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                    ):
                        break
                    declared = response.headers.get("content-length", "")
                    if declared and (not declared.isdigit() or int(declared) > MAX_IMAGE_BYTES):
                        break
                    content = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                        content.extend(chunk)
                        if len(content) > MAX_IMAGE_BYTES:
                            break
                    else:
                        if not declared or len(content) == int(declared):
                            raster = bytes(content)
                            # Moodle's legacy TeX filter can return a GIF87a
                            # with a .png URL and an image/png header. Keep the
                            # same raster/dimension checks, and serve its real
                            # type with nosniff instead of rejecting the formula.
                            if (
                                media_type == "image/png"
                                and "/filter/tex/pix.php/" in urlsplit(url).path
                                and _image_valid(raster, "image/gif")
                            ):
                                media_type = "image/gif"
                            if _image_valid(raster, media_type):
                                return StatementImage(raster, media_type)
                    break
    except (TimeoutError, httpx.HTTPError, ValueError):
        pass
    # No upstream headers, URLs, login HTML, cookies or credentials enter the
    # response or error message, including on an unsuccessful request.
    raise DomainError(
        502, "STATEMENT_IMAGE_UNAVAILABLE", "Не удалось загрузить изображение условия"
    )
