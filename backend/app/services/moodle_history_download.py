"""Stream Moodle attachments individually; retain only bounded source text."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import time
import zlib
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit

import httpx

from app.services.common import canonical_hash
from app.services.moodle_history import (
    _MAX_ARCHIVE_BYTES,
    _SOURCE_SUFFIXES,
    _archive_sources,
    _decode_text,
    _quiz_response_revision_projection,
    _safe_filename,
    _seven_zip_sources,
)

FILE_TIMEOUT_SECONDS = 60.0
BATCH_TIMEOUT_SECONDS = 140.0
_LOGGER = logging.getLogger(__name__)


class _DownloadOmitted(Exception):
    def __init__(self, reason: str):
        self.reason = reason


def _safe_download_url(url: str, base_url: str) -> bool:
    try:
        target, origin = urlsplit(url), urlsplit(base_url)
    except ValueError:
        return False
    path = unquote(target.path)
    return (
        target.scheme == origin.scheme == "https"
        and target.netloc == origin.netloc
        and target.username is None
        and target.password is None
        and not target.fragment
        and len(url) <= 2048
        and path.startswith("/pluginfile.php/")
        and not any(part in {".", ".."} for part in path.split("/"))
        and not any(char in path for char in "\0\\\r\n")
    )


def _prepare_sources(content: bytes, filename: str) -> tuple[list[tuple[str, str]], str]:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".zip":
        try:
            sources = _archive_sources(content, set())
        except (NotImplementedError, EOFError, zlib.error):
            return [], "ARCHIVE_INVALID_OR_UNSUPPORTED"
        return sources, "" if sources else "ARCHIVE_HAS_NO_READABLE_SOURCE_FILES"
    if suffix == ".7z":
        sources, failure = _seven_zip_sources(content, set())
        return sources, failure or ""
    if suffix not in _SOURCE_SUFFIXES:
        return [], ""  # Non-source attachments are not editor inputs.
    decoded = _decode_text(content)
    return (
        ([(filename, decoded)], "")
        if decoded is not None
        else ([], "SOURCE_UNAVAILABLE_OR_EXCEEDS_LOCAL_LIMIT")
    )


async def _download_one(
    client: httpx.AsyncClient,
    artifact: dict[str, Any],
    *,
    base_url: str,
    maximum_bytes: int,
    timeout_seconds: float,
) -> None:
    url = str(artifact.pop("download_url", ""))
    artifact.pop("_source_files", None)
    artifact.pop("_content_md5", None)
    artifact.pop("_source_error", None)
    try:
        async with asyncio.timeout(timeout_seconds):
            for _redirect in range(5):
                if not _safe_download_url(url, base_url):
                    raise _DownloadOmitted("DOWNLOAD_TARGET_REJECTED")
                async with client.stream(
                    "GET", url, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        try:
                            url = urljoin(url, response.headers.get("location", ""))
                        except ValueError as exc:
                            raise _DownloadOmitted("DOWNLOAD_TARGET_REJECTED") from exc
                        continue
                    if response.status_code != 200:
                        raise _DownloadOmitted("DOWNLOAD_HTTP_ERROR")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise _DownloadOmitted("DOWNLOAD_ENCODING_UNSUPPORTED")
                    length = response.headers.get("content-length", "")
                    declared = int(length) if length.isdigit() else None
                    if declared is not None and declared > maximum_bytes:
                        raise _DownloadOmitted("FILE_TOO_LARGE")
                    digest = hashlib.sha256()
                    md5 = hashlib.md5(usedforsecurity=False)
                    size = 0
                    with io.BytesIO() as buffer:
                        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
                            size += len(chunk)
                            if size > maximum_bytes:
                                raise _DownloadOmitted("FILE_TOO_LARGE")
                            buffer.write(chunk)
                            digest.update(chunk)
                            md5.update(chunk)
                        if declared is not None and size != declared:
                            raise _DownloadOmitted("DOWNLOAD_INCOMPLETE")
                        # Parsing is bounded independently from downloads and
                        # runs outside the worker's network/lease event loop.
                        sources, failure = await asyncio.to_thread(
                            _prepare_sources,
                            buffer.getvalue(),
                            _safe_filename(artifact.get("filename"), "moodle-file"),
                        )
                    artifact.update(
                        downloaded=True,
                        omission_reason="",
                        size_bytes=size,
                        sha256=digest.hexdigest(),
                        content_base64="",
                        mime_type=response.headers.get("content-type", "")[:255],
                        _content_md5=md5.hexdigest(),
                        _source_files=sources,
                        _source_error=failure,
                    )
                    return
            raise _DownloadOmitted("DOWNLOAD_TARGET_REJECTED")
    except (TimeoutError, httpx.TimeoutException):
        reason = "DOWNLOAD_TIMEOUT"
    except httpx.HTTPError:
        reason = "DOWNLOAD_FAILED"
    except _DownloadOmitted as exc:
        reason = exc.reason
    artifact.update(downloaded=False, omission_reason=reason, content_base64="", sha256="")


async def hydrate_historical_attachments(
    items: list[dict[str, Any]],
    *,
    base_url: str,
    storage_state: dict[str, Any],
    maximum_bytes: int = _MAX_ARCHIVE_BYTES,
) -> list[str]:
    """Read-only Moodle GETs. No archive bytes/URLs survive into the DB payload."""
    if not 1 <= maximum_bytes <= _MAX_ARCHIVE_BYTES:
        raise ValueError("invalid historical attachment limit")
    cookies = httpx.Cookies()
    for cookie in storage_state.get("cookies", []):
        expires = cookie.get("expires", -1)
        if expires > 0 and expires <= time.time():
            continue
        cookies.set(
            cookie["name"], cookie["value"], domain=cookie["domain"], path=cookie.get("path", "/")
        )
    deadline = asyncio.get_running_loop().time() + BATCH_TIMEOUT_SECONDS
    warnings: list[str] = []
    async with httpx.AsyncClient(
        cookies=cookies,
        trust_env=False,
        follow_redirects=False,
        timeout=httpx.Timeout(15.0, connect=10.0),
    ) as client:
        for item in items:
            for response in item.get("responses", []):
                for artifact in response.get("artifacts", []):
                    # Only this worker may provide verified source text and
                    # hashes. Never trust internal fields received over RPC.
                    for key in list(artifact):
                        if key.startswith("_"):
                            artifact.pop(key)
                    if artifact.get("omission_reason") != "DEFERRED_DOWNLOAD":
                        artifact.pop("download_url", None)
                        continue
                    started = asyncio.get_running_loop().time()
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        artifact.pop("download_url", None)
                        artifact.update(downloaded=False, omission_reason="DOWNLOAD_TIMEOUT")
                    else:
                        await _download_one(
                            client,
                            artifact,
                            base_url=base_url,
                            maximum_bytes=maximum_bytes,
                            timeout_seconds=min(FILE_TIMEOUT_SECONDS, remaining),
                        )
                    if not artifact.get("downloaded"):
                        warnings.append("ARTIFACT_OMITTED")
                        reason = artifact.get("omission_reason")
                        if reason in {"FILE_TOO_LARGE", "DOWNLOAD_TIMEOUT"}:
                            warnings.append(str(reason))
                    elif artifact.get("_source_error"):
                        warnings.append("ARCHIVE_SOURCE_OMITTED")
                        if artifact["_source_error"] == "ARCHIVE_CHECKSUM_MISMATCH":
                            warnings.append("ARCHIVE_CHECKSUM_MISMATCH")
                    _LOGGER.info(
                        "Moodle history attachment attempt=%s response=%s "
                        "downloaded=%s bytes=%s reason=%s elapsed_ms=%s",
                        item.get("attempt_id"),
                        response.get("response_id"),
                        artifact.get("downloaded"),
                        artifact.get("size_bytes", 0),
                        artifact.get("_source_error") or artifact.get("omission_reason") or "OK",
                        int((asyncio.get_running_loop().time() - started) * 1000),
                    )
            # The connector supplied metadata-only revision. Replace it with
            # verified file digests so edits in an archive are still detected.
            projected = {key: value for key, value in item.items() if key != "external_revision"}
            projected["responses"] = [
                _quiz_response_revision_projection(response)
                for response in item.get("responses", [])
            ]
            item["external_revision"] = await asyncio.to_thread(canonical_hash, projected)
    return warnings
