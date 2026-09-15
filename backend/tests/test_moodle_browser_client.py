from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.core.config import Settings
from app.integrations.errors import (
    IntegrationAssessmentUnavailable,
    IntegrationAttemptFinalized,
    IntegrationBusy,
    IntegrationConfigurationError,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from app.integrations.moodle_browser import (
    MoodleBrowserClient,
    MoodleBrowserQuizEssayArtifact,
)
from app.integrations.moodle_standard import MoodleAuthenticationError


def settings() -> Settings:
    return Settings(
        _env_file=None,
        debug=True,
        secret_key="test-secret-" + "x" * 40,
        moodle_base_url="https://moodle.example.edu",
    )


def storage_state(*, marker: str = "session") -> dict[str, object]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": marker,
                "domain": "moodle.example.edu",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [
            {
                "origin": "https://moodle.example.edu",
                "localStorage": [{"name": "lang", "value": "ru"}],
            }
        ],
    }


@pytest.mark.asyncio
async def test_login_is_hmac_signed_and_returns_identity_with_refreshed_state() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "identity": {
                    "external_subject": "42",
                    "display_name": "Teacher Name",
                    "email": "teacher@example.edu",
                    "locale": "ru",
                    "courses": [
                        {
                            "external_id": "549",
                            "title": "C++",
                            "short_name": "CPP",
                            "role": "TEACHER",
                        }
                    ],
                },
                "storage_state": storage_state(marker="refreshed"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            clock=lambda: 1_700_000_000,
            nonce_factory=lambda: "fixed-nonce-0000",
        )
        result = await browser.authenticate(
            "teacher",
            "correct horse battery staple",
            allowed_course_ids=("549", "550"),
        )

    assert captured["url"] == "http://moodle-browser:8082/internal/v1/moodle/login"
    body = captured["body"]
    assert isinstance(body, bytes)
    assert json.loads(body) == {
        "base_url": "https://moodle.example.edu",
        "password": "correct horse battery staple",
        "schema_version": "1.0",
        "username": "teacher",
        "allowed_course_ids": ["549", "550"],
    }
    canonical = f"1700000000\nfixed-nonce-0000\n{hashlib.sha256(body).hexdigest()}".encode()
    expected = hmac.new(b"b" * 32, canonical, hashlib.sha256).hexdigest()
    headers = captured["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["x-moodle-signature"] == f"v1={expected}"
    assert result.identity.external_subject == "42"
    assert result.identity.token == ""
    assert result.identity.courses[0].role == "TEACHER"
    assert result.storage_state["cookies"][0]["value"] == "refreshed"


@pytest.mark.asyncio
async def test_login_rejects_duplicate_or_invalid_allowed_course_ids_before_http() -> None:
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
        )
        with pytest.raises(IntegrationProtocolError, match="duplicates"):
            await browser.authenticate(
                "teacher",
                "secret",
                allowed_course_ids=("549", "549"),
            )
        with pytest.raises(IntegrationProtocolError, match="course id"):
            await browser.authenticate(
                "teacher",
                "secret",
                allowed_course_ids=("0",),
            )

    assert calls == 0


