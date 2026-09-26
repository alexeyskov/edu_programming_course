from __future__ import annotations

import asyncio
import base64
import hashlib
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from moodle_browser.config import Settings
from moodle_browser.models import HistoricalArtifact, QuizArtifact
from moodle_browser.service import (
    _HISTORICAL_CONTENT_BUDGET_BYTES,
    _HISTORICAL_TEXT_BUDGET_BYTES,
    MoodleBrowserService,
)

BASE_URL = "https://edu.mmcs.sfedu.ru"
FILE_URL = f"{BASE_URL}/pluginfile.php/123/question/response_attachments/1/answer.zip"
MAX_BYTES = 100 * 1024 * 1024
LINK = {"external_id": "a" * 64, "filename": "answer.zip", "url": FILE_URL}


class ChunkStream(httpx.AsyncByteStream):
    def __init__(self, size: int, *, delay: float = 0):
        self.size = size
        self.delay = delay
        self.bytes_yielded = 0
        self.closed = False

    async def __aiter__(self):
        while self.bytes_yielded < self.size:
            if self.delay:
                await asyncio.sleep(self.delay)
            chunk = b"x" * min(64 * 1024, self.size - self.bytes_yielded)
            self.bytes_yielded += len(chunk)
            yield chunk

    async def aclose(self):
        self.closed = True


class CookieContext:
    def __init__(self):
        self.urls = []

    async def cookies(self, url):
        self.urls.append(url)
        return [{"name": "MoodleSession", "value": "scoped-test-session"}]


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("moodle_browser.service.httpx.AsyncClient", client)


