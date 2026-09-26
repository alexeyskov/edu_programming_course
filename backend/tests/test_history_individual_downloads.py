from __future__ import annotations

import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import httpx
import py7zr
import pytest

from app.services import moodle_history_download as downloads
from app.services.moodle_history import _source_omissions, historical_source_files
from app.services.submission_origin import historical_response_observations
from app.services.sync import _MoodleBrowserAdapter

BASE = "https://moodle.example.test"
URL = BASE + "/pluginfile.php/123/question/response_attachments/1/solution.zip"
STATE = {
    "cookies": [
        {
            "name": "MoodleSession",
            "value": "fixture",
            "domain": "moodle.example.test",
            "path": "/",
            "expires": -1,
        }
    ]
}


def item(url=URL):
    return {
        "module": "quiz",
        "cmid": 7,
        "attempt_id": "123",
        "responses": [
            {
                "response_id": "1",
                "artifacts": [
                    {
                        "external_id": "a" * 64,
                        "filename": "solution.zip",
                        "download_url": url,
                        "downloaded": False,
                        "omission_reason": "DEFERRED_DOWNLOAD",
                    }
                ],
            }
        ],
    }


def install_transport(monkeypatch, handler):
    original = httpx.AsyncClient

    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(downloads.httpx, "AsyncClient", client)


async def test_two_archives_over_100_mib_combined_keep_all_sources_without_bulk_base64(monkeypatch):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("main.cpp", "int main() { return 0; }\n")
        archive.writestr("build/output.bin", b"x" * (55 * 1024 * 1024))
    content = buffer.getvalue()
    calls = []

    def get(request):
        assert request.headers["cookie"] == "MoodleSession=fixture"
        assert request.headers["accept-encoding"] == "identity"
        calls.append(str(request.url))
        return httpx.Response(200, stream=httpx.ByteStream(content))

    install_transport(monkeypatch, get)
    value = item()
    value["responses"].append({**item()["responses"][0], "response_id": "2"})
    warnings = await downloads.hydrate_historical_attachments(
        [value], base_url=BASE, storage_state=STATE
    )
    assert warnings == [] and len(calls) == 2
    for response in value["responses"]:
        artifact = response["artifacts"][0]
        assert artifact["size_bytes"] > 55 * 1024 * 1024
        assert artifact["content_base64"] == "" and "download_url" not in artifact
        assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
        single = {"responses": [response]}
        assert historical_source_files(single)[0]["content"] == "int main() { return 0; }\n"
        assert not _source_omissions(single)
        observation = historical_response_observations(single)[0]
        assert observation["size"] == len(content)
        assert observation["sha256"] == artifact["sha256"]
    assert len(json.dumps(value)) < 5000  # Only digests and small source text remain.


