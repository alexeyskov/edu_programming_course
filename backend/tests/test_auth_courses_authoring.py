from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.cli import _normalize_base_url, build_parser
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
    CourseSection,
)
from app.models.enums import AssessmentStatus, CourseRole, TaskVersionStatus
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    LoginTransaction,
    UsedLaunchNonce,
)
from app.models.tasks import Assessment, TaskVersion


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


async def _dev_login(
    client: AsyncClient,
    *,
    role: str,
    admin_token: str | None = None,
) -> dict:
    headers = await _csrf(client)
    payload = {"role": role}
    if admin_token is not None:
        payload["admin_token"] = admin_token
    response = await client.post("/api/v1/auth/dev-login", headers=headers, json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def _signed_assertion(payload: dict, secret: str) -> str:
    body = (
        base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    signature = (
        base64.urlsafe_b64encode(
            hmac.new(secret.encode(), body.encode("ascii"), hashlib.sha256).digest()
        )
        .decode("ascii")
        .rstrip("=")
    )
    return f"{body}.{signature}"


async def test_dev_login_session_admin_elevation_and_logout(app_bundle):
    app, _, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.admin_token = SecretStr("development-administrator-token")
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        rejected = await client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={"role": "TEACHER", "admin_token": "wrong"},
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "INVALID_ADMIN_TOKEN"

        session = await _dev_login(client, role="TEACHER")
        assert session["roles"] == ["TEACHER"]
        assert session["capabilities"] == []
        headers = {"X-CSRFToken": client.cookies[settings.csrf_cookie_name]}
        elevated = await client.post(
            "/api/v1/auth/admin-elevation",
            headers=headers,
            json={"admin_token": "development-administrator-token"},
        )
        assert elevated.status_code == 200
        assert elevated.json()["capabilities"] == ["SYSTEM_SETTINGS"]
        assert elevated.json()["admin_elevation_expires_at"]

        dropped = await client.delete("/api/v1/auth/admin-elevation", headers=headers)
        assert dropped.status_code == 204
        current = await client.get("/api/v1/auth/session")
        assert current.status_code == 200
        assert current.json()["capabilities"] == []

        logged_out = await client.post("/api/v1/auth/logout", headers=headers)
        assert logged_out.status_code == 204
        assert (await client.get("/api/v1/auth/session")).status_code == 401


async def test_auth_contract_has_two_roles_and_admin_token_grants_only_capability(app_bundle):
    app, _, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.admin_token = SecretStr("development-administrator-token")

    assert {role.value for role in CourseRole} == {"STUDENT", "TEACHER"}
    assert not any(
        marker in path.lower()
        for path in app.openapi()["paths"]
        for marker in ("register", "registration", "signup", "sign-up")
    )

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        unsupported = await client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={"role": "ADMIN"},
        )
        assert unsupported.status_code == 422

        session = await _dev_login(
            client,
            role="STUDENT",
            admin_token="development-administrator-token",
        )
        assert session["roles"] == ["STUDENT"]
        assert {row["role"] for row in session["memberships"]} == {"STUDENT"}
        assert session["capabilities"] == ["SYSTEM_SETTINGS"]
        assert session["admin_elevation_expires_at"]


async def test_admin_elevation_is_revoked_when_client_network_prefix_changes(app_bundle):
    app, _, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.admin_token = SecretStr("development-administrator-token")

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("198.51.100.10", 41000)),
        base_url="http://testserver",
    ) as original:
        session = await _dev_login(
            original,
            role="TEACHER",
            admin_token="development-administrator-token",
        )
        assert session["capabilities"] == ["SYSTEM_SETTINGS"]
        cookies = original.cookies

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("203.0.113.25", 42000)),
        base_url="http://testserver",
        cookies=cookies,
    ) as moved:
        current = await moved.get("/api/v1/auth/session")
        assert current.status_code == 200
        assert current.json()["roles"] == ["TEACHER"]
        assert current.json()["capabilities"] == []
        assert current.json()["admin_elevation_expires_at"] is None


