from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.models.attempts import Attempt
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.identity import LMSConnection
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment, AvailabilityRule
from app.services.common import DomainError
from app.services.moodle_quiz_runtime import resolve_moodle_quiz_context
from app.services.workspace import start_attempt
from tests.test_auth_courses_authoring import _dev_login


async def _seed_activities(sessions, session, *, module="quiz"):
    course_id = uuid.UUID(session["memberships"][0]["course_id"])
    principal_id = uuid.UUID(session["principal"]["id"])
    async with sessions() as db, db.begin():
        course = await db.get(Course, course_id)
        course.external_id = "549"
        connection = await db.get(LMSConnection, course.connection_id)
        connection.provider = "MOODLE"
        connection.config = {"auth_mode": "PLUGINLESS", "pluginless_transport": "PLAYWRIGHT"}
        membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == course_id,
                CourseMembership.principal_id == principal_id,
            )
        )
        group = CourseGroup(course_id=course_id, external_id="manual-sync-group", name="Test")
        db.add(group)
        await db.flush()
        db.add(CourseMembershipGroup(coursemembership_id=membership.id, coursegroup_id=group.id))
        rows = {}
        for index, state in enumerate(["CURRENT", "ERROR", "MISSING_IN_MOODLE"]):
            assessment = Assessment(
                course_id=course_id,
                title=f"Work {state}",
                created_by_id=principal_id,
                status="PUBLISHED",
                policy={"moodle_metadata_read_only": True},
            )
            db.add(assessment)
            await db.flush()
            db.add(
                AvailabilityRule(
                    assessment_id=assessment.id,
                    target_type="GROUP",
                    target_external_id=group.external_id,
                    allowed=True,
                    authored_by_id=principal_id,
                )
            )
            db.add(
                ExternalMapping(
                    connection_id=connection.id,
                    local_type="Assessment",
                    local_id=assessment.id,
                    external_type=f"mod_{module}",
                    external_id=str(500 + index),
                    metadata_json={
                        "managed_by": "MOODLE_ACTIVITY_IMPORT",
                        "module": module,
                        "cmid": 500 + index,
                        "sync_state": state,
                    },
                )
            )
            rows[state] = assessment.id

        # A foreign connection's marker must not hide another LMS's local work.
        foreign = LMSConnection(
            name="Other Moodle", provider="MOODLE", base_url="https://other.example.test"
        )
        db.add(foreign)
        await db.flush()
        db.add(
            ExternalMapping(
                connection_id=foreign.id,
                local_type="Assessment",
                local_id=rows["CURRENT"],
                external_type=f"mod_{module}",
                external_id="501",
                metadata_json={"sync_state": "MISSING_IN_MOODLE"},
            )
        )
        retained = Attempt(assessment_id=rows["MISSING_IN_MOODLE"], principal_id=principal_id)
        db.add(retained)
        await db.flush()
        return course_id, principal_id, rows, retained.id


@pytest.mark.parametrize("role", ["TEACHER", "STUDENT"])
async def test_course_list_hides_only_confirmed_missing_activities_and_keeps_audit(
    app_bundle, role
):
    app, sessions, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _dev_login(client, role=role)
        course_id, _, rows, retained_id = await _seed_activities(sessions, session)
        response = await client.get(f"/api/v1/courses/{course_id}/assessments")
        assert response.status_code == 200, response.text
        listed = {item["id"] for item in response.json()}
        assert str(rows["CURRENT"]) in listed
        assert str(rows["ERROR"]) in listed  # An outage is not proof of deletion.
        assert str(rows["MISSING_IN_MOODLE"]) not in listed

        direct = await client.get(f"/api/v1/assessments/{rows['MISSING_IN_MOODLE']}")
        assert direct.status_code == (200 if role == "TEACHER" else 404)
        async with sessions() as db, db.begin():
            assert await db.get(Assessment, rows["MISSING_IN_MOODLE"]) is not None
            attempt = await db.get(Attempt, retained_id)
            assert attempt is not None and attempt.state == "ACTIVE"
            mapping = await db.scalar(
                select(ExternalMapping).where(
                    ExternalMapping.local_id == rows["MISSING_IN_MOODLE"],
                )
            )
            mapping.metadata_json = {**mapping.metadata_json, "sync_state": "CURRENT"}
        # A later explicit successful refresh can restore the same work.
        restored = await client.get(f"/api/v1/courses/{course_id}/assessments")
        assert str(rows["MISSING_IN_MOODLE"]) in {item["id"] for item in restored.json()}