@pytest.mark.asyncio
async def test_course_discovery_is_fixed_origin_and_normalized() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "discovery": {
                    "external_id": "549",
                    "actor_role": "TEACHER",
                    "preview": {
                        "external_id": "549",
                        "title": "Programming in C++",
                        "short_name": "CPP",
                        "external_revision": "a" * 64,
                        "membership_revision": "b" * 64,
                        "starts_at_epoch": 0,
                        "ends_at_epoch": 0,
                        "sections": [
                            {
                                "external_id": "10",
                                "title": "Labs",
                                "position": 1,
                                "visible": True,
                                "activities": [
                                    {
                                        "cmid": 77,
                                        "instance_id": 88,
                                        "module": "assign",
                                        "name": "Lab 1",
                                    }
                                ],
                            }
                        ],
                        "groups": [
                            {"external_id": "3", "name": "Teachers"},
                            {"external_id": "4", "name": "2.1"},
                        ],
                        "membership_snapshot": {
                            "complete": True,
                            "members": [
                                {
                                    "user_id": "42",
                                    "display_name": "Teacher Name",
                                    "role": "TEACHER",
                                    "roles": ["TEACHER"],
                                    "groups": [{"external_id": "3", "name": "Teachers"}],
                                },
                                {
                                    "user_id": "43",
                                    "display_name": "Student Name",
                                    "role": "STUDENT",
                                    "roles": ["STUDENT"],
                                    "groups": [{"external_id": "4", "name": "2.1"}],
                                },
                            ],
                        },
                    },
                    "capabilities": {
                        "roster": True,
                        "groups": True,
                        "grades": False,
                        "comments": False,
                    },
                },
                "storage_state": storage_state(marker="after-discovery"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        assert (
            browser.recognize_course_url("https://moodle.example.edu/course/view.php?id=549")
            == "549"
        )
        result = await browser.discover_course("549", "42")

    assert captured["url"] == ("http://moodle-browser:8082/internal/v1/moodle/course/discover")
    request_body = captured["body"]
    assert isinstance(request_body, dict)
    assert request_body["external_id"] == "549"
    assert request_body["actor_external_subject"] == "42"
    assert request_body["interactive"] is False
    assert result.discovery.external_id == "549"
    assert result.discovery.preview["title"] == "Programming in C++"
    assert result.discovery.preview["sections"][0]["activities"][0]["cmid"] == 77
    assert result.discovery.preview["groups"] == [
        {"external_id": "3", "name": "Teachers"},
        {"external_id": "4", "name": "2.1"},
    ]
    assert result.discovery.preview["membership_snapshot"]["members"][1]["role"] == "STUDENT"
    assert len(result.discovery.preview["external_revision"]) == 64
    assert result.discovery.capabilities["grades"] is False
    assert result.storage_state["cookies"][0]["value"] == "after-discovery"


@pytest.mark.asyncio
async def test_discovery_rejects_wrong_course_and_unconfirmed_teacher() -> None:
    responses = [
        {
            "external_id": "550",
            "title": "Wrong course",
            "actor_role": "TEACHER",
            "sections": [],
            "participants": [],
            "capabilities": {},
        },
        {
            "external_id": "549",
            "title": "Course",
            "actor_role": "STUDENT",
            "sections": [],
            "participants": [],
            "capabilities": {},
        },
    ]

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"discovery": responses.pop(0), "storage_state": storage_state()},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="different course"):
            await browser.discover_course("549", "42")
        with pytest.raises(IntegrationProtocolError, match="teacher membership"):
            await browser.discover_course("549", "42")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["ASSIGN_TABLE_NOT_FOUND", "QUIZ_TABLE_NOT_FOUND", "private-token", None]
)
async def test_history_error_preserves_only_known_connector_diagnostics(code: str | None) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            headers={"X-Moodle-Error-Code": code} if code else {},
            json={"detail": "secret-bearing page body"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(), client, service_url="http://moodle-browser:8082",
            shared_secret="b" * 32, storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError) as error:
            await browser.discover_historical_submissions(
                course_id="549", actor_external_subject="42", module="assign", cmid=777,
            )

    message = str(error.value)
    if code in ("ASSIGN_TABLE_NOT_FOUND", "QUIZ_TABLE_NOT_FOUND"):
        assert code in message
    else:
        assert "HTTP 502" in message
    assert "secret-bearing" not in message
    assert "private-token" not in message