async def test_production_has_no_local_login_and_moodle_rejects_admin_role(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        settings.debug = False
        disabled = await client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={"role": "STUDENT"},
        )
        assert disabled.status_code == 404
        assert disabled.json()["code"] == "DEV_LOGIN_DISABLED"

        now = int(time.time())
        invalid_launch = await client.post(
            "/api/v1/auth/moodle/callback",
            data={
                "assertion": _signed_assertion(
                    {
                        "iss": "https://moodle.example.test",
                        "sub": "42",
                        "aud": settings.public_base_url,
                        "iat": now,
                        "exp": now + 60,
                        "nonce": "ef" * 16,
                        "display_name": "Unsupported role",
                        "role": "ADMIN",
                    },
                    "irrelevant-signature-secret",
                )
            },
        )
        assert invalid_launch.status_code == 403
        assert invalid_launch.json()["code"] == "INVALID_COURSE_ROLE"

    async with session_factory() as db:
        principals = list((await db.scalars(select(ExternalPrincipal))).all())
        assert principals == []


async def test_database_rejects_a_third_course_role(app_bundle):
    _, session_factory, _ = app_bundle
    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://role-constraint.example.test",
        )
        db.add(connection)
        await db.flush()
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="role-constraint-user",
            display_name="Role Constraint User",
        )
        course = Course(
            connection_id=connection.id,
            external_id="role-constraint-course",
            title="C++",
        )
        db.add_all([principal, course])
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role="ADMIN",
            )
        )
        with pytest.raises(IntegrityError):
            await db.flush()


