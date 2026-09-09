from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.config import Settings
from app.models.courses import Course
from app.models.identity import LMSConnection
from app.models.integration import ExternalMapping, SyncOutbox


def test_documented_lms_worker_environment_aliases() -> None:
    settings = Settings(
        _env_file=None,
        APP_DEBUG=True,
        APP_SECRET_KEY="test-secret-key-with-more-than-thirty-two-characters",
        LMS_CHECKPOINT_MAX_FILES=77,
        LMS_SYNC_RECEIPT_MAX_BYTES=70_000,
        LMS_SYNC_COURSE_INTERVAL_SECONDS=123,
    )
    assert settings.sync_checkpoint_max_files == 77
    assert settings.sync_receipt_max_bytes == 70_000
    assert settings.sync_course_interval_seconds == 123


async def _teacher_login(
    client: AsyncClient, *, csrf_cookie_name: str
) -> tuple[dict, dict[str, str]]:
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    response = await client.post(
        "/api/v1/auth/dev-login",
        headers={"X-CSRFToken": csrf.json()["csrf_token"]},
        json={"role": "TEACHER"},
    )
    assert response.status_code == 201, response.text
    return response.json(), {"X-CSRFToken": client.cookies[csrf_cookie_name]}


async def test_assessment_mapping_is_explicit_unique_and_projects_moodle_deadlines(
    app_bundle,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session, headers = await _teacher_login(client, csrf_cookie_name=settings.csrf_cookie_name)
        course_id = uuid.UUID(session["memberships"][0]["course_id"])
        async with session_factory() as db:
            course = await db.get(Course, course_id)
            assert course is not None
            course.policies = {
                "lms_activities": [
                    {
                        "cmid": 701,
                        "module": "assign",
                        "name": "Контрольная Moodle",
                        "opens_at_epoch": 1_800_000_000,
                        "due_at_epoch": 1_800_003_600,
                        "cutoff_at_epoch": 1_800_007_200,
                        "grade_max": 10.0,
                    },
                    {
                        "cmid": 702,
                        "module": "assign",
                        "name": "Шкала 20",
                        "opens_at_epoch": 0,
                        "due_at_epoch": 0,
                        "cutoff_at_epoch": 0,
                        "grade_max": 20.0,
                    },
                    {
                        "cmid": 703,
                        "module": "assign",
                        "name": "Сроки не заданы",
                        "opens_at_epoch": 0,
                        "due_at_epoch": 0,
                        "cutoff_at_epoch": 0,
                        "grade_max": 10.0,
                    },
                ]
            }
            await db.commit()

        created = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "CONTROL",
                "title": "Контрольная",
                "max_score": "10.00",
                "policy": {
                    "lms_grade_mapping": {
                        "provider": "MOODLE",
                        "module": "assign",
                        "cmid": 701,
                        "sync_deadlines": True,
                    }
                },
            },
        )
        assert created.status_code == 201, created.text
        assessment_id = uuid.UUID(created.json()["id"])
        assert datetime.fromisoformat(created.json()["opens_at"]) == datetime.fromtimestamp(
            1_800_000_000, tz=UTC
        )
        assert datetime.fromisoformat(created.json()["closes_at"]) == datetime.fromtimestamp(
            1_800_007_200, tz=UTC
        )

        async with session_factory() as db:
            mapping = await db.scalar(
                select(ExternalMapping).where(ExternalMapping.local_id == assessment_id)
            )
            assert mapping is not None
            assert mapping.external_type == "mod_assign"
            assert mapping.external_id == "701"
            assert mapping.metadata_json["sync_state"] == "ANSWER_TRANSPORT_UNSUPPORTED"
            assert mapping.metadata_json["submission_mode"] == "REQUIRES_CONFIGURATION"

        duplicate = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "LAB",
                "title": "Ошибочный дубль",
                "max_score": "10.00",
                "policy": {"lms_grade_mapping": {"cmid": 701}},
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "LMS_ACTIVITY_ALREADY_MAPPED"

        wrong_scale = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "LAB",
                "title": "Несогласованная шкала",
                "max_score": "10.00",
                "policy": {"lms_grade_mapping": {"cmid": 702}},
            },
        )
        assert wrong_scale.status_code == 422
        assert wrong_scale.json()["code"] == "LMS_GRADE_RANGE_MISMATCH"

        local_open = datetime.fromtimestamp(1_800_200_000, tz=UTC)
        local_close = datetime.fromtimestamp(1_800_203_600, tz=UTC)
        unknown_remote_dates = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "LAB",
                "title": "Локальные сроки сохраняются",
                "opens_at": local_open.isoformat(),
                "closes_at": local_close.isoformat(),
                "max_score": "10.00",
                "policy": {"lms_grade_mapping": {"cmid": 703}},
            },
        )
        assert unknown_remote_dates.status_code == 201, unknown_remote_dates.text
        assert datetime.fromisoformat(unknown_remote_dates.json()["opens_at"]) == local_open
        assert datetime.fromisoformat(unknown_remote_dates.json()["closes_at"]) == local_close

        removed = await client.patch(
            f"/api/v1/assessments/{assessment_id}",
            headers=headers,
            json={"policy": {}},
        )
        assert removed.status_code == 200, removed.text
        async with session_factory() as db:
            assert (
                await db.scalar(
                    select(ExternalMapping.id).where(ExternalMapping.local_id == assessment_id)
                )
                is None
            )