@pytest.mark.parametrize("operation", ["sync_quiz_essay", "sync_quiz_answers"])
@pytest.mark.parametrize("code", [
    "MOODLE_RESPONSE_TIMEOUT", "MOODLE_DOCUMENT_TIMEOUT", "MOODLE_DNS_ERROR",
    "MOODLE_CONNECTION_ERROR", "MOODLE_TLS_ERROR", "MOODLE_HTTP_ERROR",
    "MOODLE_NAVIGATION_ERROR", "private-token", None,
])
async def test_navigation_errors_preserve_safe_diagnostics_and_retries(operation, code):
    async def handler(_request):
        return httpx.Response(
            503, headers={"X-Moodle-Error-Code": code} if code else {},
            json={"detail": "secret-bearing page body"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(), client, service_url="http://moodle-browser:8082",
            shared_secret="b" * 32, storage_state=storage_state(),
        )
        with pytest.raises(IntegrationUnavailable) as caught:
            await browser._call(operation, {})
    assert caught.value.retryable
    if code and code.startswith("MOODLE_"):
        assert code in str(caught.value)
    assert "secret-bearing" not in str(caught.value)
    assert "private-token" not in str(caught.value)


@pytest.mark.parametrize("operation", ["sync_quiz_essay", "sync_quiz_answers"])
@pytest.mark.parametrize("code", ["UPLOAD_INVALID_FILE", "UPLOAD_INVALID_TYPE", "private-token"])
async def test_upload_errors_preserve_safe_diagnostics(operation, code) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502, headers={"X-Moodle-Error-Code": code},
            json={"detail": "secret-bearing page body"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(), client, service_url="http://moodle-browser:8082",
            shared_secret="b" * 32, storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError) as caught:
            await browser._call(operation, {})
    if code.startswith("UPLOAD_"):
        assert code in str(caught.value)
    else:
        assert "HTTP 502" in str(caught.value)
    assert "secret-bearing" not in str(caught.value)
    assert "private-token" not in str(caught.value)


async def test_historical_submission_discovery_is_bounded_and_refreshes_state() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "course_id": "549",
                "activity": {"module": "quiz", "cmid": 777},
                "items": [
                    {
                        "external_id": "quiz:777:123",
                        "module": "quiz",
                        "cmid": 777,
                        "attempt_id": "123",
                        "user_id": "43",
                        "display_name": "Student Name",
                        "state": "GRADED",
                        "submitted_at_epoch": 1_700_000_000,
                        "grade": 8.0,
                        "grade_max": 10.0,
                        "comment": "Проверено",
                        "responses_complete": False,
                        "responses": [
                            {
                                "response_id": "1",
                                "question_text": "Задача",
                                "answer_text": "int main() {}",
                                "answer_complete": True,
                                "answer_omission_reason": "",
                                "grade": 8.0,
                                "grade_max": 10.0,
                                "comment": "Хорошо",
                                "artifacts": [],
                            }
                        ],
                        "external_revision": "a" * 64,
                    }
                ],
                "next_cursor": "0:1",
                "complete": False,
                "warnings": [],
                "storage_state": storage_state(marker="after-history"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.discover_historical_submissions(
            course_id="549",
            actor_external_subject="42",
            module="quiz",
            cmid=777,
            cursor="0:0",
            limit=5,
            priority_only=True,
        )

    assert captured["url"] == (
        "http://moodle-browser:8082/internal/v1/moodle/activity/submissions/discover"
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["activity"] == {"module": "quiz", "cmid": 777}
    assert body["actor_external_subject"] == "42"
    assert body["cursor"] == "0:0"
    assert body["limit"] == 5
    assert body["priority_only"] is True
    assert result.items[0]["external_id"] == "quiz:777:123"
    assert result.items[0]["responses_complete"] is False
    assert result.next_cursor == "0:1"
    assert result.complete is False
    assert result.storage_state["cookies"][0]["value"] == "after-history"


@pytest.mark.asyncio
async def test_invalid_credentials_and_grade_delivery_are_safely_mapped() -> None:
    async def login_handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"detail": "safe"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(login_handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
        )
        with pytest.raises(MoodleAuthenticationError):
            await browser.authenticate("teacher", "wrong-password")

    captured: dict[str, object] = {}

    async def grade_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "DELIVERED",
                "receipt": {"target_path": "/mod/assign/view.php"},
                "storage_state": storage_state(marker="after-grade"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(grade_handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.push_grade(
            {
                "courseid": "549",
                "cmid": "77",
                "userid": "43",
                "grade": 5.0,
                "comment": "Reviewed",
            },
            "grade-event-1",
        )
        assert result.status == "DELIVERED"
        assert result.receipt["target_path"] == "/mod/assign/view.php"
        assert result.storage_state["cookies"][0]["value"] == "after-grade"
    grade_body = captured["body"]
    assert isinstance(grade_body, dict)
    assert grade_body["payload"] == {
        "module": "assign",
        "course_id": "549",
        "cmid": 77,
        "user_id": "43",
        "grade": 5.0,
        "comment": "Reviewed",
    }


@pytest.mark.asyncio
async def test_quiz_grade_transmits_the_explicit_local_grade_scale() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "DELIVERED",
                "receipt": {"target_path": "/mod/quiz/comment.php"},
                "storage_state": storage_state(marker="after-grade"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        await browser.push_grade(
            {
                "module": "quiz",
                "course_id": "549",
                "cmid": 30354,
                "user_id": "43",
                "attempt_id": "134403",
                "question_slot": 1,
                "grade": "1.20",
                "grade_scale_max": "3.00",
                "quiz_overall_grade_max": "3.00",
                "comment": "Reviewed",
            },
            "quiz-grade-event-1",
        )

    assert captured["payload"] == {
        "module": "quiz",
        "course_id": "549",
        "cmid": 30354,
        "user_id": "43",
        "attempt_id": "134403",
        "question_slot": 1,
        "grade": 1.2,
        "grade_scale_max": 3.0,
        "quiz_overall_grade_max": 3.0,
        "comment": "Reviewed",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload_update",
    [
        {},
        {"grade_scale_max": 0},
        {"grade_scale_max": 3, "grade": 3.01},
    ],
)
async def test_quiz_grade_fails_closed_without_a_valid_local_scale(
    payload_update: dict[str, object],
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid Quiz scale must not reach the browser service")

    payload: dict[str, object] = {
        "module": "quiz",
        "course_id": "549",
        "cmid": 30354,
        "user_id": "43",
        "attempt_id": "134403",
        "question_slot": 1,
        "grade": 1.2,
        "quiz_overall_grade_max": 3,
        "comment": "Reviewed",
    }
    payload.update(payload_update)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="grade scale"):
            await browser.push_grade(payload, "quiz-grade-event-1")


@pytest.mark.asyncio
async def test_quiz_grade_fails_closed_without_a_confirmed_overall_scale() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unconfirmed Quiz scale must not reach the browser service")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="overall grade scale"):
            await browser.push_grade(
                {
                    "module": "quiz",
                    "course_id": "549",
                    "cmid": 30354,
                    "user_id": "43",
                    "attempt_id": "134403",
                    "question_slot": 1,
                    "grade": 1.2,
                    "grade_scale_max": 3,
                    "comment": "Reviewed",
                },
                "quiz-grade-event-1",
            )


def test_service_url_and_storage_state_cannot_escape_configured_origins() -> None:
    async_client = httpx.AsyncClient(follow_redirects=False)
    try:
        with pytest.raises(IntegrationConfigurationError, match="service URL"):
            MoodleBrowserClient(
                settings(),
                async_client,
                service_url="http://moodle-browser:8082/untrusted/path",
                shared_secret="b" * 32,
            )

        state = storage_state()
        state["cookies"][0]["domain"] = "evil.example"
        with pytest.raises(IntegrationProtocolError, match="cookie domain"):
            MoodleBrowserClient(
                settings(),
                async_client,
                service_url="http://moodle-browser:8082",
                shared_secret="b" * 32,
                storage_state=state,
            )

        with pytest.raises(IntegrationProtocolError, match="origin is not configured"):
            MoodleBrowserClient(
                settings(),
                async_client,
                service_url="http://moodle-browser:8082",
                shared_secret="b" * 32,
                storage_state={
                    "cookies": [],
                    "origins": [{"origin": "https://evil.example", "localStorage": []}],
                },
            )
    finally:
        # No network request is made; close explicitly to avoid leaking the pool.
        import asyncio

        asyncio.run(async_client.aclose())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("finalize", "expected_status"),
    [(False, "DRAFT_SAVED"), (True, "FINALIZED")],
)
async def test_quiz_essay_sync_encodes_and_hashes_raw_artifact(
    finalize: bool,
    expected_status: str,
) -> None:
    captured: dict[str, object] = {}
    content = b"int main() { return 0; }\n"
    digest = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": expected_status,
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "attempt_id": "123",
                    "question_slot": "1",
                    "filename": "solution.cpp",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "idempotency_key": "quiz:549:777:42:v1",
                },
                "storage_state": storage_state(marker="after-quiz-sync"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.sync_quiz_essay(
            "549",
            777,
            MoodleBrowserQuizEssayArtifact("solution.cpp", content),
            answer_transport="ESSAY_ATTACHMENT",
            finalize=finalize,
            idempotency_key="quiz:549:777:42:v1",
        )

    assert captured["url"] == ("http://moodle-browser:8082/internal/v1/moodle/quiz/essay/sync")
    body = captured["body"]
    assert isinstance(body, dict)
    assert body == {
        "schema_version": "1.0",
        "base_url": "https://moodle.example.edu",
        "course_id": "549",
        "cmid": 777,
        "answer_transport": "ESSAY_ATTACHMENT",
        "artifact": {
            "filename": "solution.cpp",
            "content_base64": "aW50IG1haW4oKSB7IHJldHVybiAwOyB9Cg==",
            "sha256": digest,
        },
        "finalize": finalize,
        "idempotency_key": "quiz:549:777:42:v1",
        "storage_state": storage_state(),
    }
    assert result.status == expected_status
    assert result.receipt.course_id == "549"
    assert result.receipt.cmid == 777
    assert result.receipt.attempt_id == "123"
    assert result.receipt.question_slot == "1"
    assert result.receipt.sha256 == digest
    assert result.receipt.size_bytes == len(content)
    assert result.storage_state["cookies"][0]["value"] == "after-quiz-sync"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("artifact", "message"),
    [
        (MoodleBrowserQuizEssayArtifact("../solution.cpp", b"x"), "filename"),
        (MoodleBrowserQuizEssayArtifact("solution.cpp.", b"x"), "filename"),
        (
            MoodleBrowserQuizEssayArtifact("solution.cpp", b"x" * (4 * 1024 * 1024 + 1)),
            "size",
        ),
    ],
)
async def test_quiz_essay_sync_rejects_unsafe_or_oversized_artifact_before_http(
    artifact: MoodleBrowserQuizEssayArtifact,
    message: str,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid artifact must not reach browser service")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match=message):
            await browser.sync_quiz_essay(
                "549",
                777,
                artifact,
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v1",
            )


@pytest.mark.asyncio
async def test_quiz_essay_prepare_binds_concrete_question_and_refreshes_state() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "READY",
                "preparation": {
                    "course_id": "549",
                    "cmid": 30354,
                    "attempt_id": "141716",
                    "question_slot": "1",
                    "question_text": "Реализовать класс Vector3D.",
                    "answer_transport": "ESSAY_ATTACHMENT",
                    "available_answer_transports": ["ESSAY_ATTACHMENT"],
                },
                "storage_state": storage_state(marker="after-quiz-prepare"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.prepare_quiz_essay("549", 30354)

    assert captured["url"] == ("http://moodle-browser:8082/internal/v1/moodle/quiz/essay/prepare")
    assert captured["body"] == {
        "schema_version": "1.0",
        "base_url": "https://moodle.example.edu",
        "course_id": "549",
        "cmid": 30354,
        "storage_state": storage_state(),
    }
    assert result.preparation.attempt_id == "141716"
    assert result.preparation.question_slot == "1"
    assert result.preparation.question_text == "Реализовать класс Vector3D."
    assert result.preparation.answer_transport == "ESSAY_ATTACHMENT"
    assert result.preparation.available_answer_transports == ("ESSAY_ATTACHMENT",)
    assert result.storage_state["cookies"][0]["value"] == "after-quiz-prepare"


@pytest.mark.asyncio
async def test_quiz_essay_prepare_rejects_response_that_changes_expected_attempt() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "READY",
                "preparation": {
                    "course_id": "549",
                    "cmid": 30354,
                    "attempt_id": "141717",
                    "question_slot": "1",
                    "question_text": "Другой вопрос.",
                    "answer_transport": "ESSAY_ATTACHMENT",
                    "available_answer_transports": ["ESSAY_ATTACHMENT"],
                },
                "storage_state": storage_state(),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="changed the bound"):
            await browser.prepare_quiz_essay(
                "549",
                30354,
                expected_attempt_id="141716",
                expected_question_slot="1",
            )


@pytest.mark.asyncio
async def test_assignment_submission_sync_uses_module_route_and_managed_receipt() -> None:
    captured: dict[str, object] = {}
    content = b"int main() { return 0; }\n"
    digest = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "FINALIZED",
                "receipt": {
                    "course_id": "549",
                    "cmid": 23461,
                    "filename": "main.cpp",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "idempotency_key": "assign:549:23461:42:v2",
                },
                "storage_state": storage_state(marker="after-assignment-sync"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.sync_assignment_submission(
            "549",
            23461,
            MoodleBrowserQuizEssayArtifact("main.cpp", content),
            answer_transport="ASSIGN_FILE",
            finalize=False,
            requires_submission_statement=True,
            submission_drafts=False,
            previous_managed_filename="main.cpp",
            previous_managed_sha256=digest,
            idempotency_key="assign:549:23461:42:v2",
        )

    assert captured["url"] == (
        "http://moodle-browser:8082/internal/v1/moodle/assignment/submission/sync"
    )
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["answer_transport"] == "ASSIGN_FILE"
    assert body["previous_managed_filename"] == "main.cpp"
    assert body["previous_managed_sha256"] == digest
    assert body["submission_drafts"] is False
    assert body["requires_submission_statement"] is True
    assert result.status == "FINALIZED"


@pytest.mark.asyncio
async def test_assignment_prepare_uses_live_student_form_route() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "READY",
                "preparation": {
                    "course_id": "549",
                    "cmid": 23461,
                    "answer_transport": "ASSIGN_FILE",
                    "available_answer_transports": [
                        "ASSIGN_ONLINE_TEXT",
                        "ASSIGN_FILE",
                    ],
                },
                "storage_state": storage_state(marker="after-assignment-prepare"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.prepare_assignment_submission("549", 23461)

    assert captured["url"] == (
        "http://moodle-browser:8082/internal/v1/moodle/assignment/submission/prepare"
    )
    assert captured["body"] == {
        "schema_version": "1.0",
        "base_url": "https://moodle.example.edu",
        "course_id": "549",
        "cmid": 23461,
        "storage_state": storage_state(),
    }
    assert result.preparation.answer_transport == "ASSIGN_FILE"
    assert result.preparation.available_answer_transports == (
        "ASSIGN_ONLINE_TEXT",
        "ASSIGN_FILE",
    )
    assert result.storage_state["cookies"][0]["value"] == "after-assignment-prepare"


@pytest.mark.asyncio
async def test_assignment_prepare_maps_student_access_denial() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "safe"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationAssessmentUnavailable):
            await browser.prepare_assignment_submission("549", 23461)


@pytest.mark.asyncio
async def test_quiz_essay_sync_rejects_unproven_answer_transport_before_http() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unproven transport must not reach browser service")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="answer transport"):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", b"x"),
                answer_transport="FUTURE_TRANSPORT",  # type: ignore[arg-type]
                finalize=False,
                idempotency_key="quiz:549:777:42:unsupported",
            )


@pytest.mark.asyncio
async def test_quiz_essay_sync_accepts_empty_source() -> None:
    digest = hashlib.sha256(b"").hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["artifact"] == {
            "filename": "solution.cpp",
            "content_base64": "",
            "sha256": digest,
        }
        return httpx.Response(
            200,
            json={
                "status": "DRAFT_SAVED",
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "attempt_id": "123",
                    "question_slot": "1",
                    "filename": "solution.cpp",
                    "sha256": digest,
                    "size_bytes": 0,
                    "idempotency_key": "quiz:549:777:42:empty",
                },
                "storage_state": storage_state(marker="after-empty-sync"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.sync_quiz_essay(
            "549",
            777,
            MoodleBrowserQuizEssayArtifact("solution.cpp", b""),
            answer_transport="ESSAY_ATTACHMENT",
            finalize=False,
            idempotency_key="quiz:549:777:42:empty",
        )

    assert result.receipt.size_bytes == 0


@pytest.mark.asyncio
async def test_four_mibibyte_quiz_artifact_fits_default_transport_limit() -> None:
    content = b"x" * (4 * 1024 * 1024)
    digest = hashlib.sha256(content).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert len(request.content) < 6 * 1024 * 1024
        return httpx.Response(
            200,
            json={
                "status": "DRAFT_SAVED",
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "attempt_id": "123",
                    "question_slot": "1",
                    "filename": "solution.cpp",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "idempotency_key": "quiz:549:777:42:v1",
                },
                "storage_state": storage_state(marker="large-artifact-saved"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.sync_quiz_essay(
            "549",
            777,
            MoodleBrowserQuizEssayArtifact("solution.cpp", content),
            answer_transport="ESSAY_ATTACHMENT",
            finalize=False,
            idempotency_key="quiz:549:777:42:v1",
        )

    assert result.receipt.size_bytes == 4 * 1024 * 1024


@pytest.mark.asyncio
async def test_quiz_artifact_respects_lower_configured_transport_limit() -> None:
    constrained_settings = Settings(
        _env_file=None,
        debug=True,
        secret_key="test-secret-" + "x" * 40,
        moodle_base_url="https://moodle.example.edu",
        moodle_browser_request_body_max_bytes=16 * 1024,
        moodle_browser_storage_state_max_bytes=16 * 1024,
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("oversized encoded request must not reach browser service")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            constrained_settings,
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="request exceeds"):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", b"x" * (13 * 1024)),
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v1",
            )


@pytest.mark.asyncio
async def test_quiz_question_slot_fails_closed_until_service_supports_multi_essay() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        raise AssertionError("unsupported question slot must not reach browser service")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationConfigurationError, match="multi-essay"):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", b"x"),
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v1",
                question_slot=1,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "error_type", "message"),
    [
        (401, MoodleAuthenticationError, "session expired"),
        (409, IntegrationProtocolError, "idempotency key conflicts"),
        (429, IntegrationBusy, "connector is busy"),
        (503, IntegrationUnavailable, "status 503"),
    ],
)
async def test_quiz_essay_sync_maps_service_errors(
    status_code: int,
    error_type: type[Exception],
    message: str,
) -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"detail": "safe"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(error_type, match=message):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", b"x"),
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v1",
            )


@pytest.mark.asyncio
async def test_quiz_essay_sync_sends_expected_attempt_identity_and_maps_finalized() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(423, json={"code": "MOODLE_ATTEMPT_FINALIZED"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationAttemptFinalized):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", b"x"),
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v2",
                expected_attempt_id="123",
                expected_question_slot="1",
            )

    assert captured["expected_attempt_id"] == "123"
    assert captured["expected_question_slot"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("receipt_mutation", ["digest", "size", "oversized"])
async def test_quiz_essay_sync_rejects_mismatched_or_unbounded_receipt(
    receipt_mutation: str,
) -> None:
    content = b"x"
    digest = hashlib.sha256(content).hexdigest()

    async def handler(_: httpx.Request) -> httpx.Response:
        receipt: dict[str, object] = {
            "course_id": "549",
            "cmid": 777,
            "attempt_id": "123",
            "question_slot": "1",
            "filename": "solution.cpp",
            "sha256": digest,
            "size_bytes": 1,
            "idempotency_key": "quiz:549:777:42:v1",
        }
        if receipt_mutation == "digest":
            receipt["sha256"] = "0" * 64
        elif receipt_mutation == "size":
            receipt["size_bytes"] = 2
        else:
            receipt["filename"] = "x" * 5000
        return httpx.Response(
            200,
            json={
                "status": "DRAFT_SAVED",
                "receipt": receipt,
                "storage_state": storage_state(marker="must-not-be-accepted"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", content),
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v1",
            )
        assert browser.storage_state == storage_state()


@pytest.mark.asyncio
@pytest.mark.parametrize("returned_status", ["draft_saved", "FINALIZED", "DELIVERED"])
async def test_quiz_essay_sync_rejects_invalid_or_inconsistent_status(
    returned_status: str,
) -> None:
    content = b"x"
    digest = hashlib.sha256(content).hexdigest()

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": returned_status,
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "attempt_id": "123",
                    "question_slot": "1",
                    "filename": "solution.cpp",
                    "sha256": digest,
                    "size_bytes": len(content),
                    "idempotency_key": "quiz:549:777:42:v1",
                },
                "storage_state": storage_state(marker="must-not-be-accepted"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError, match="status"):
            await browser.sync_quiz_essay(
                "549",
                777,
                MoodleBrowserQuizEssayArtifact("solution.cpp", content),
                answer_transport="ESSAY_ATTACHMENT",
                finalize=False,
                idempotency_key="quiz:549:777:42:v1",
            )
        assert browser.storage_state == storage_state()