async def test_missing_activity_cannot_be_published_or_offer_publication_targets(app_bundle):
    app, sessions, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _dev_login(client, role="TEACHER")
        _, _, rows, _ = await _seed_activities(sessions, session)
        assessment_id = rows["MISSING_IN_MOODLE"]
        headers = {"X-CSRFToken": client.cookies[settings.csrf_cookie_name]}
        publication = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish", headers=headers, json={}
        )
        targets = await client.get(f"/api/v1/assessments/{assessment_id}/publication-targets")
        assert publication.status_code == targets.status_code == 409
        assert (
            publication.json()["code"] == targets.json()["code"] == "MOODLE_ASSESSMENT_UNAVAILABLE"
        )
        validation = await client.post(
            f"/api/v1/assessments/{assessment_id}/validate", headers=headers, json={}
        )
        assert validation.status_code == 200
        assert any(
            issue["code"] == "MOODLE_ASSESSMENT_UNAVAILABLE"
            for issue in validation.json()["errors"]
        )


async def test_local_ai_toggle_does_not_rewrite_manually_imported_activity(app_bundle):
    app, sessions, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _dev_login(client, role="TEACHER")
        _, _, rows, _ = await _seed_activities(sessions, session)
        async with sessions() as db:
            mapping = await db.scalar(select(ExternalMapping).where(
                ExternalMapping.local_id == rows["CURRENT"],
                ExternalMapping.external_id == "500",
            ))
            before = dict(mapping.metadata_json)
        response = await client.patch(
            f"/api/v1/assessments/{rows['CURRENT']}",
            headers={"X-CSRFToken": client.cookies[settings.csrf_cookie_name]},
            json={"student_ai_enabled": True},
        )
        assert response.status_code == 200, response.text
        async with sessions() as db:
            mapping = await db.scalar(select(ExternalMapping).where(
                ExternalMapping.local_id == rows["CURRENT"],
                ExternalMapping.external_id == "500",
            ))
            assert mapping is not None
            assert mapping.metadata_json == before


@pytest.mark.parametrize("module", ["quiz", "assign"])
async def test_missing_activity_rejects_direct_start_without_deleting_existing_attempt(
    app_bundle, module
):
    app, sessions, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        session = await _dev_login(client, role="STUDENT")
        _, principal_id, rows, retained_id = await _seed_activities(
            sessions, session, module=module
        )
        missing = rows["MISSING_IN_MOODLE"]
        response = await client.post(
            f"/api/v1/assessments/{missing}/attempts",
            headers={"X-CSRFToken": client.cookies[settings.csrf_cookie_name]},
            json={},
        )
        assert response.status_code == 409, response.text
        assert response.json()["code"] == "MOODLE_ASSESSMENT_UNAVAILABLE"
        async with sessions() as db:
            with pytest.raises(DomainError, match="removed from Moodle"):
                await start_attempt(db, assessment_id=missing, principal_id=principal_id)
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(Attempt)
                    .where(Attempt.assessment_id == missing)
                )
                == 1
            )
            assert (await db.get(Attempt, retained_id)).state == "ACTIVE"
            if module == "quiz":
                # Resolve remains neutral for immutable queued delivery/audit.
                assert await resolve_moodle_quiz_context(db, missing) is not None
                assert await resolve_moodle_quiz_context(db, rows["ERROR"]) is not None