async def test_missing_moodle_activity_is_visible_in_mapping_state(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session, headers = await _teacher_login(client, csrf_cookie_name=settings.csrf_cookie_name)
        course_id = uuid.UUID(session["memberships"][0]["course_id"])
        created = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "LAB",
                "title": "Ещё не создана в Moodle",
                "max_score": "10.00",
                "policy": {"lms_grade_mapping": {"cmid": 999}},
            },
        )
        assert created.status_code == 201, created.text
        assessment_id = uuid.UUID(created.json()["id"])
        async with session_factory() as db:
            mapping = await db.scalar(
                select(ExternalMapping).where(ExternalMapping.local_id == assessment_id)
            )
            assert mapping is not None
            assert mapping.metadata_json["activity"] is None
            assert mapping.metadata_json["sync_state"] == "MISSING_IN_MOODLE"


async def test_quiz_essay_mapping_uses_proven_attachment_mode_and_projects_deadlines(
    app_bundle,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session, headers = await _teacher_login(client, csrf_cookie_name=settings.csrf_cookie_name)
        course_id = uuid.UUID(session["memberships"][0]["course_id"])
        async with session_factory() as db:
            course = await db.get(Course, course_id)
            assert course is not None
            course.policies = {
                "lms_activities": [
                    {
                        "cmid": 30354,
                        "module": "quiz",
                        "name": "Самостоятельная работа",
                        "opens_at_epoch": 1_800_100_000,
                        "due_at_epoch": 1_800_103_600,
                        # A Quiz grade range is deliberately not coupled to the
                        # local rubric while browser-based grade export is staged.
                        "grade_max": 100.0,
                        "question_count": 1,
                        "essay_question_count": 1,
                        "import_supported": True,
                        "answer_transport": "ESSAY_ATTACHMENT",
                    }
                ]
            }
            await db.commit()

        created = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "INDEPENDENT",
                "title": "Самостоятельная",
                "max_score": "10.00",
                "policy": {
                    "lms_activity_mapping": {
                        "provider": "MOODLE",
                        "module": "quiz",
                        "cmid": 30354,
                    }
                },
            },
        )
        assert created.status_code == 201, created.text
        assert datetime.fromisoformat(created.json()["opens_at"]) == datetime.fromtimestamp(
            1_800_100_000, tz=UTC
        )
        assert datetime.fromisoformat(created.json()["closes_at"]) == datetime.fromtimestamp(
            1_800_103_600, tz=UTC
        )

        assessment_id = uuid.UUID(created.json()["id"])
        async with session_factory() as db:
            mapping = await db.scalar(
                select(ExternalMapping).where(ExternalMapping.local_id == assessment_id)
            )
            assert mapping is not None
            assert mapping.external_type == "mod_quiz"
            assert mapping.external_id == "30354"
            assert mapping.metadata_json["module"] == "quiz"
            assert mapping.metadata_json["submission_mode"] == "ESSAY_ATTACHMENT"
            assert mapping.metadata_json["sync_state"] == "CURRENT"

        duplicate = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "LAB",
                "title": "Дубль Quiz",
                "max_score": "10.00",
                "policy": {"lms_activity_mapping": {"module": "mod_quiz", "cmid": 30354}},
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == "LMS_ACTIVITY_ALREADY_MAPPED"