async def test_course_membership_isolation_and_student_safe_projection(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as teacher_client:
        teacher = await _dev_login(teacher_client, role="TEACHER")
        own_course_id = uuid.UUID(teacher["memberships"][0]["course_id"])
        async with session_factory() as db:
            own_course = await db.get(Course, own_course_id)
            assert own_course is not None
            own_course.policies = {"hidden_teacher_policy": True}
            other = Course(
                connection_id=own_course.connection_id,
                external_id="other-course",
                title="Other course",
                policies={"must_not_leak": True},
            )
            db.add(other)
            await db.commit()
            other_id = other.id

        listed = await teacher_client.get("/api/v1/courses")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [str(own_course_id)]
        forbidden = await teacher_client.get(f"/api/v1/courses/{other_id}")
        assert forbidden.status_code == 403

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as student_client:
        student = await _dev_login(student_client, role="STUDENT")
        course_id = uuid.UUID(student["memberships"][0]["course_id"])
        async with session_factory() as db:
            membership_id = await db.scalar(
                select(CourseMembershipGroup.coursemembership_id)
                .join(
                    CourseGroup,
                    CourseGroup.id == CourseMembershipGroup.coursegroup_id,
                )
                .where(CourseGroup.course_id == course_id)
            )
            if membership_id is None:
                from app.models.courses import CourseMembership

                membership = await db.scalar(
                    select(CourseMembership).where(
                        CourseMembership.course_id == course_id,
                        CourseMembership.principal_id == uuid.UUID(student["principal"]["id"]),
                    )
                )
                assert membership is not None
                group = CourseGroup(
                    course_id=course_id,
                    external_id="student-group",
                    name="1.1",
                )
                hidden_group = CourseGroup(
                    course_id=course_id,
                    external_id="other-group",
                    name="2.1",
                )
                hidden_section = CourseSection(
                    course_id=course_id,
                    external_id="hidden-section",
                    title="Hidden",
                    position=100,
                    visible=False,
                )
                db.add_all([group, hidden_group, hidden_section])
                await db.flush()
                db.add(
                    CourseMembershipGroup(
                        coursemembership_id=membership.id,
                        coursegroup_id=group.id,
                    )
                )
                await db.commit()

        student_courses = await student_client.get("/api/v1/courses")
        assert student_courses.status_code == 200
        assert "policies" not in student_courses.json()[0]
        assert "external_id" not in student_courses.json()[0]
        groups = await student_client.get(f"/api/v1/courses/{course_id}/groups")
        assert [row["name"] for row in groups.json()] == ["1.1"]
        assert "external_id" not in groups.json()[0]
        sections = await student_client.get(f"/api/v1/courses/{course_id}/sections")
        assert all(row["visible"] for row in sections.json())
        policies = await student_client.get(f"/api/v1/courses/{course_id}/policies")
        assert policies.status_code == 403


async def test_task_and_assessment_state_machine_and_course_immutability(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _dev_login(client, role="TEACHER")
        headers = {"X-CSRFToken": client.cookies[settings.csrf_cookie_name]}
        course_id = uuid.UUID(session["memberships"][0]["course_id"])
        item = await client.post(
            "/api/v1/task-bank/items",
            headers=headers,
            json={
                "scope": "COURSE",
                "course": str(course_id),
                "slug": "immutable-sum",
                "category": "Basics",
                "tags": ["loops"],
            },
        )
        assert item.status_code == 201, item.text
        item_id = item.json()["id"]
        immutable_scope = await client.patch(
            f"/api/v1/task-bank/items/{item_id}",
            headers=headers,
            json={"course": str(course_id)},
        )
        assert immutable_scope.status_code == 422

        version = await client.post(
            f"/api/v1/task-bank/items/{item_id}/versions",
            headers=headers,
            json={
                "title": "Sum",
                "statement": "Read two integers and print their sum.",
                "language": "CPP",
                "language_standard": "C++20",
                "multi_file": False,
                "starter_files": [
                    {"path": "main.cpp", "content": ""},
                    {"path": "fixtures/input.txt", "content": "2 3\n"},
                ],
                "build_profile": "cpp-gcc-c++20-single",
                "public_examples": [],
                "hidden_test_manifest": {},
                "max_score": 10,
                "difficulty": "1",
                "ai_policy": {},
            },
        )
        assert version.status_code == 201, version.text
        version_id = version.json()["id"]
        updated = await client.put(
            f"/api/v1/task-versions/{version_id}",
            headers=headers,
            json={
                "title": "Configured sum",
                "statement": "Read two integers and print their configured sum.",
                "language": "CPP",
                "language_standard": "C++20",
                "multi_file": False,
                "starter_files": [
                    {"path": "main.cpp", "content": ""},
                    {"path": "fixtures/input.txt", "content": "2 3\n"},
                ],
                "build_profile": "cpp-gcc-c++20-single",
                "public_examples": [],
                "hidden_test_manifest": {},
                "max_score": 10,
                "difficulty": "1",
                "ai_policy": {},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["title"] == "Configured sum"
        assert [file["path"] for file in updated.json()["starter_files"]] == [
            "main.cpp",
            "fixtures/input.txt",
        ]
        validation = await client.post(
            f"/api/v1/task-versions/{version_id}/validate", headers=headers, json={}
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is True
        published = await client.post(
            f"/api/v1/task-versions/{version_id}/publish", headers=headers, json={}
        )
        assert published.status_code == 200
        assert published.json()["status"] == "PUBLISHED"
        immutable_version = await client.put(
            f"/api/v1/task-versions/{version_id}",
            headers=headers,
            json={
                "title": "Must not change",
                "statement": "Published content is immutable.",
                "language": "CPP",
                "language_standard": "C++20",
                "multi_file": False,
                "starter_files": [{"path": "main.cpp", "content": ""}],
                "build_profile": "cpp-gcc-c++20-single",
                "public_examples": [],
                "hidden_test_manifest": {},
                "max_score": 10,
                "difficulty": "1",
                "ai_policy": {},
            },
        )
        assert immutable_version.status_code == 409
        assert immutable_version.json()["code"] == "TASK_VERSION_IMMUTABLE"

        async with session_factory() as db:
            own = await db.get(Course, course_id)
            assert own is not None
            other = Course(
                connection_id=own.connection_id,
                external_id="section-other-course",
                title="Other",
            )
            db.add(other)
            await db.flush()
            wrong_section = CourseSection(
                course_id=other.id,
                external_id="wrong-section",
                title="Wrong",
                position=0,
            )
            db.add(wrong_section)
            await db.commit()
            wrong_section_id = wrong_section.id

        mismatch = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={
                "type": "LAB",
                "title": "Wrong section",
                "section_id": str(wrong_section_id),
                "max_score": "10.00",
            },
        )
        assert mismatch.status_code == 422
        assert mismatch.json()["code"] == "SECTION_COURSE_MISMATCH"

        assessment = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={"type": "LAB", "title": "Lab", "max_score": "10.00"},
        )
        assert assessment.status_code == 201
        assessment_id = assessment.json()["id"]
        attached = await client.post(
            f"/api/v1/assessments/{assessment_id}/items",
            headers=headers,
            json={"task_version": version_id, "position": 0, "points": "10.00"},
        )
        assert attached.status_code == 201, attached.text
        assessment_published = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish", headers=headers, json={}
        )
        assert assessment_published.status_code == 200
        assert assessment_published.json()["status"] == "PUBLISHED"
        archive_in_use = await client.post(
            f"/api/v1/task-versions/{version_id}/archive", headers=headers, json={}
        )
        assert archive_in_use.status_code == 409
        assert archive_in_use.json()["code"] == "TASK_VERSION_IN_USE"
        edit_after_publish = await client.patch(
            f"/api/v1/assessments/{assessment_id}",
            headers=headers,
            json={"title": "Changed"},
        )
        assert edit_after_publish.status_code == 409
        assert edit_after_publish.json()["code"] == "ASSESSMENT_IMMUTABLE"


async def test_assessment_publication_atomically_publishes_valid_attached_draft(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        teacher = await _dev_login(client, role="TEACHER")
        headers = {"X-CSRFToken": client.cookies[settings.csrf_cookie_name]}
        course_id = teacher["memberships"][0]["course_id"]

        item = await client.post(
            "/api/v1/task-bank/items",
            headers=headers,
            json={
                "scope": "COURSE",
                "course": course_id,
                "slug": "atomic-import-publication",
                "category": "Imported",
                "tags": [],
            },
        )
        assert item.status_code == 201, item.text
        version = await client.post(
            f"/api/v1/task-bank/items/{item.json()['id']}/versions",
            headers=headers,
            json={
                "title": "Configured imported task",
                "statement": "Solve the configured task.",
                "language": "CPP",
                "language_standard": "C++20",
                "multi_file": False,
                "starter_files": [{"path": "main.cpp", "content": ""}],
                "build_profile": "cpp-gcc-c++20-single",
                "public_examples": [],
                "hidden_test_manifest": {},
                "max_score": 10,
                "difficulty": "1",
                "ai_policy": {},
            },
        )
        assert version.status_code == 201, version.text
        version_id = version.json()["id"]
        assert (
            await client.post(
                f"/api/v1/task-versions/{version_id}/publish", headers=headers, json={}
            )
        ).status_code == 200

        assessment = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={"type": "LAB", "title": "Imported lab", "max_score": "10.00"},
        )
        assert assessment.status_code == 201, assessment.text
        assessment_id = assessment.json()["id"]
        attached = await client.post(
            f"/api/v1/assessments/{assessment_id}/items",
            headers=headers,
            json={"task_version": version_id, "position": 0, "points": "10.00"},
        )
        assert attached.status_code == 201, attached.text

        # Moodle materialisation attaches a draft directly. Recreate that state
        # after using the public authoring API to build the surrounding records.
        async with session_factory() as db:
            stored_version = await db.get(TaskVersion, uuid.UUID(version_id))
            assert stored_version is not None
            stored_version.status = TaskVersionStatus.DRAFT.value
            stored_version.published_at = None
            await db.commit()

        validation = await client.post(
            f"/api/v1/assessments/{assessment_id}/validate", headers=headers, json={}
        )
        assert validation.status_code == 200, validation.text
        assert validation.json()["valid"] is True
        assert {row["code"] for row in validation.json()["warnings"]} >= {"TASK_WILL_BE_PUBLISHED"}

        published = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish", headers=headers, json={}
        )
        assert published.status_code == 200, published.text
        assert published.json()["status"] == AssessmentStatus.PUBLISHED.value
        async with session_factory() as db:
            stored_version = await db.get(TaskVersion, uuid.UUID(version_id))
            stored_assessment = await db.get(Assessment, uuid.UUID(assessment_id))
            assert stored_version is not None
            assert stored_assessment is not None
            assert stored_version.status == TaskVersionStatus.PUBLISHED.value
            assert stored_version.published_at is not None
            assert stored_assessment.status == AssessmentStatus.PUBLISHED.value
            assert stored_assessment.published_at is not None


async def test_legacy_import_configuration_marker_does_not_block_local_publication(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        teacher = await _dev_login(client, role="TEACHER")
        headers = {"X-CSRFToken": client.cookies[settings.csrf_cookie_name]}
        course_id = teacher["memberships"][0]["course_id"]
        item = await client.post(
            "/api/v1/task-bank/items",
            headers=headers,
            json={
                "scope": "COURSE",
                "course": course_id,
                "slug": "unconfigured-import-publication",
                "category": "Imported",
                "tags": [],
            },
        )
        assert item.status_code == 201, item.text
        version = await client.post(
            f"/api/v1/task-bank/items/{item.json()['id']}/versions",
            headers=headers,
            json={
                "title": "Imported placeholder",
                "statement": "Replace this placeholder.",
                "language": "CPP",
                "language_standard": "C++17",
                "multi_file": False,
                "starter_files": [{"path": "main.cpp", "content": ""}],
                "build_profile": "cpp-gcc-c++17-single",
                "public_examples": [],
                "hidden_test_manifest": {},
                "max_score": 10,
                "difficulty": "",
                "ai_policy": {},
            },
        )
        assert version.status_code == 201, version.text
        version_id = version.json()["id"]
        assert (
            await client.post(
                f"/api/v1/task-versions/{version_id}/publish", headers=headers, json={}
            )
        ).status_code == 200
        assessment = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={"type": "LAB", "title": "Imported draft", "max_score": "10.00"},
        )
        assert assessment.status_code == 201, assessment.text
        assessment_id = assessment.json()["id"]
        assert (
            await client.post(
                f"/api/v1/assessments/{assessment_id}/items",
                headers=headers,
                json={"task_version": version_id, "position": 0, "points": "10.00"},
            )
        ).status_code == 201
        async with session_factory() as db:
            stored_version = await db.get(TaskVersion, uuid.UUID(version_id))
            stored_assessment = await db.get(Assessment, uuid.UUID(assessment_id))
            assert stored_version is not None
            assert stored_assessment is not None
            stored_version.status = TaskVersionStatus.DRAFT.value
            stored_version.published_at = None
            stored_assessment.policy = {"lms_import_requires_configuration": True}
            await db.commit()

        configured = await client.put(
            f"/api/v1/task-versions/{version_id}",
            headers=headers,
            json={
                "title": "Imported placeholder",
                "statement": "Replace this placeholder.",
                "language": "CPP",
                "language_standard": "C++17",
                "multi_file": False,
                "starter_files": [{"path": "main.cpp", "content": ""}],
                "build_profile": "cpp-gcc-c++17-single",
                "public_examples": [],
                "hidden_test_manifest": {},
                "max_score": 10,
                "difficulty": "",
                "ai_policy": {"lms_import_requires_configuration": True},
            },
        )
        assert configured.status_code == 200, configured.text

        validation = await client.post(
            f"/api/v1/assessments/{assessment_id}/validate", headers=headers, json={}
        )
        assert validation.status_code == 200, validation.text
        body = validation.json()
        assert body["valid"] is True
        assert body["errors"] == []
        assert "LMS_IMPORT_REQUIRES_CONFIGURATION" in {row["code"] for row in body["warnings"]}
        assert version_id not in json.dumps(body)

        published = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish", headers=headers, json={}
        )
        assert published.status_code == 200, published.text
        async with session_factory() as db:
            stored_version = await db.get(TaskVersion, uuid.UUID(version_id))
            stored_assessment = await db.get(Assessment, uuid.UUID(assessment_id))
            assert stored_version is not None
            assert stored_assessment is not None
            assert stored_version.status == TaskVersionStatus.PUBLISHED.value
            assert stored_assessment.status == AssessmentStatus.PUBLISHED.value


