from __future__ import annotations

import base64
import hashlib

import pytest
from conftest import BASE_URL, auth_headers, encoded, storage_state
from fastapi.testclient import TestClient

from moodle_browser.service import MoodleAttemptFinalized


def login_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "username": "teacher",
        "password": "not-logged-or-returned",
        "allowed_course_ids": ["549"],
    }


def test_health_is_public_and_reports_browser_readiness(client: TestClient) -> None:
    assert client.get("/health/live").json()["browser"] == "connected"
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["ready"] is True


def test_login_requires_hmac_and_nonce_cannot_be_replayed(client: TestClient) -> None:
    body = encoded(login_payload())
    assert client.post("/internal/v1/moodle/login", content=body).status_code == 401
    headers = auth_headers(body, nonce="fixed-login-nonce-000000000")
    accepted = client.post("/internal/v1/moodle/login", content=body, headers=headers)
    assert accepted.status_code == 200
    assert "not-logged-or-returned" not in accepted.text
    replay = client.post("/internal/v1/moodle/login", content=body, headers=headers)
    assert replay.status_code == 409


def test_signature_is_bound_to_exact_body(client: TestClient) -> None:
    body = encoded(login_payload())
    changed = encoded({**login_payload(), "username": "another"})
    response = client.post(
        "/internal/v1/moodle/login",
        content=changed,
        headers=auth_headers(body),
    )
    assert response.status_code == 401


def test_login_rejects_duplicate_or_non_positive_allowed_course_ids(
    client: TestClient,
) -> None:
    duplicate = {**login_payload(), "allowed_course_ids": ["549", "549"]}
    duplicate_body = encoded(duplicate)
    duplicate_response = client.post(
        "/internal/v1/moodle/login",
        content=duplicate_body,
        headers=auth_headers(duplicate_body),
    )
    assert duplicate_response.status_code == 422

    invalid = {**login_payload(), "allowed_course_ids": ["0"]}
    invalid_body = encoded(invalid)
    invalid_response = client.post(
        "/internal/v1/moodle/login",
        content=invalid_body,
        headers=auth_headers(invalid_body),
    )
    assert invalid_response.status_code == 422


def test_discover_contract_is_typed_and_has_teacher_evidence(client: TestClient) -> None:
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "external_id": "549",
        "actor_external_subject": "42",
        "storage_state": storage_state(),
    }
    body = encoded(payload)
    response = client.post(
        "/internal/v1/moodle/course/discover",
        content=body,
        headers=auth_headers(body),
    )
    assert response.status_code == 200
    assert response.json()["discovery"]["actor_role"] == "TEACHER"
    assert response.json()["discovery"]["preview"]["external_id"] == "549"