@pytest.mark.asyncio
async def test_full_100_mib_archive_fits_history_budget_and_validates(monkeypatch):
    stream = ChunkStream(MAX_BYTES)
    install_transport(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    available = (_HISTORICAL_CONTENT_BUDGET_BYTES - _HISTORICAL_TEXT_BUDGET_BYTES) // 4 * 3
    assert available >= MAX_BYTES
    artifact, consumed = await service._historical_artifact(
        SimpleNamespace(context=CookieContext()), LINK, maximum_bytes=available
    )
    assert artifact["downloaded"] is True
    assert artifact["size_bytes"] == MAX_BYTES
    assert consumed == ((MAX_BYTES + 2) // 3) * 4
    assert stream.closed
    validated = HistoricalArtifact.model_validate(artifact)
    assert validated.sha256 == artifact["sha256"]
    # The same encoded archive remains forbidden in the student upload RPC.
    with pytest.raises(ValidationError):
        QuizArtifact(
            filename="submission.zip",
            content_base64=artifact["content_base64"],
            sha256=artifact["sha256"],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_stream_is_stopped_without_fallback(monkeypatch, declared):
    # A smaller configured limit exercises the same streaming boundary cheaply.
    limit = 2 * 1024 * 1024
    stream = ChunkStream(limit * 3)
    requests = []

    def download(request):
        requests.append(request)
        headers = {"content-length": str(stream.size)} if declared else {}
        return httpx.Response(200, headers=headers, stream=stream)

    install_transport(monkeypatch, download)
    service = MoodleBrowserService(
        Settings(shared_secret=b"x" * 32, history_artifact_max_bytes=limit)
    )
    artifact, consumed = await service._historical_artifact(
        SimpleNamespace(context=CookieContext()), LINK, maximum_bytes=limit
    )
    assert artifact["omission_reason"] == "FILE_TOO_LARGE"
    assert consumed == 0
    assert len(requests) == 1
    assert stream.bytes_yielded == (0 if declared else limit + 64 * 1024)
    assert stream.closed


@pytest.mark.asyncio
async def test_100_mib_plus_one_byte_aborts_stream_at_boundary(monkeypatch):
    stream = ChunkStream(MAX_BYTES + 1)
    install_transport(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, consumed = await service._historical_artifact(
        SimpleNamespace(context=CookieContext()), LINK, maximum_bytes=MAX_BYTES
    )
    assert artifact["downloaded"] is False
    assert artifact["omission_reason"] == "FILE_TOO_LARGE"
    assert artifact["content_base64"] == ""
    assert consumed == 0
    assert stream.bytes_yielded == MAX_BYTES + 1
    assert stream.closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers"), [(206, {}), (200, {"content-encoding": "gzip"})]
)
async def test_partial_or_unrequested_compressed_transport_is_not_an_archive(
    monkeypatch, status, headers
):
    stream = ChunkStream(1024)
    install_transport(
        monkeypatch, lambda _request: httpx.Response(status, headers=headers, stream=stream)
    )
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, _consumed = await service._historical_artifact(
        SimpleNamespace(context=CookieContext()), LINK, maximum_bytes=MAX_BYTES
    )
    assert artifact["omission_reason"] == "DOWNLOAD_FAILED"
    assert stream.bytes_yielded == 0
    assert stream.closed


@pytest.mark.asyncio
async def test_exhausted_aggregate_budget_is_not_a_per_file_size_failure(monkeypatch):
    stream = ChunkStream(1025)
    install_transport(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, consumed = await service._historical_artifact(
        SimpleNamespace(context=CookieContext()), LINK, maximum_bytes=1024
    )
    assert artifact["omission_reason"] == "RESPONSE_BUDGET"
    assert consumed == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "target",
    ["https://outside.example/pluginfile.php/private", f"{BASE_URL}/login/index.php"],
)
async def test_redirect_target_is_rejected_before_sending_cookies(monkeypatch, target):
    calls = []

    def download(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    install_transport(monkeypatch, download)
    context = CookieContext()
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, consumed = await service._historical_artifact(
        SimpleNamespace(context=context), LINK, maximum_bytes=MAX_BYTES
    )
    assert calls == [FILE_URL]
    assert context.urls == [FILE_URL]
    assert artifact["omission_reason"] == "DOWNLOAD_FAILED"
    assert consumed == 0


@pytest.mark.asyncio
async def test_safe_redirect_refreshes_url_scoped_cookies(monkeypatch):
    second_url = FILE_URL + "?forcedownload=1"

    def download(request):
        assert request.headers["cookie"] == "MoodleSession=scoped-test-session"
        if str(request.url) == FILE_URL:
            return httpx.Response(302, headers={"location": second_url})
        return httpx.Response(200, stream=httpx.ByteStream(b"archive"))

    install_transport(monkeypatch, download)
    context = CookieContext()
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, _consumed = await service._historical_artifact(
        SimpleNamespace(context=context), LINK, maximum_bytes=MAX_BYTES
    )
    assert context.urls == [FILE_URL, second_url]
    assert artifact["downloaded"] is True
    assert base64.b64decode(artifact["content_base64"]) == b"archive"


@pytest.mark.asyncio
async def test_download_wall_time_is_bounded(monkeypatch):
    stream = ChunkStream(1024, delay=0.1)
    install_transport(monkeypatch, lambda _request: httpx.Response(200, stream=stream))
    monkeypatch.setattr("moodle_browser.service._HISTORICAL_ARTIFACT_DOWNLOAD_SECONDS", 0.01)
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, _consumed = await service._historical_artifact(
        SimpleNamespace(context=CookieContext()), LINK, maximum_bytes=MAX_BYTES
    )
    assert artifact["omission_reason"] == "DOWNLOAD_FAILED"
    assert stream.closed


@pytest.mark.parametrize("encoded", ["eB==", "eA==AAAA", "eA=", "eA==\n", "!!!!"])
def test_historical_model_rejects_noncanonical_base64(encoded):
    with pytest.raises(ValidationError, match="canonical base64"):
        HistoricalArtifact(
            external_id="a" * 64,
            filename="answer.zip",
            size_bytes=1,
            content_base64=encoded,
            sha256=hashlib.sha256(b"x").hexdigest(),
        )


def test_historical_model_rejects_size_above_100_mib():
    with pytest.raises(ValidationError):
        HistoricalArtifact(
            external_id="a" * 64,
            filename="answer.zip",
            size_bytes=MAX_BYTES + 1,
            downloaded=False,
            omission_reason="FILE_TOO_LARGE",
        )