async def test_student_sees_only_published_available_assessments(app_bundle):
    app, _, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as teacher_client:
        teacher = await _dev_login(teacher_client, role="TEACHER")
        headers = {"X-CSRFToken": teacher_client.cookies[settings.csrf_cookie_name]}
        course_id = teacher["memberships"][0]["course_id"]
        draft = await teacher_client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={"type": "LAB", "title": "Secret draft", "max_score": "10.00"},
        )
        assert draft.status_code == 201
        draft_id = draft.json()["id"]

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as student_client:
        student = await _dev_login(student_client, role="STUDENT")
        student_course = student["memberships"][0]["course_id"]
        listed = await student_client.get(f"/api/v1/courses/{student_course}/assessments")
        assert listed.status_code == 200
        assert all(row["id"] != draft_id for row in listed.json())
        direct = await student_client.get(f"/api/v1/assessments/{draft_id}")
        assert direct.status_code == 404
        task_bank = await student_client.get("/api/v1/task-bank/items")
        assert task_bank.status_code == 403


async def test_assessment_review_policy_defaults_and_roundtrips(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        teacher = await _dev_login(client, role="TEACHER")
        headers = {"X-CSRFToken": client.cookies[settings.csrf_cookie_name]}
        course_id = teacher["memberships"][0]["course_id"]

        created = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={"type": "LAB", "title": "Policy defaults", "max_score": "10.00"},
        )
        assert created.status_code == 201, created.text
        assert created.json()["review_required"] is True
        assert created.json()["decision_support_enabled"] is True

        assessment_id = created.json()["id"]
        updated = await client.patch(
            f"/api/v1/assessments/{assessment_id}",
            headers=headers,
            json={"review_required": False, "decision_support_enabled": False},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["review_required"] is False
        assert updated.json()["decision_support_enabled"] is False

        read = await client.get(f"/api/v1/assessments/{assessment_id}")
        assert read.status_code == 200, read.text
        assert read.json()["review_required"] is False
        assert read.json()["decision_support_enabled"] is False

        other = await client.post(
            f"/api/v1/courses/{course_id}/assessments",
            headers=headers,
            json={"type": "LAB", "title": "Other draft", "max_score": "10.00"},
        )
        assert other.status_code == 201, other.text
        rule = await client.post(
            f"/api/v1/assessments/{assessment_id}/availability-rules",
            headers=headers,
            json={"target_type": "COURSE", "target_external_id": "", "allowed": True},
        )
        assert rule.status_code == 201, rule.text
        rule_id = rule.json()["id"]

        wrong_assessment = await client.delete(
            f"/api/v1/assessments/{other.json()['id']}/availability-rules/{rule_id}",
            headers=headers,
        )
        assert wrong_assessment.status_code == 404
        assert wrong_assessment.json()["code"] == "AVAILABILITY_RULE_NOT_FOUND"

        async with session_factory() as db, db.begin():
            row = await db.get(Assessment, uuid.UUID(assessment_id))
            assert row is not None
            row.status = "PUBLISHED"
        immutable = await client.delete(
            f"/api/v1/assessments/{assessment_id}/availability-rules/{rule_id}",
            headers=headers,
        )
        assert immutable.status_code == 409
        assert immutable.json()["code"] == "ASSESSMENT_IMMUTABLE"

        async with session_factory() as db, db.begin():
            row = await db.get(Assessment, uuid.UUID(assessment_id))
            assert row is not None
            row.status = "DRAFT"
        removed = await client.delete(
            f"/api/v1/assessments/{assessment_id}/availability-rules/{rule_id}",
            headers=headers,
        )
        assert removed.status_code == 204
        missing = await client.delete(
            f"/api/v1/assessments/{assessment_id}/availability-rules/{rule_id}",
            headers=headers,
        )
        assert missing.status_code == 404

        async with session_factory() as db, db.begin():
            row = await db.get(Assessment, uuid.UUID(assessment_id))
            assert row is not None
            row.status = "PUBLISHED"
            row.decision_support_enabled = True
        runtime_disable = await client.patch(
            f"/api/v1/assessments/{assessment_id}",
            headers=headers,
            json={"decision_support_enabled": False},
        )
        immutable_review_policy = await client.patch(
            f"/api/v1/assessments/{assessment_id}",
            headers=headers,
            json={"review_required": True},
        )
        assert runtime_disable.status_code == 200, runtime_disable.text
        assert runtime_disable.json()["decision_support_enabled"] is False
        assert immutable_review_policy.status_code == 409
        assert immutable_review_policy.json()["code"] == "ASSESSMENT_IMMUTABLE"


async def test_moodle_callback_verifier_hmac_elevation_and_replay(app_bundle):
    app, session_factory, settings = app_bundle
    secret = "moodle-launch-shared-secret-for-tests"
    admin_token = "development-administrator-token"
    settings.moodle_launch_shared_secret = SecretStr(secret)
    settings.admin_token = SecretStr(admin_token)
    settings.public_base_url = "http://testserver"
    settings.frontend_url = "http://frontend.test/"
    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            enabled=True,
            config={"auth_mode": "BRIDGE"},
        )
        db.add(connection)
        await db.flush()
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add(course)
        await db.commit()
        connection_id = connection.id

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        headers = await _csrf(client)
        started = await client.post(
            f"/api/v1/auth/lms/{connection_id}/start",
            headers=headers,
            json={"admin_token": admin_token, "course_id": "549"},
        )
        assert started.status_code == 201, started.text
        state = parse_qs(urlsplit(started.json()["redirect_url"]).query)["state"][0]
        now = int(time.time())
        payload = {
            "iss": "https://moodle.example.test/",
            "sub": "42",
            "aud": "http://testserver/",
            "iat": now,
            "exp": now + 60,
            "nonce": "ab" * 16,
            "display_name": "Teacher 42",
            "role": "TEACHER",
            "course_id": 549,
            "state": state,
        }
        assertion = _signed_assertion(payload, secret)
        body, _ = assertion.split(".", 1)
        invalid = f"{body}.{'A' * 43}"
        bad_callback = await client.post(
            "/api/v1/auth/moodle/callback",
            data={"assertion": invalid, "state": state},
        )
        assert bad_callback.status_code == 403
        assert bad_callback.json()["code"] == "INVALID_LAUNCH_SIGNATURE"

        callback = await client.post(
            "/api/v1/auth/moodle/callback",
            data={"assertion": assertion, "state": state},
        )
        assert callback.status_code == 303, callback.text
        current = await client.get("/api/v1/auth/session")
        assert current.status_code == 200
        assert current.json()["roles"] == ["STUDENT"]
        assert current.json()["capabilities"] == ["SYSTEM_SETTINGS"]
        assert current.json()["memberships"][0]["course_name"] == "C++"

        replay = await client.post(
            "/api/v1/auth/moodle/callback",
            data={"assertion": assertion, "state": state},
        )
        assert replay.status_code == 403
        assert replay.json()["code"] == "INVALID_LOGIN_STATE"
    async with session_factory() as db:
        transaction = await db.scalar(select(LoginTransaction))
        nonce = await db.scalar(select(UsedLaunchNonce))
        assert transaction is not None and transaction.used_at is not None
        assert nonce is not None
        assert nonce.nonce_hash != payload["nonce"]