@pytest.mark.parametrize(
    "target",
    [
        "https://evil.example/pluginfile.php/a",
        "http://moodle.example.test/pluginfile.php/a",
        BASE + "/admin/index.php",
        BASE + "/pluginfile.php/%2e%2e/admin/index.php",
        "https://[invalid",
    ],
)
async def test_redirects_never_send_session_to_an_unapproved_target(monkeypatch, target):
    calls = []

    def get(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": target})

    install_transport(monkeypatch, get)
    value = item()
    warnings = await downloads.hydrate_historical_attachments(
        [value], base_url=BASE, storage_state=STATE
    )
    assert calls == [URL] and warnings == ["ARTIFACT_OMITTED"]
    assert _source_omissions(value)[0]["reason"] == "DOWNLOAD_TARGET_REJECTED"


async def test_oversized_attachment_is_a_visible_file_limit_not_a_combined_budget(monkeypatch):
    install_transport(
        monkeypatch,
        lambda _: httpx.Response(
            200, headers={"content-length": "104857601"}, stream=httpx.ByteStream(b"")
        ),
    )
    value = item()
    assert await downloads.hydrate_historical_attachments(
        [value], base_url=BASE, storage_state=STATE
    ) == ["ARTIFACT_OMITTED", "FILE_TOO_LARGE"]
    assert _source_omissions(value)[0]["reason"] == "FILE_TOO_LARGE"


async def test_corrupt_archive_reports_materialization_failure_not_success(monkeypatch):
    install_transport(
        monkeypatch, lambda _: httpx.Response(200, stream=httpx.ByteStream(b"not an archive"))
    )
    value = item()
    assert await downloads.hydrate_historical_attachments(
        [value], base_url=BASE, storage_state=STATE
    ) == ["ARCHIVE_SOURCE_OMITTED"]
    assert _source_omissions(value)[0]["kind"] == "ATTACHMENT_ARCHIVE"


async def test_7z_crc_failure_has_specific_diagnostic_and_never_imports_damaged_code(monkeypatch):
    buffer = io.BytesIO()
    with py7zr.SevenZipFile(buffer, mode="w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.writestr(b"int main() { return 0; }\n", "main.cpp")
    original = buffer.getvalue()
    corrupted = original.replace(b"return 0", b"return 1", 1)
    assert corrupted != original
    install_transport(
        monkeypatch, lambda _: httpx.Response(200, stream=httpx.ByteStream(corrupted))
    )
    value = item()
    value["responses"][0]["artifacts"][0]["filename"] = "solution.7z"
    assert await downloads.hydrate_historical_attachments(
        [value],
        base_url=BASE,
        storage_state=STATE,
    ) == ["ARCHIVE_SOURCE_OMITTED", "ARCHIVE_CHECKSUM_MISMATCH"]
    assert _source_omissions(value)[0]["reason"] == "ARCHIVE_CHECKSUM_MISMATCH"
    files = historical_source_files(value)
    assert len(files) == 1 and files[0]["path"] == "moodle-archive-import-error.txt"
    assert "ARCHIVE_CHECKSUM_MISMATCH" in files[0]["content"]
    assert "int main" not in files[0]["content"]


async def test_batch_deadline_does_not_leave_unprocessed_download_references(monkeypatch):
    monkeypatch.setattr(downloads, "BATCH_TIMEOUT_SECONDS", 0)
    install_transport(monkeypatch, lambda _: pytest.fail("no download after deadline"))
    value = item()
    assert await downloads.hydrate_historical_attachments(
        [value], base_url=BASE, storage_state=STATE
    ) == ["ARTIFACT_OMITTED", "DOWNLOAD_TIMEOUT"]
    assert "download_url" not in value["responses"][0]["artifacts"][0]


async def test_inline_compatibility_payload_is_not_changed_to_a_deferred_failure(monkeypatch):
    value = item()
    value["responses"][0]["artifacts"] = []
    value["responses"][0]["answer_text"] = "int main(){}"
    install_transport(monkeypatch, lambda _: pytest.fail("inline answer has no files"))
    assert (
        await downloads.hydrate_historical_attachments([value], base_url=BASE, storage_state=STATE)
        == []
    )
    assert historical_source_files(value)[0]["content"] == "int main(){}"


async def test_internal_extracted_files_and_digests_cannot_arrive_from_rpc(monkeypatch):
    value = item()
    artifact = value["responses"][0]["artifacts"][0]
    artifact.update(
        downloaded=False,
        omission_reason="FILE_TOO_LARGE",
        _source_files=[("injected.cpp", "must not be trusted")],
        _content_md5="0" * 32,
        _source_error="injected",
    )
    install_transport(monkeypatch, lambda _: pytest.fail("no deferred download"))
    await downloads.hydrate_historical_attachments([value], base_url=BASE, storage_state=STATE)
    assert not any(key.startswith("_") for key in artifact)
    assert "download_url" not in artifact
    assert historical_response_observations(value) == []


async def test_worker_adapter_requests_references_and_merges_download_warnings(monkeypatch):
    value = item()

    async def discover(**kwargs):
        assert kwargs["attachment_delivery"] == "reference"
        return SimpleNamespace(
            items=[value],
            storage_state=STATE,
            course_id="12",
            module="quiz",
            cmid=7,
            next_cursor=None,
            complete=True,
            warnings=["ACTIVITY_METADATA_INCOMPLETE"],
        )

    install_transport(monkeypatch, lambda _: httpx.Response(403))
    adapter = object.__new__(_MoodleBrowserAdapter)
    adapter.browser = SimpleNamespace(discover_historical_submissions=discover, base_url=BASE)
    adapter.history_artifact_max_bytes = 100 * 1024 * 1024
    result = await adapter.discover_historical_submissions({"module": "quiz", "cmid": 7})
    assert result.value["warnings"] == ["ACTIVITY_METADATA_INCOMPLETE", "ARTIFACT_OMITTED"]
    artifact = result.value["items"][0]["responses"][0]["artifacts"][0]
    assert artifact["omission_reason"] == "DOWNLOAD_HTTP_ERROR"
    assert "download_url" not in artifact


async def test_streamed_size_limit_is_enforced_without_content_length(monkeypatch):
    install_transport(monkeypatch, lambda _: httpx.Response(200, stream=httpx.ByteStream(b"x" * 9)))
    value = item()
    assert await downloads.hydrate_historical_attachments(
        [value],
        base_url=BASE,
        storage_state=STATE,
        maximum_bytes=8,
    ) == ["ARTIFACT_OMITTED", "FILE_TOO_LARGE"]
    assert not value["responses"][0]["artifacts"][0]["downloaded"]