def test_grade_is_type_safe_and_returns_a_delivery_receipt(client: TestClient) -> None:
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "storage_state": storage_state(),
        "payload": {
            "course_id": "549",
            "cmid": 777,
            "user_id": "77",
            "grade": 8,
            "comment": "Проверено",
        },
        "idempotency_key": "grade:549:777:77:v1",
    }
    body = encoded(payload)
    response = client.post(
        "/internal/v1/moodle/assignment/grade",
        content=body,
        headers=auth_headers(body),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "DELIVERED"
    assert response.json()["receipt"]["module"] == "assign"

    payload["payload"] = {**payload["payload"], "javascript": "alert(1)"}  # type: ignore[dict-item]
    invalid_body = encoded(payload)
    invalid = client.post(
        "/internal/v1/moodle/assignment/grade",
        content=invalid_body,
        headers=auth_headers(invalid_body),
    )
    assert invalid.status_code == 422

    payload["payload"] = {
        "module": "quiz",
        "course_id": "549",
        "cmid": 777,
        "user_id": "77",
        "grade": 8,
        "comment": "Проверено",
    }
    missing_quiz_target = encoded(payload)
    invalid_quiz = client.post(
        "/internal/v1/moodle/assignment/grade",
        content=missing_quiz_target,
        headers=auth_headers(missing_quiz_target),
    )
    assert invalid_quiz.status_code == 422


def test_historical_submissions_are_hmac_protected_and_cursor_bounded(
    client: TestClient,
) -> None:
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "course_id": "549",
        "actor_external_subject": "42",
        "activity": {"module": "quiz", "cmid": 30354},
        "cursor": "0:0",
        "limit": 10,
        "storage_state": storage_state(),
    }
    body = encoded(payload)
    assert (
        client.post("/internal/v1/moodle/activity/submissions/discover", content=body).status_code
        == 401
    )
    response = client.post(
        "/internal/v1/moodle/activity/submissions/discover",
        content=body,
        headers=auth_headers(body),
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["external_id"] == "quiz:30354:123"
    assert response.json()["complete"] is True

    payload["cursor"] = "../../etc/passwd"
    invalid_body = encoded(payload)
    invalid = client.post(
        "/internal/v1/moodle/activity/submissions/discover",
        content=invalid_body,
        headers=auth_headers(invalid_body),
    )
    assert invalid.status_code == 422


def test_request_body_is_bounded_before_json_parsing(client: TestClient) -> None:
    body = b"x" * (6 * 1024 * 1024 + 1)
    response = client.post(
        "/internal/v1/moodle/login",
        content=body,
        headers=auth_headers(body),
    )
    assert response.status_code == 413


@pytest.mark.parametrize("artifact", [b"", b"x"])
def test_quiz_essay_sync_uses_same_hmac_contract(client: TestClient, artifact: bytes) -> None:
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "course_id": "549",
        "cmid": 777,
        "artifact": {
            "filename": "solution.cpp",
            "content_base64": base64.b64encode(artifact).decode(),
            "sha256": hashlib.sha256(artifact).hexdigest(),
        },
        "finalize": False,
        "idempotency_key": "quiz:549:777:42:v1",
        "storage_state": storage_state(),
    }
    body = encoded(payload)
    unsigned = client.post("/internal/v1/moodle/quiz/essay/sync", content=body)
    assert unsigned.status_code == 401
    response = client.post(
        "/internal/v1/moodle/quiz/essay/sync",
        content=body,
        headers=auth_headers(body),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT_SAVED"
    assert response.json()["receipt"]["sha256"] == payload["artifact"]["sha256"]
    assert response.json()["receipt"]["size_bytes"] == len(artifact)


def test_quiz_essay_prepare_uses_hmac_and_returns_bound_question(client: TestClient) -> None:
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "course_id": "549",
        "cmid": 30354,
        "storage_state": storage_state(),
    }
    body = encoded(payload)
    assert client.post("/internal/v1/moodle/quiz/essay/prepare", content=body).status_code == 401

    response = client.post(
        "/internal/v1/moodle/quiz/essay/prepare",
        content=body,
        headers=auth_headers(body),
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "READY",
        "preparation": {
            "course_id": "549",
            "cmid": 30354,
            "attempt_id": "141716",
            "question_slot": "1",
            "question_text": "Реализовать класс Vector3D.",
            "answer_transport": "ESSAY_ATTACHMENT",
            "available_answer_transports": ["ESSAY_ATTACHMENT"],
            "remaining_seconds": None,
        },
        "storage_state": storage_state(),
    }


def test_finalized_moodle_attempt_has_a_typed_locked_response(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = b"int main() {}\n"
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "course_id": "549",
        "cmid": 777,
        "artifact": {
            "filename": "solution.cpp",
            "content_base64": base64.b64encode(artifact).decode(),
            "sha256": hashlib.sha256(artifact).hexdigest(),
        },
        "expected_attempt_id": "123",
        "expected_question_slot": "1",
        "finalize": False,
        "idempotency_key": "quiz:549:777:42:finalized",
        "storage_state": storage_state(),
    }

    async def finalized(_payload: object) -> None:
        raise MoodleAttemptFinalized("already finalized")

    monkeypatch.setattr(
        client.app.state.moodle_browser_service,
        "sync_quiz_essay",
        finalized,
    )
    body = encoded(payload)
    response = client.post(
        "/internal/v1/moodle/quiz/essay/sync",
        content=body,
        headers=auth_headers(body),
    )

    assert response.status_code == 423
    assert response.json() == {"detail": "Moodle attempt is already finalized"}


def test_assignment_submission_sync_uses_hmac_and_typed_transport(
    client: TestClient,
) -> None:
    artifact = b"int main() { return 0; }\n"
    payload = {
        "schema_version": "1.0",
        "base_url": BASE_URL,
        "course_id": "549",
        "cmid": 23461,
        "answer_transport": "ASSIGN_FILE",
        "artifact": {
            "filename": "main.cpp",
            "content_base64": base64.b64encode(artifact).decode(),
            "sha256": hashlib.sha256(artifact).hexdigest(),
        },
        "finalize": False,
        "requires_submission_statement": True,
        "submission_drafts": False,
        "idempotency_key": "assign:549:23461:42:v1",
        "storage_state": storage_state(),
    }
    body = encoded(payload)
    assert (
        client.post("/internal/v1/moodle/assignment/submission/sync", content=body).status_code
        == 401
    )
    response = client.post(
        "/internal/v1/moodle/assignment/submission/sync",
        content=body,
        headers=auth_headers(body),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT_SAVED"
    assert response.json()["receipt"]["cmid"] == 23461
