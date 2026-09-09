from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.api.attempts import _moodle_preparation_error, _prepare_moodle_assignment
from app.core.credential_crypto import BROWSER_STATE_CREDENTIAL_KIND, encrypt_moodle_browser_state
from app.db.base import utcnow
from app.integrations.errors import IntegrationAssessmentUnavailable, IntegrationAttemptFinalized
from app.integrations.moodle_browser import MoodleBrowserClient
from app.models.attempts import Attempt
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.identity import ExternalPrincipal, LMSConnection, MoodleCredential
from app.models.integration import ExternalMapping
from app.models.tasks import (
    Assessment,
    AssessmentItem,
    AvailabilityRule,
    TaskBankItem,
    TaskVersion,
)
from app.services.common import DomainError


def _browser_state() -> dict[str, object]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "student-session",
                "domain": "moodle.example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def test_finalized_moodle_preparation_uses_student_terminal_error_contract() -> None:
    error = _moodle_preparation_error(
        IntegrationAttemptFinalized("attempt was completed directly in Moodle")
    )

    assert error.status_code == 409
    assert error.code == "LMS_ATTEMPT_FINALIZED"


@pytest.mark.asyncio
async def test_live_moodle_denial_after_failed_sync_does_not_create_local_assignment_attempt(
    app_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32

    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={"auth_mode": "PLUGINLESS", "pluginless_transport": "PLAYWRIGHT"},
        )
        db.add(connection)
        await db.flush()
        student = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="104684",
            display_name="Test Student",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add_all([student, course])
        await db.flush()
        membership = CourseMembership(
            course_id=course.id,
            principal_id=student.id,
            role="STUDENT",
            active=True,
        )
        group = CourseGroup(
            course_id=course.id,
            external_id="group-24",
            name="2.4 — подгруппа преподавателя",
            active=True,
        )
        db.add_all([membership, group])
        await db.flush()
        db.add(
            CourseMembershipGroup(
                coursemembership_id=membership.id,
                coursegroup_id=group.id,
            )
        )
        item = TaskBankItem(
            course_id=course.id,
            slug="moodle-assign-23461",
            created_by_id=student.id,
        )
        db.add(item)
        await db.flush()
        version = TaskVersion(
            item_id=item.id,
            number=1,
            title="Лабораторная работа №1",
            statement="Решите задачу.",
            language="CPP",
            language_standard="C++20",
            multi_file=True,
            starter_files=[{"path": "main.cpp", "content": ""}],
            build_profile="cpp-gcc-c++20-multi",
            max_score=Decimal("5.00"),
            content_hash="a" * 64,
            status="PUBLISHED",
            authored_by_id=student.id,
        )
        db.add(version)
        await db.flush()
        unconfirmed_source = {
            "title": True,
            "settings": True,
            "statement": False,
            "schedule": True,
            "duration": True,
            "grade": True,
            "attempt_policy": True,
            "statement_deferred": False,
        }
        assessment = Assessment(
            course_id=course.id,
            title="Лабораторная работа №1",
            status="PUBLISHED",
            opens_at=utcnow() - timedelta(days=30),
            closes_at=utcnow() - timedelta(days=1),
            attempt_limit=1,
            max_score=Decimal("5.00"),
            policy={
                "moodle_metadata_read_only": True,
                "moodle_source_confirmation": unconfirmed_source,
            },
            created_by_id=student.id,
        )
        db.add(assessment)
        await db.flush()
        db.add_all(
            [
                AssessmentItem(
                    assessment_id=assessment.id,
                    task_version_id=version.id,
                    points=Decimal("5.00"),
                ),
                AvailabilityRule(
                    assessment_id=assessment.id,
                    target_type="GROUP",
                    target_external_id=group.external_id,
                    allowed=True,
                    authored_by_id=student.id,
                ),
                ExternalMapping(
                    connection_id=connection.id,
                    local_type="Assessment",
                    local_id=assessment.id,
                    external_type="mod_assign",
                    external_id="23461",
                    metadata_json={
                        "module": "assign",
                        "cmid": 23461,
                        "submission_mode": "ASSIGN_FILE",
                        # Admission must still reach the live Moodle form when
                        # an unrelated/background course refresh has failed
                        # and its cached source confirmation is incomplete.
                        "sync_state": "ERROR",
                        "moodle_source_confirmation": unconfirmed_source,
                        "activity": {
                            "module": "assign",
                            "cmid": 23461,
                            "import_supported": True,
                            "answer_transport": "ASSIGN_FILE",
                        },
                    },
                ),
                MoodleCredential(
                    connection_id=connection.id,
                    principal_id=student.id,
                    kind=BROWSER_STATE_CREDENTIAL_KIND,
                    encrypted_secret=encrypt_moodle_browser_state(
                        _browser_state(),
                        settings,
                        connection_id=connection.id,
                        principal_id=student.id,
                    ),
                    status="ACTIVE",
                ),
            ]
        )
        await db.commit()

        async def deny(*_args: object, **_kwargs: object) -> None:
            raise IntegrationAssessmentUnavailable("student cannot edit this assignment")

        monkeypatch.setattr(MoodleBrowserClient, "prepare_assignment_submission", deny)

        with pytest.raises(DomainError) as denied:
            await _prepare_moodle_assignment(
                db,
                settings,
                assessment_id=assessment.id,
                principal_id=student.id,
            )

        assert denied.value.code == "MOODLE_ASSESSMENT_UNAVAILABLE"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(Attempt)
                .where(
                    Attempt.assessment_id == assessment.id,
                    Attempt.principal_id == student.id,
                )
            )
            == 0
        )

        mapping = await db.scalar(
            select(ExternalMapping).where(ExternalMapping.local_id == assessment.id)
        )
        assert mapping is not None
        await db.delete(mapping)
        await db.flush()
        with pytest.raises(DomainError) as missing_mapping:
            await _prepare_moodle_assignment(
                db,
                settings,
                assessment_id=assessment.id,
                principal_id=student.id,
            )
        assert missing_mapping.value.code == "MOODLE_RUNTIME_MAPPING_UNAVAILABLE"