async def test_direct_moodle_launch_requires_configured_browser_origin(app_bundle):
    app, session_factory, settings = app_bundle
    secret = "direct-launch-shared-secret-with-more-than-32-characters"
    settings.moodle_launch_shared_secret = SecretStr(secret)
    settings.public_base_url = "http://testserver"
    async with session_factory() as db:
        db.add(
            LMSConnection(
                name="Direct Moodle",
                provider="MOODLE",
                base_url="https://moodle.example.test",
                config={"auth_mode": "BRIDGE"},
            )
        )
        await db.commit()

    def assertion(nonce: str) -> str:
        now = int(time.time())
        return _signed_assertion(
            {
                "iss": "https://moodle.example.test/",
                "sub": "direct-student",
                "aud": settings.public_base_url,
                "iat": now,
                "exp": now + 60,
                "nonce": nonce,
                "display_name": "Direct Student",
                "role": "STUDENT",
                "course_id": None,
            },
            secret,
        )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
        follow_redirects=False,
    ) as client:
        missing = await client.post(
            "/api/v1/auth/moodle/callback",
            data={"assertion": assertion("11" * 16)},
        )
        wrong = await client.post(
            "/api/v1/auth/moodle/callback",
            headers={"Origin": "https://attacker.example"},
            data={"assertion": assertion("22" * 16)},
        )
        accepted = await client.post(
            "/api/v1/auth/moodle/callback",
            headers={"Origin": "https://moodle.example.test"},
            data={"assertion": assertion("33" * 16)},
        )

    assert missing.status_code == 403
    assert missing.json()["code"] == "INVALID_LAUNCH_ORIGIN"
    assert wrong.status_code == 403
    assert wrong.json()["code"] == "INVALID_LAUNCH_ORIGIN"
    assert accepted.status_code == 303


