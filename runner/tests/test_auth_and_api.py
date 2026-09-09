from __future__ import annotations

import json

from fastapi.testclient import TestClient

from conftest import signed_headers


def _valid_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "request_id": "api-test",
        "profile_id": "cpp-clang-c++20-single",
        "action": "compile",
        "files": [{"path": "main.cpp", "content": "int main() { return 0; }"}],
        "stdin": "",
    }


def test_health_is_public(client: TestClient) -> None:
    assert client.get("/health/live").status_code == 200
    readiness = client.get("/health/ready")
    assert readiness.status_code == 200
    payload = readiness.json()
    assert payload["policy"] == "UNRESTRICTED_CONTAINER"
    assert payload["executor"] == "local"
    assert "without per-job" in payload["warning"]


def test_profile_catalog_uses_same_hmac_contract(client: TestClient) -> None:
    assert client.get("/v1/profiles").status_code == 401
    response = client.get("/v1/profiles", headers=signed_headers(b""))
    assert response.status_code == 200
    assert any(item["id"] == "cpp-clang-c++20-single" for item in response.json())


def test_job_requires_valid_signature(client: TestClient) -> None:
    body = json.dumps(_valid_payload()).encode()
    assert client.post("/v1/jobs", content=body).status_code == 401
    headers = signed_headers(body)
    headers["X-Runner-Signature"] = "v1=" + "0" * 64
    assert client.post("/v1/jobs", content=body, headers=headers).status_code == 401


def test_nonce_cannot_be_replayed(client: TestClient) -> None:
    body = json.dumps(_valid_payload()).encode()
    headers = signed_headers(body, nonce="fixed-nonce-000000000000")
    assert client.post("/v1/jobs", content=body, headers=headers).status_code == 200
    assert client.post("/v1/jobs", content=body, headers=headers).status_code == 409


def test_traversal_is_rejected_by_contract(client: TestClient) -> None:
    payload = _valid_payload()
    payload["files"] = [{"path": "../../etc/passwd", "content": "x"}]
    body = json.dumps(payload).encode()
    response = client.post("/v1/jobs", content=body, headers=signed_headers(body))
    assert response.status_code == 422
    assert "path" in response.text


def test_extra_fields_are_rejected(client: TestClient) -> None:
    payload = _valid_payload()
    payload["command"] = "sh -c id"
    body = json.dumps(payload).encode()
    response = client.post("/v1/jobs", content=body, headers=signed_headers(body))
    assert response.status_code == 422


def test_execution_limits_are_strict_and_forbid_unknown_fields(
    client: TestClient,
) -> None:
    for limits in (
        {"cpu_seconds": "1", "memory_mb": 64},
        {"cpu_seconds": 1, "memory_mb": 64, "process_count": 1},
        {"cpu_seconds": 0, "memory_mb": 64},
        {"cpu_seconds": 1},
    ):
        payload = _valid_payload()
        payload["limits"] = limits
        body = json.dumps(payload).encode()
        response = client.post("/v1/jobs", content=body, headers=signed_headers(body))
        assert response.status_code == 422


def test_request_body_is_bounded_before_json_parsing(client: TestClient) -> None:
    body = b"x" * (4 * 1024 * 1024 + 1)
    response = client.post("/v1/jobs", content=body, headers=signed_headers(body))
    assert response.status_code == 413