async def test_quiz_essay_mapping_rejects_explicit_question_slot(app_bundle) -> None:
    app, _, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session, headers = await _teacher_login(client, csrf_cookie_name=settings.csrf_cookie_name)
        response = await client.post(
            f"/api/v1/courses/{session['memberships'][0]['course_id']}/assessments",
            headers=headers,
            json={
                "type": "INDEPENDENT",
                "title": "Некорректный слот",
                "max_score": "10.00",
                "policy": {
                    "lms_activity_mapping": {
                        "module": "quiz",
                        "cmid": 30354,
                        "question_slot": 1,
                    }
                },
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "MOODLE_QUESTION_SLOT_UNSUPPORTED"


async def test_publishing_course_task_creates_one_idempotent_moodle_outbox_event(
    app_bundle,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session, headers = await _teacher_login(client, csrf_cookie_name=settings.csrf_cookie_name)
        course_id = session["memberships"][0]["course_id"]
        async with session_factory() as db:
            course = await db.get(Course, uuid.UUID(course_id))
            assert course is not None
            connection = await db.get(LMSConnection, course.connection_id)
            assert connection is not None
            connection.provider = "MOODLE"
            connection.config = {"auth_mode": "BRIDGE"}
            await db.commit()
        item = await client.post(
            "/api/v1/task-bank/items",
            headers=headers,
            json={
                "scope": "COURSE",
                "course": course_id,
                "slug": "moodle-mirror-contract",
                "category": "Moodle",
                "tags": [],
            },
        )
        assert item.status_code == 201, item.text
        version = await client.post(
            f"/api/v1/task-bank/items/{item.json()['id']}/versions",
            headers=headers,
            json={
                "title": "Зеркалируемая задача",
                "statement": "Напечатайте число.",
                "language": "CPP",
                "language_standard": "C++20",
                "multi_file": False,
                "starter_files": [{"path": "main.cpp", "content": ""}],
                "build_profile": "cpp-gcc-c++20-single",
                "public_examples": [],
                "hidden_test_manifest": {
                    "schema_version": 1,
                    "cases": [
                        {
                            "name": "zero",
                            "stdin": "0\n",
                            "expected_stdout": "0\n",
                            "comparison": "EXACT",
                        }
                    ],
                },
                "max_score": "10.00",
                "difficulty": "1",
                "ai_policy": {},
            },
        )
        assert version.status_code == 201, version.text
        version_id = version.json()["id"]
        first = await client.post(
            f"/api/v1/task-versions/{version_id}/publish", headers=headers, json={}
        )
        second = await client.post(
            f"/api/v1/task-versions/{version_id}/publish", headers=headers, json={}
        )
        assert first.status_code == 200
        assert second.status_code == 200

        async with session_factory() as db:
            rows = list(
                (
                    await db.scalars(
                        select(SyncOutbox).where(
                            SyncOutbox.aggregate_id == uuid.UUID(version_id),
                            SyncOutbox.event_type == "task.version",
                        )
                    )
                ).all()
            )
            assert len(rows) == 1
            assert rows[0].payload["status"] == "PUBLISHED"
            assert '"schema_version":1' in rows[0].payload["definition_json"]
