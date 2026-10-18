from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from edu_language.app import create_app
from edu_language.config import Settings
from edu_language.models import Query, safe_path
from edu_language.security import RequestAuthenticator, signed_headers
from edu_language.service import _DROP, safe_result


SECRET = b"s" * 32


def manifest(**overrides):
    return {
        "workspace_id": "a" * 64,
        "build_profile": "cpp-gcc-c++20-single",
        "files": [{"path": "main.cpp", "content": "int main() {}"}],
        "method": "hover",
        "path": "main.cpp",
        "position": {"line": 0, "character": 4},
        **overrides,
    }


@pytest.mark.parametrize(
    "path",
    [
        "../main.cpp",
        "/main.cpp",
        "dir/../main.cpp",
        "main.cpp/",
        "x\\main.cpp",
        ".clangd",
        "compile_commands.json",
        "dir/.hidden/a.h",
        "a//main.cpp",
        "a\x00.cpp",
        "a\n.cpp",
    ],
)
def test_rejects_unsafe_paths(path):
    with pytest.raises(ValueError):
        safe_path(path)


def test_accepts_headers_and_sources():
    for path in ["main.cpp", "include/my header.hpp", "lib/test.c", "template.tpp"]:
        assert safe_path(path) == path


@pytest.mark.parametrize(
    "overrides",
    [
        {"workspace_id": "client-controlled"},
        {"build_profile": "arbitrary-flags"},
        {"path": "other.cpp"},
        {"position": {"line": 4, "character": 0}},
        {"position": {"line": 0, "character": 100}},
        {"position": None},
        {"files": [{"path": "main.cpp", "content": ""}] * 2},
        {"files": [{"path": "main.cpp", "content": "\x00"}]},
        {"context": {"command": "execute"}},
        {"context": {"tabSize": 99}},
    ],
)
def test_validates_query(overrides):
    with pytest.raises(ValidationError):
        Query(**manifest(**overrides))


def test_bounds_utf8_size_not_only_characters():
    with pytest.raises(ValidationError):
        Query(
            **manifest(files=[{"path": "main.cpp", "content": "я" * (1024 * 1024 + 1)}])
        )


def test_position_is_utf16():
    Query(
        **manifest(
            files=[{"path": "main.cpp", "content": "😀"}],
            position={"line": 0, "character": 2},
        )
    )


def test_hmac_and_replay():
    body = b"{}"
    headers = {
        key.lower(): value for key, value in signed_headers(SECRET, body).items()
    }
    auth = RequestAuthenticator(SECRET)
    auth.verify(headers, body)
    with pytest.raises(ValueError):
        auth.verify(headers, body)
    with pytest.raises(ValueError):
        RequestAuthenticator(SECRET).verify(headers, b"different")
    old = {
        key.lower(): value
        for key, value in signed_headers(
            SECRET, body, timestamp=int(time.time()) - 120
        ).items()
    }
    with pytest.raises(ValueError):
        RequestAuthenticator(SECRET).verify(old, body)
    with pytest.raises(ValueError):
        RequestAuthenticator(SECRET).verify(
            {**headers, "x-code-intelligence-signature": "ключ"}, body
        )


def test_location_sanitization():
    root = Path("/tmp/scope")
    location = {
        "uri": "file:///tmp/scope/main.cpp",
        "range": {"start": {"line": 0, "character": 0}},
    }
    assert safe_result(location, root, {"main.cpp"})["uri"] == "main.cpp"
    for uri in [
        "file:///tmp/other/main.cpp",
        "file:///etc/passwd",
        "https://external/main.cpp",
        "file://host/tmp/scope/main.cpp",
        "file:///tmp/scope/../private.cpp",
    ]:
        assert safe_result({"uri": uri}, root, {"main.cpp"}) is _DROP
    assert safe_result(
        {
            "command": {"command": "unsafe"},
            "label": "safe",
            "data": {"source": "external"},
        },
        root,
        set(),
    ) == {"label": "safe"}


def test_edit_text_is_never_truncated_or_rewritten():
    root = Path("/tmp/scope")
    value = 'const char* path = "/tmp/scope/main.cpp";\n' + "x" * 64000
    assert safe_result({"newText": value, "insertText": value}, root, set()) == {
        "newText": value,
        "insertText": value,
    }


class FakeService:
    ready = False

    async def start(self):
        self.ready = True

    async def close(self):
        self.ready = False

    async def query(self, payload):
        return {"result": {"items": []}, "diagnostics": []}


def test_http_authentication_validation_and_limits(tmp_path):
    settings = Settings(shared_secret=SECRET, work_root=tmp_path, body_limit=1024)
    with TestClient(create_app(settings, service=FakeService())) as client:
        readiness = client.get("/health/ready")
        assert readiness.status_code == 200
        assert readiness.json() == {
            "ready": True,
            "isolation": "container",
            "filesystem_isolated": False,
        }
        body = json.dumps(manifest()).encode()
        assert client.post("/v1/query", content=body).status_code == 401
        headers = signed_headers(SECRET, body)
        headers["Content-Type"] = "application/json"
        response = client.post("/v1/query", content=body, headers=headers)
        assert response.status_code == 200
        assert response.json() == {"result": {"items": []}, "diagnostics": []}
        assert (
            client.post("/v1/query", content=body, headers=headers).status_code == 401
        )
        assert client.post("/v1/query", content=b"x" * 1025).status_code == 413
        invalid = json.dumps(manifest(position={"line": 999, "character": 1})).encode()
        response = client.post(
            "/v1/query",
            content=invalid,
            headers={
                **signed_headers(SECRET, invalid),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 422
        assert "int main" not in response.text


def test_env_cannot_disable_sandbox(monkeypatch):
    monkeypatch.setenv("CODE_INTELLIGENCE_SHARED_SECRET", "s" * 32)
    monkeypatch.setenv("CODE_INTELLIGENCE_TEST_WITHOUT_SANDBOX", "true")
    assert not Settings.from_env().test_without_sandbox


def test_isolation_defaults_to_container_and_accepts_explicit_landlock(monkeypatch):
    monkeypatch.setenv("CODE_INTELLIGENCE_SHARED_SECRET", "s" * 32)
    monkeypatch.delenv("CODE_INTELLIGENCE_ISOLATION", raising=False)
    assert Settings.from_env().isolation == "container"
    monkeypatch.setenv("CODE_INTELLIGENCE_ISOLATION", "landlock")
    assert Settings.from_env().isolation == "landlock"
    monkeypatch.setenv("CODE_INTELLIGENCE_ISOLATION", "none")
    with pytest.raises(ValueError, match="isolation must be"):
        Settings.from_env()


@pytest.mark.parametrize("mode", ["container", "landlock"])
def test_readiness_reports_actual_isolation(tmp_path, mode):
    settings = Settings(shared_secret=SECRET, work_root=tmp_path, isolation=mode)
    with TestClient(create_app(settings, service=FakeService())) as client:
        assert client.get("/health/ready").json() == {
            "ready": True,
            "isolation": mode,
            "filesystem_isolated": mode == "landlock",
        }