async def test_course_import_projects_only_lms_confirmed_teacher_snapshot(app_bundle):
    app, session_factory, settings = app_bundle
    secret = "moodle-launch-shared-secret-for-tests"
    settings.moodle_launch_shared_secret = SecretStr(secret)
    settings.moodle_service_token = SecretStr("service-token")
    settings.admin_token = SecretStr("development-administrator-token")
    settings.public_base_url = "http://testserver"
    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            enabled=True,
            config={"auth_mode": "BRIDGE"},
        )
        db.add(connection)
        await db.flush()
        existing = Course(
            connection_id=connection.id,
            external_id="549",
            title="Existing",
            catalog_enabled=True,
        )
        db.add(existing)
        await db.commit()
        connection_id = connection.id

    def moodle_response(request: httpx.Request) -> httpx.Response:
        values = parse_qs(request.content.decode())
        function = values["wsfunction"][0]
        course_id = values["courseid"][0]
        if function == "local_programming_bridge_get_course_snapshot":
            return httpx.Response(
                200,
                json={
                    "revision": "course-v1",
                    "payload": {
                        "course": {
                            "id": int(course_id),
                            "fullname": f"Course {course_id}",
                            "shortname": f"C{course_id}",
                            "startdate": 0,
                            "enddate": 0,
                        },
                        "sections": [{"id": 1, "name": "Section", "number": 1, "visible": True}],
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "revision": "members-v1",
                "payload": {
                    "members": [
                        {
                            "user_id": "42",
                            "username": "teacher42",
                            "fullname": "Teacher 42",
                            "role": "TEACHER",
                            "suspended": False,
                            "groups": [{"id": "g1", "name": "Teachers"}],
                        }
                    ]
                },
            },
        )

    shared_http = httpx.AsyncClient(transport=httpx.MockTransport(moodle_response))
    app.state.http_client = shared_http
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            headers = await _csrf(client)
            started = await client.post(
                f"/api/v1/auth/lms/{connection_id}/start",
                headers=headers,
                json={
                    "course_id": "549",
                    "admin_token": "development-administrator-token",
                },
            )
            state = parse_qs(urlsplit(started.json()["redirect_url"]).query)["state"][0]
            now = int(time.time())
            launch = {
                "iss": "https://moodle.example.test/",
                "sub": "42",
                "aud": "http://testserver",
                "iat": now,
                "exp": now + 60,
                "nonce": "cd" * 16,
                "display_name": "Teacher 42",
                "role": "TEACHER",
                "course_id": 549,
                "state": state,
            }
            callback = await client.post(
                "/api/v1/auth/moodle/callback",
                data={"assertion": _signed_assertion(launch, secret), "state": state},
            )
            assert callback.status_code == 303
            settings.moodle_base_url = "https://another-moodle.example.test"
            disallowed = await client.post(
                "/api/v1/course-imports",
                headers=headers,
                json={"url": "https://moodle.example.test/course/view.php?id=550"},
            )
            assert disallowed.status_code == 422
            assert disallowed.json()["code"] == "COURSE_URL_NOT_RECOGNIZED"
            settings.moodle_base_url = "https://moodle.example.test"
            imported = await client.post(
                "/api/v1/course-imports",
                headers=headers,
                json={"url": "https://moodle.example.test/course/view.php?id=550"},
            )
            assert imported.status_code == 201, imported.text
            assert imported.json()["preview"]["title"] == "Course 550"
            job_id = imported.json()["id"]
            confirmed = await client.post(
                f"/api/v1/course-imports/{job_id}/confirm",
                headers=headers,
                json={},
            )
            assert confirmed.status_code == 200, confirmed.text
            assert confirmed.json()["state"] == "CONFIRMED"
            course_id = confirmed.json()["confirmed_course"]
            sections = await client.get(f"/api/v1/courses/{course_id}/sections")
            assert sections.status_code == 200
            assert sections.json()[0]["title"] == "Section"
            groups = await client.get(f"/api/v1/courses/{course_id}/groups")
            assert groups.status_code == 200
            assert groups.json()[0]["name"] == "Teachers"
            assert "external_id" not in groups.json()[0]
    finally:
        await shared_http.aclose()


def test_cli_connection_contract_has_no_secret_argv_and_seed_is_explicit():
    parser = build_parser()
    connection = parser.parse_args(
        [
            "bootstrap-connection",
            "--name",
            "Moodle",
            "--base-url",
            "https://moodle.example.test/",
        ]
    )
    demo = parser.parse_args(["seed-demo"])

    assert connection.provider == "MOODLE"
    assert not hasattr(connection, "service_token")
    assert demo.command == "seed-demo"
    assert _normalize_base_url("https://Moodle.Example.Test/", debug=False) == (
        "https://moodle.example.test"
    )
