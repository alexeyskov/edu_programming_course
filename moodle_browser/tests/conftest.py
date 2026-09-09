from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from moodle_browser.app import create_app
from moodle_browser.config import Settings
from moodle_browser.models import (
    AssignmentSubmissionPrepareRequest,
    AssignmentSubmissionPrepareResponse,
    AssignmentSubmissionSyncRequest,
    AssignmentSubmissionSyncResponse,
    CourseDiscoverResponse,
    GradeRequest,
    GradeResponse,
    HistoricalSubmissionsRequest,
    HistoricalSubmissionsResponse,
    LoginResponse,
    QuizEssayPrepareRequest,
    QuizEssayPrepareResponse,
    QuizEssaySyncRequest,
    QuizEssaySyncResponse,
)
from moodle_browser.security import signed_headers

SHARED_SECRET = b"moodle-browser-test-secret-32-bytes-minimum"
BASE_URL = "https://edu.mmcs.sfedu.ru"


def storage_state() -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "opaque-session-value",
                "domain": "edu.mmcs.sfedu.ru",
                "path": "/",
                "expires": -1.0,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


class FakeService:
    def readiness(self) -> dict[str, object]:
        return {"browser": "connected", "ready": True}

    async def login(self, _payload: object) -> LoginResponse:
        return LoginResponse.model_validate(
            {
                "identity": {
                    "external_subject": "42",
                    "display_name": "Преподаватель",
                    "email": "teacher@example.test",
                    "locale": "ru",
                    "courses": [],
                },
                "storage_state": storage_state(),
            }
        )

    async def discover_course(self, payload: Any) -> CourseDiscoverResponse:
        snapshot = {
            "complete": True,
            "members": [
                {
                    "user_id": payload.actor_external_subject,
                    "display_name": "Преподаватель",
                    "email": "",
                    "suspended": False,
                    "role": "TEACHER",
                    "roles": ["TEACHER"],
                    "groups": [],
                }
            ],
        }
        return CourseDiscoverResponse.model_validate(
            {
                "discovery": {
                    "external_id": payload.external_id,
                    "actor_role": "TEACHER",
                    "preview": {
                        "external_id": payload.external_id,
                        "title": "C++",
                        "short_name": "CPP",
                        "external_revision": "a" * 64,
                        "membership_revision": "b" * 64,
                        "starts_at_epoch": 0,
                        "ends_at_epoch": 0,
                        "sections": [],
                        "groups": [],
                        "membership_snapshot": snapshot,
                    },
                    "capabilities": {
                        "roster": True,
                        "groups": True,
                        "grades": False,
                        "comments": False,
                    },
                },
                "storage_state": storage_state(),
            }
        )

    async def grade_assignment(self, payload: GradeRequest) -> GradeResponse:
        return GradeResponse.model_validate(
            {
                "status": "DELIVERED",
                "receipt": {
                    "module": payload.payload.module,
                    "course_id": payload.payload.course_id,
                    "cmid": payload.payload.cmid,
                    "user_id": payload.payload.user_id,
                    "idempotency_key": payload.idempotency_key,
                },
                "storage_state": storage_state(),
            }
        )

    async def discover_historical_submissions(
        self, payload: HistoricalSubmissionsRequest
    ) -> HistoricalSubmissionsResponse:
        return HistoricalSubmissionsResponse.model_validate(
            {
                "course_id": payload.course_id,
                "activity": payload.activity.model_dump(mode="json"),
                "items": [
                    {
                        "external_id": f"quiz:{payload.activity.cmid}:123",
                        "module": payload.activity.module,
                        "cmid": payload.activity.cmid,
                        "attempt_id": "123",
                        "user_id": "77",
                        "display_name": "Студент",
                        "state": "GRADED",
                        "submitted_at_epoch": 1_787_608_800,
                        "grade": 8.0,
                        "grade_max": 10.0,
                        "comment": "Проверено",
                        "responses": [],
                        "external_revision": "c" * 64,
                    }
                ],
                "next_cursor": None,
                "complete": True,
                "warnings": [],
                "storage_state": storage_state(),
            }
        )

    async def sync_quiz_essay(self, payload: QuizEssaySyncRequest) -> QuizEssaySyncResponse:
        artifact = base64.b64decode(payload.artifact.content_base64, validate=True)
        return QuizEssaySyncResponse.model_validate(
            {
                "status": "FINALIZED" if payload.finalize else "DRAFT_SAVED",
                "receipt": {
                    "course_id": payload.course_id,
                    "cmid": payload.cmid,
                    "attempt_id": "123",
                    "question_slot": "1",
                    "filename": payload.artifact.filename,
                    "sha256": payload.artifact.sha256,
                    "size_bytes": len(artifact),
                    "idempotency_key": payload.idempotency_key,
                },
                "storage_state": storage_state(),
            }
        )

    async def prepare_quiz_essay(
        self, payload: QuizEssayPrepareRequest
    ) -> QuizEssayPrepareResponse:
        return QuizEssayPrepareResponse.model_validate(
            {
                "status": "READY",
                "preparation": {
                    "course_id": payload.course_id,
                    "cmid": payload.cmid,
                    "attempt_id": "141716",
                    "question_slot": "1",
                    "question_text": "Реализовать класс Vector3D.",
                    "answer_transport": "ESSAY_ATTACHMENT",
                    "available_answer_transports": ["ESSAY_ATTACHMENT"],
                },
                "storage_state": storage_state(),
            }
        )

    async def sync_assignment_submission(
        self, payload: AssignmentSubmissionSyncRequest
    ) -> AssignmentSubmissionSyncResponse:
        artifact = base64.b64decode(payload.artifact.content_base64, validate=True)
        return AssignmentSubmissionSyncResponse.model_validate(
            {
                "status": "FINALIZED" if payload.finalize else "DRAFT_SAVED",
                "receipt": {
                    "course_id": payload.course_id,
                    "cmid": payload.cmid,
                    "filename": payload.artifact.filename,
                    "sha256": payload.artifact.sha256,
                    "size_bytes": len(artifact),
                    "idempotency_key": payload.idempotency_key,
                },
                "storage_state": storage_state(),
            }
        )

    async def prepare_assignment_submission(
        self, payload: AssignmentSubmissionPrepareRequest
    ) -> AssignmentSubmissionPrepareResponse:
        return AssignmentSubmissionPrepareResponse.model_validate(
            {
                "status": "READY",
                "preparation": {
                    "course_id": payload.course_id,
                    "cmid": payload.cmid,
                    "answer_transport": "ASSIGN_FILE",
                    "available_answer_transports": ["ASSIGN_FILE"],
                },
                "storage_state": storage_state(),
            }
        )


@pytest.fixture
def settings() -> Settings:
    return Settings(shared_secret=SHARED_SECRET, base_url=BASE_URL)


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, service=FakeService()))


def encoded(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()


def auth_headers(body: bytes, *, nonce: str | None = None) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        **signed_headers(SHARED_SECRET, body, nonce=nonce),
    }
