from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.authoring import _assessment_student_read, _student_available, _validate_assessment
from app.api.courses import _project_course
from app.auth.sessions import create_principal_session
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    decrypt_moodle_browser_state,
    encrypt_moodle_browser_state,
)
from app.db.base import utcnow
from app.integrations.errors import IntegrationUnavailable
from app.integrations.moodle import CourseDiscovery
from app.integrations.moodle_browser import MoodleBrowserClient, MoodleBrowserDiscoveryResult
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.models.attempts import Attempt
from app.models.courses import (
    Course,
    CourseGroup,
    CourseImportJob,
    CourseMembership,
    CourseMembershipGroup,
)
from app.models.enums import SyncOutboxState
from app.models.identity import (
    AdminElevation,
    ExternalPrincipal,
    LMSConnection,
    MoodleCredential,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.tasks import (
    Assessment,
    AssessmentItem,
    AvailabilityRule,
    TaskBankItem,
    TaskVersion,
)
from app.services.common import DomainError
from app.services.moodle_materialization import (
    classify_moodle_assessment,
    materialize_moodle_activity_drafts,
)
from app.services.policy import MembershipContext, ensure_assessment_available


def _state(marker: str) -> dict[str, object]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": marker,
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


@pytest.mark.asyncio
async def test_initial_partial_course_projection_merges_returned_group_links(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    async with session_factory() as db, db.begin():
        connection = LMSConnection(
            name="Large Moodle",
            provider="MOODLE",
            base_url="https://large-moodle.example.test",
            enabled=True,
        )
        db.add(connection)
        await db.flush()
        actor = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="teacher-1",
            display_name="Коваленко Алексей",
        )
        db.add(actor)
        await db.flush()

        course = await _project_course(
            db,
            connection=connection,
            preview={
                "external_id": "549",
                "title": "C++",
                "external_revision": "course-partial",
                "membership_revision": "roster-partial",
                "sections": [],
                "groups": [
                    {
                        "external_id": "group-24",
                        "name": "2.4 подгруппа Коваленко А.С.",
                    }
                ],
                "membership_snapshot": {
                    "complete": False,
                    "members": [
                        {
                            "user_id": "student-1",
                            "display_name": "Студент Один",
                            "role": "STUDENT",
                            "groups": [
                                {
                                    "external_id": "group-24",
                                    "name": "2.4 подгруппа Коваленко А.С.",
                                }
                            ],
                        }
                    ],
                },
            },
            capabilities={"roster": False, "groups": False},
            created_by_id=actor.id,
            actor_external_subject=actor.external_subject,
        )

        membership = await db.scalar(
            select(CourseMembership)
            .join(ExternalPrincipal, ExternalPrincipal.id == CourseMembership.principal_id)
            .where(
                CourseMembership.course_id == course.id,
                ExternalPrincipal.external_subject == "student-1",
            )
        )
        assert membership is not None
        linked_group = await db.scalar(
            select(CourseGroup)
            .join(
                CourseMembershipGroup,
                CourseMembershipGroup.coursegroup_id == CourseGroup.id,
            )
            .where(CourseMembershipGroup.coursemembership_id == membership.id)
        )
        assert linked_group is not None
        assert linked_group.external_id == "group-24"


async def test_published_moodle_work_visibility_depends_on_group_not_sync_confirmation(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    await _seed_teacher(session_factory, settings)
    async with session_factory() as db, db.begin():
        membership = await db.scalar(
            select(CourseMembership).where(CourseMembership.role == "TEACHER")
        )
        assert membership is not None
        principal = await db.get(ExternalPrincipal, membership.principal_id)
        course = await db.get(Course, membership.course_id)
        assert principal is not None and course is not None
        group = CourseGroup(course_id=course.id, external_id="legacy-group", name="Legacy")
        db.add(group)
        await db.flush()
        db.add(
            CourseMembershipGroup(
                coursemembership_id=membership.id,
                coursegroup_id=group.id,
            )
        )
        assessment = Assessment(
            course_id=course.id,
            type="LAB",
            title="Legacy guessed Moodle work",
            instructions="Legacy placeholder",
            max_score=Decimal("10.00"),
            status="PUBLISHED",
            policy={"moodle_metadata_read_only": True},
            created_by_id=principal.id,
        )
        db.add(assessment)
        await db.flush()
        rule = AvailabilityRule(
            assessment_id=assessment.id,
            target_type="GROUP",
            target_external_id=group.external_id,
            allowed=True,
            authored_by_id=principal.id,
        )
        db.add(rule)
        await db.flush()
        context = MembershipContext(
            membership=membership,
            principal=principal,
            course=course,
            group_external_ids=frozenset({group.external_id}),
        )
        # Publication is a local group decision. Incomplete discovery metadata
        # must not make the card disappear; exact admission is checked against
        # the student's live Moodle form when they open it.
        assert await _student_available(db, assessment, context) is True
        await ensure_assessment_available(db, assessment, context)
        student_read = await _assessment_student_read(db, assessment, context)
        assert student_read.requires_live_lms_preparation is True
        assert "policy" not in student_read.model_dump()

        await db.delete(rule)
        await db.flush()
        assert await _student_available(db, assessment, context) is False
        with pytest.raises(DomainError) as unassigned:
            await ensure_assessment_available(db, assessment, context)
        assert unassigned.value.code == "ASSESSMENT_NOT_ASSIGNED"


async def test_unconfirmed_moodle_fields_are_not_replaced_with_local_defaults(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    await _seed_teacher(session_factory, settings)
    async with session_factory() as db, db.begin():
        course = await db.scalar(select(Course).where(Course.external_id == "100"))
        principal = await db.scalar(select(ExternalPrincipal))
        assert course is not None and principal is not None
        created = await materialize_moodle_activity_drafts(
            db,
            course=course,
            created_by_id=principal.id,
            activities=[
                {
                    "cmid": 9001,
                    "module": "quiz",
                    "name": "Лабораторная: неподтвержденные настройки",
                    "description": "Условие присутствует, но provenance отсутствует.",
                    "opens_at_epoch": 1_800_000_000,
                    "due_at_epoch": 1_800_003_600,
                    "duration_seconds": 3_600,
                    "grade_max": 10,
                    "attempt_limit": 1,
                    "quiz_grading_method": "LAST",
                    "quiz_grading_method_confirmed": True,
                    "question_count": 1,
                    "essay_question_count": 1,
                    # A connector cannot opt into deferred statements without
                    # also proving that the complete random pool is Essay.
                    "random_question_count": 1,
                    "statement_deferred": True,
                    "import_supported": True,
                    "answer_transport": "ESSAY_ONLINE_TEXT",
                }
            ],
        )
        assessment = await db.scalar(select(Assessment).where(Assessment.course_id == course.id))
        version = await db.scalar(select(TaskVersion))
        mapping = await db.scalar(
            select(ExternalMapping).where(ExternalMapping.external_id == "9001")
        )
        assert created == 1
        assert assessment is not None and version is not None and mapping is not None
        assert assessment.max_score == Decimal("0.00")
        assert version.max_score == Decimal("0.00")
        assert assessment.attempt_limit is None
        assert assessment.duration_seconds is None
        assert assessment.opens_at is None and assessment.closes_at is None
        assert mapping.metadata_json["sync_state"] == "MOODLE_SOURCE_UNCONFIRMED"
        errors, warnings = await _validate_assessment(db, assessment)
        assert "MOODLE_SOURCE_UNCONFIRMED" not in {issue.code for issue in errors}
        assert "MOODLE_SOURCE_UNCONFIRMED" in {issue.code for issue in warnings}
        # Missing provenance must not mask genuine structural blockers. A newly
        # discovered activity whose grade was never confirmed still has no safe
        # positive score and remains impossible to publish.
        assert {"POSITIVE_SCORE_REQUIRED", "INVALID_POINTS"} <= {
            issue.code for issue in errors
        }

        refreshed = await materialize_moodle_activity_drafts(
            db,
            course=course,
            created_by_id=principal.id,
            activities=[
                {
                    "cmid": 9001,
                    "module": "quiz",
                    "name": "Лабораторная: случайный Essay",
                    "description": "",
                    "opens_at_epoch": 1_800_000_000,
                    "due_at_epoch": 1_800_003_600,
                    "duration_seconds": 3_600,
                    "grade_max": 10,
                    "attempt_limit": 1,
                    "quiz_grading_method": "LAST",
                    "quiz_grading_method_confirmed": True,
                    "question_count": 1,
                    "essay_question_count": 0,
                    "random_question_count": 1,
                    "random_essay_confirmed": True,
                    "statement_deferred": True,
                    "import_supported": True,
                    "title_confirmed": True,
                    "settings_confirmed": True,
                    "statement_confirmed": False,
                    "schedule_confirmed": True,
                    "duration_confirmed": True,
                    "grade_confirmed": True,
                    "attempt_policy_confirmed": True,
                }
            ],
        )
        assert refreshed == 0
        assert assessment.instructions == ""
        assert version.statement == ""
        assert version.ai_policy["statement_deferred"] is True
        assert mapping.metadata_json["sync_state"] == "ANSWER_TRANSPORT_UNSUPPORTED"
        errors, warnings = await _validate_assessment(db, assessment)
        assert "MOODLE_SOURCE_UNCONFIRMED" not in {issue.code for issue in errors}
        assert "MOODLE_SOURCE_UNCONFIRMED" not in {issue.code for issue in warnings}


async def test_assignment_publication_gate_validates_starter_shape_and_file_types(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    await _seed_teacher(session_factory, settings)
    async with session_factory() as db, db.begin():
        course = await db.scalar(select(Course).where(Course.external_id == "100"))
        principal = await db.scalar(select(ExternalPrincipal))
        assert course is not None and principal is not None
        assessment = Assessment(
            course_id=course.id,
            title="Assignment publication contract",
            max_score=Decimal("10.00"),
            multi_file=False,
            created_by_id=principal.id,
        )
        item = TaskBankItem(
            scope="COURSE",
            course_id=course.id,
            slug="assignment-publication-contract",
            created_by_id=principal.id,
        )
        db.add_all([assessment, item])
        await db.flush()
        version = TaskVersion(
            item_id=item.id,
            number=1,
            title="Starter",
            statement="Write a program",
            multi_file=False,
            starter_files=[{"path": "main.cpp", "content": ""}],
            max_score=Decimal("10.00"),
            content_hash="a" * 64,
            status="PUBLISHED",
            authored_by_id=principal.id,
        )
        db.add(version)
        await db.flush()
        db.add(
            AssessmentItem(
                assessment_id=assessment.id,
                task_version_id=version.id,
                points=Decimal("10.00"),
            )
        )
        mapping = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="mod_assign",
            external_id="23461",
            metadata_json={
                "module": "assign",
                "submission_mode": "ASSIGN_ONLINE_TEXT",
                "sync_state": "CURRENT",
                "activity": {"import_supported": True},
            },
        )
        db.add(mapping)
        await db.flush()

        errors, _ = await _validate_assessment(db, assessment)
        assert "MOODLE_ONLINE_TEXT_SINGLE_TRANSLATION_UNIT_REQUIRED" not in {
            issue.code for issue in errors
        }

        version.starter_files = [
            {"path": "main.cpp", "content": ""},
            {"path": "input.txt", "content": "1\n"},
        ]
        errors, _ = await _validate_assessment(db, assessment)
        assert "MOODLE_ONLINE_TEXT_SINGLE_TRANSLATION_UNIT_REQUIRED" not in {
            issue.code for issue in errors
        }

        version.starter_files = [{"path": "main.cpp", "content": ""}]
        mapping.metadata_json = {
            **mapping.metadata_json,
            "submission_mode": "ASSIGN_FILE",
            "activity": {
                "import_supported": True,
                "max_submission_files": 2,
                "file_types_confirmed": True,
                "accepted_file_types": ".zip",
            },
        }
        errors, _ = await _validate_assessment(db, assessment)
        assert "MOODLE_ASSIGNMENT_FILE_TYPE_FORBIDDEN" not in {issue.code for issue in errors}


def _discovery(
    external_id: str,
    *,
    revision: int,
    activities: list[dict[str, object]] | None = None,
) -> CourseDiscovery:
    return CourseDiscovery(
        external_id=external_id,
        preview={
            "external_id": external_id,
            "title": f"C++ course revision {revision}",
            "short_name": "CPP",
            "external_revision": f"{revision:064x}",
            "membership_revision": f"{revision + 100:064x}",
            "starts_at_epoch": 0,
            "ends_at_epoch": 0,
            "sections": [
                {
                    "external_id": "10",
                    "title": "Labs",
                    "position": 0,
                    "visible": True,
                    "activities": activities or [],
                }
            ],
            "groups": [],
            "membership_snapshot": {
                "complete": True,
                "members": [
                    {
                        "user_id": "42",
                        "display_name": "Ada Teacher",
                        "email": "ada@example.test",
                        "suspended": False,
                        "role": "TEACHER",
                        "roles": ["TEACHER"],
                        "groups": [],
                    }
                ],
            },
        },
        capabilities={
            "roster": True,
            "groups": True,
            "grades": False,
            "comments": False,
            "checkpoints": False,
            "task_bank_mirror": False,
            "native_question_bank_write": False,
        },
    )


async def _seed_teacher(
    session_factory,
    settings,
    *,
    credential_mode: str = "active",
    elevated: bool = True,
) -> tuple[LMSConnection, str, MoodleCredential | None]:
    settings.moodle_base_url = "https://moodle.example.test"
    async with session_factory() as db:
        connection = LMSConnection(
            name="MMCS Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            enabled=True,
            config={
                "auth_mode": "PLUGINLESS",
                "pluginless_transport": "PLAYWRIGHT",
            },
        )
        db.add(connection)
        await db.flush()
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Ada Teacher",
        )
        db.add(principal)
        await db.flush()
        teacher_token = TeacherAccessToken(
            public_id=f"test{principal.id.hex[:12]}",
            label="Ada Teacher",
            secret_hash="$argon2id$v=19$m=8,t=1,p=1$dGVzdHRlc3R0ZXN0dGVzdA$ZmFrZWhhc2g",
            created_by_id=principal.id,
        )
        db.add(teacher_token)
        await db.flush()
        db.add(
            TeacherTokenGrant(
                token_id=teacher_token.id,
                principal_id=principal.id,
            )
        )
        access_course = Course(
            connection_id=connection.id,
            external_id="100",
            title="Existing teacher course",
            sync_status="CURRENT",
            catalog_enabled=True,
        )
        db.add(access_course)
        await db.flush()
        db.add(
            CourseMembership(
                course_id=access_course.id,
                principal_id=principal.id,
                role="TEACHER",
                active=True,
            )
        )
        credential: MoodleCredential | None = None
        if credential_mode != "missing":
            credential = MoodleCredential(
                connection_id=connection.id,
                principal_id=principal.id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _state("initial-session"),
                    settings,
                    connection_id=connection.id,
                    principal_id=principal.id,
                ),
                status="ACTIVE",
            )
            if credential_mode == "invalid_shape":
                credential.encrypted_secret = encrypt_moodle_browser_state(
                    {"cookies": "not-a-list", "origins": []},
                    settings,
                    connection_id=connection.id,
                    principal_id=principal.id,
                )
            elif credential_mode == "expired":
                credential.expires_at = utcnow() - timedelta(seconds=1)
            elif credential_mode == "busy":
                credential.lease_owner = "another-request"
                credential.lease_expires_at = utcnow() + timedelta(minutes=2)
            elif credential_mode == "stale":
                credential.lease_owner = "crashed-request"
                credential.lease_expires_at = utcnow() - timedelta(seconds=1)
            db.add(credential)
        local_session = await create_principal_session(db, principal.id, settings)
        if elevated:
            now = utcnow()
            db.add(
                AdminElevation(
                    principal_id=principal.id,
                    session_key=local_session.token_hash,
                    granted_at=now,
                    last_used_at=now,
                    expires_at=now + timedelta(minutes=30),
                    absolute_expires_at=now + timedelta(hours=1),
                    request_ip_prefix="",
                )
            )
        await db.commit()
        return connection, local_session.bearer, credential


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


def test_moodle_activity_classifier_is_explicit_and_excludes_archives() -> None:
    assert classify_moodle_assessment("Задание 1", "Лабораторные работы") == "LAB"
    assert classify_moodle_assessment("Работа 2", "Самостоятельные") == "INDEPENDENT"
    assert classify_moodle_assessment("Проверочная работа", "Тема 3") == "CONTROL"
    assert classify_moodle_assessment("Итог", "Экзамен") == "EXAM"
    assert classify_moodle_assessment("Экзамен 2024", "АРХИВ") is None
    assert classify_moodle_assessment("Тест 1", "Тесты") is None
    assert classify_moodle_assessment("Вариант 1", "Индивидуальные") is None


async def test_closed_moodle_quiz_publishes_to_group_and_defers_student_access_to_moodle(
    app_bundle,
) -> None:
    app, session_factory, settings = app_bundle
    connection, bearer, _credential = await _seed_teacher(session_factory, settings)
    now = utcnow().replace(microsecond=0)
    global_open = now - timedelta(days=30)
    # Old Moodle quizzes may retain an equal, unusable global boundary while
    # an explicit user override legitimately reopens the work for one student.
    global_close = global_open
    override_close = now + timedelta(days=30)

    async with session_factory() as db, db.begin():
        course = await db.scalar(
            select(Course).where(
                Course.connection_id == connection.id,
                Course.external_id == "100",
            )
        )
        teacher = await db.scalar(
            select(ExternalPrincipal).where(
                ExternalPrincipal.connection_id == connection.id,
                ExternalPrincipal.external_subject == "42",
            )
        )
        assert course is not None and teacher is not None
        student = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="104684",
            display_name="Test User",
        )
        other_student = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="104685",
            display_name="Other Student",
        )
        db.add_all([student, other_student])
        await db.flush()
        student_membership = CourseMembership(
            course_id=course.id,
            principal_id=student.id,
            role="STUDENT",
            active=True,
        )
        other_membership = CourseMembership(
            course_id=course.id,
            principal_id=other_student.id,
            role="STUDENT",
            active=True,
        )
        db.add_all([student_membership, other_membership])
        await db.flush()
        teacher_membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == course.id,
                CourseMembership.principal_id == teacher.id,
                CourseMembership.role == "TEACHER",
            )
        )
        assert teacher_membership is not None
        group = CourseGroup(
            course_id=course.id,
            external_id="group-24",
            name="2.4 — подгруппа преподавателя",
            active=True,
        )
        db.add(group)
        await db.flush()
        db.add_all(
            [
                CourseMembershipGroup(
                    coursemembership_id=teacher_membership.id,
                    coursegroup_id=group.id,
                ),
                CourseMembershipGroup(
                    coursemembership_id=student_membership.id,
                    coursegroup_id=group.id,
                ),
            ]
        )
        created = await materialize_moodle_activity_drafts(
            db,
            course=course,
            created_by_id=teacher.id,
            activities=[
                {
                    "cmid": 30354,
                    "instance_id": 5001,
                    "module": "quiz",
                    "name": "Самостоятельная работа №1",
                    # Moodle binds a concrete question only after this student's
                    # attempt starts; discovery proved the whole random pool is Essay.
                    "description": "",
                    "visible": True,
                    "user_visible": True,
                    "url": "https://moodle.example.test/mod/quiz/view.php?id=30354",
                    "opens_at_epoch": int(global_open.timestamp()),
                    "due_at_epoch": int(global_close.timestamp()),
                    "cutoff_at_epoch": 0,
                    "grade_max": 10.0,
                    "duration_seconds": 3_600,
                    "attempt_limit": 1,
                    "quiz_grading_method": "LAST",
                    "quiz_grading_method_confirmed": True,
                    "question_count": 1,
                    "essay_question_count": 0,
                    "random_question_count": 1,
                    "random_essay_confirmed": True,
                    "statement_deferred": True,
                    "import_supported": True,
                    "title_confirmed": True,
                    "settings_confirmed": True,
                    "statement_confirmed": False,
                    "schedule_confirmed": True,
                    "duration_confirmed": True,
                    "grade_confirmed": True,
                    "attempt_policy_confirmed": True,
                    "user_overrides_confirmed": True,
                    "user_overrides": [
                        {
                            "override_id": 8123,
                            "user_id": "104684",
                            "display_name": "Test User",
                            "opens_at_epoch": 0,
                            "due_at_epoch": int(override_close.timestamp()),
                            "cutoff_at_epoch": 0,
                            "opens_at_overridden": False,
                            "due_at_overridden": True,
                            "cutoff_at_overridden": False,
                            "duration_seconds": 1_800,
                            "duration_overridden": True,
                            "attempt_limit": 10,
                            "attempt_limit_overridden": True,
                            "attempt_limit_unlimited": False,
                            "confirmed": True,
                        }
                    ],
                }
            ],
        )
        assert created == 1
        assessment = await db.scalar(
            select(Assessment).where(
                Assessment.course_id == course.id,
                Assessment.title == "Самостоятельная работа №1",
            )
        )
        assert assessment is not None
        version = await db.scalar(
            select(TaskVersion)
            .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
            .where(AssessmentItem.assessment_id == assessment.id)
        )
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.local_id == assessment.id,
                ExternalMapping.external_id == "30354",
            )
        )
        assert version is not None and mapping is not None
        assert assessment.instructions == ""
        assert version.statement == ""
        assert version.ai_policy["statement_deferred"] is True
        assert assessment.policy["moodle_source_confirmation"] == {
            "title": True,
            "settings": True,
            "statement": False,
            "schedule": True,
            "duration": True,
            "grade": True,
            "attempt_policy": True,
            "statement_deferred": True,
        }
        assert mapping.metadata_json["sync_state"] == "ANSWER_TRANSPORT_UNSUPPORTED"
        # Simulate a Moodle upgrade after a successful materialization: the
        # connector still has the stable activity mapping and the last valid
        # task/score, but one discovery field is temporarily unconfirmed.
        source_confirmation = dict(assessment.policy["moodle_source_confirmation"])
        source_confirmation["duration"] = False
        assessment.policy = {
            **assessment.policy,
            "moodle_source_confirmation": source_confirmation,
        }
        mapping.metadata_json = {
            **mapping.metadata_json,
            "moodle_source_confirmation": source_confirmation,
            "sync_state": "MOODLE_SOURCE_UNCONFIRMED",
        }
        assessment_id = assessment.id
        student_id = student.id
        other_student_id = other_student.id
        group_id = group.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        headers = await _csrf(client)
        targets = await client.get(f"/api/v1/assessments/{assessment_id}/publication-targets")
        assert targets.status_code == 200, targets.text
        assert targets.json()["overrides_confirmed"] is True
        assert targets.json()["principals"] == []
        assert targets.json()["groups"] == [
            {
                "id": str(group_id),
                "external_id": "group-24",
                "name": "2.4 — подгруппа преподавателя",
            }
        ]

        validation = await client.post(
            f"/api/v1/assessments/{assessment_id}/validate",
            headers=headers,
            json={},
        )
        assert validation.status_code == 200, validation.text
        assert "MOODLE_SOURCE_UNCONFIRMED" not in {
            row["code"] for row in validation.json()["errors"]
        }
        assert "MOODLE_SOURCE_UNCONFIRMED" in {
            row["code"] for row in validation.json()["warnings"]
        }

        individual = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish",
            headers=headers,
            json={"principal_ids": [str(student_id)]},
        )
        assert individual.status_code == 422
        assert individual.json()["code"] == "INDIVIDUAL_PUBLICATION_UNSUPPORTED"

        published = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish",
            headers=headers,
            json={"group_ids": [str(group_id)]},
        )
        assert published.status_code == 200, published.text
        assert published.json()["status"] == "PUBLISHED"
        rules = published.json()["availability_rules"]
        assert len(rules) == 1
        assert rules[0]["target_type"] == "GROUP"
        assert rules[0]["target_external_id"] == "group-24"
        assert rules[0]["duration_seconds"] is None
        assert rules[0]["attempt_limit"] is None

        # Freshness/provenance is non-blocking, but the stable activity identity
        # remains mandatory even when only the published group list is changed.
        async with session_factory() as db:
            mapping = await db.scalar(
                select(ExternalMapping).where(
                    ExternalMapping.local_id == assessment_id,
                    ExternalMapping.external_id == "30354",
                )
            )
            assert mapping is not None
            mapping.external_id = "invalid-cmid"
            await db.commit()
        invalid_mapping = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish",
            headers=headers,
            json={"group_ids": [str(group_id)]},
        )
        assert invalid_mapping.status_code == 409, invalid_mapping.text
        assert invalid_mapping.json()["code"] == "ASSESSMENT_INVALID"
        assert invalid_mapping.json()["errors"][0]["code"] == (
            "MOODLE_RUNTIME_MAPPING_UNAVAILABLE"
        )
        async with session_factory() as db:
            mapping = await db.scalar(
                select(ExternalMapping).where(
                    ExternalMapping.local_id == assessment_id,
                    ExternalMapping.external_id == "invalid-cmid",
                )
            )
            assert mapping is not None
            mapping.external_id = "30354"
            await db.commit()

    async with session_factory() as db:
        assessment = await db.get(Assessment, assessment_id)
        student = await db.get(ExternalPrincipal, student_id)
        membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == assessment.course_id,
                CourseMembership.principal_id == student_id,
            )
        )
        course = await db.get(Course, assessment.course_id)
        assert assessment is not None and student is not None
        assert membership is not None and course is not None
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.local_id == assessment.id,
                ExternalMapping.external_id == "30354",
            )
        )
        assert mapping is not None
        mapping.metadata_json = {
            **mapping.metadata_json,
            "sync_state": "ERROR",
        }
        await db.flush()
        student_context = MembershipContext(
            membership=membership,
            principal=student,
            course=course,
            group_external_ids=frozenset({"group-24"}),
        )
        # A failed background refresh must not make an already-published
        # group work disappear. Moodle decides this exact student's access
        # only when the student opens the work.
        assert await _student_available(db, assessment, student_context) is True
        effective = await ensure_assessment_available(
            db,
            assessment,
            student_context,
        )
        assert effective.closes_at is not None
        assert effective.closes_at.replace(tzinfo=UTC) == global_close.replace(microsecond=0)
        assert effective.duration_seconds == 3_600
        assert effective.attempt_limit == 1

        other_student = await db.get(ExternalPrincipal, other_student_id)
        other_membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == assessment.course_id,
                CourseMembership.principal_id == other_student_id,
            )
        )
        assert other_student is not None and other_membership is not None
        with pytest.raises(DomainError) as unavailable:
            await ensure_assessment_available(
                db,
                assessment,
                MembershipContext(
                    membership=other_membership,
                    principal=other_student,
                    course=course,
                    group_external_ids=frozenset(),
                ),
            )
        assert unavailable.value.code == "ASSESSMENT_NOT_ASSIGNED"


async def test_playwright_course_import_and_manual_sync_refresh_encrypted_state(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    connection, bearer, credential = await _seed_teacher(session_factory, settings)
    assert credential is not None
    calls = 0

    async def discover_course(
        browser: MoodleBrowserClient,
        external_id: str,
        actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        nonlocal calls
        calls += 1
        assert external_id == "549"
        assert actor_external_subject == "42"
        assert interactive is True
        expected_state = "initial-session" if calls == 1 else "refresh-1"
        assert browser.storage_state == _state(expected_state)
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=calls),
            storage_state=_state(f"refresh-{calls}"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover_course)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        assert imported.json()["preview"]["title"] == "C++ course revision 1"

        async with session_factory() as db:
            refreshed = await db.get(MoodleCredential, credential.id)
            assert refreshed is not None
            assert refreshed.revision == 2
            assert refreshed.lease_owner is None and refreshed.lease_expires_at is None
            assert refreshed.last_used_at is not None
            assert decrypt_moodle_browser_state(
                refreshed.encrypted_secret,
                settings,
                connection_id=connection.id,
                principal_id=refreshed.principal_id,
            ) == _state("refresh-1")

        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=await _csrf(client),
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text
        synced = await client.post(
            f"/api/v1/courses/{confirmed.json()['confirmed_course']}/sync",
            headers=await _csrf(client),
            json={},
        )
        assert synced.status_code == 200, synced.text
        assert synced.json()["title"] == "C++ course revision 2"

        async with session_factory() as db:
            manual_receipt = await db.scalar(
                select(SyncOutbox).where(
                    SyncOutbox.course_id == uuid.UUID(confirmed.json()["confirmed_course"]),
                    SyncOutbox.event_type == "course.sync",
                    SyncOutbox.state == SyncOutboxState.DELIVERED.value,
                )
            )
        assert manual_receipt is not None
        assert manual_receipt.payload.get("foreground") is True
        assert manual_receipt.receipt.get("foreground") is True

        async def unavailable(*_args, **_kwargs) -> MoodleBrowserDiscoveryResult:
            raise IntegrationUnavailable("bounded connector outage")

        monkeypatch.setattr(MoodleBrowserClient, "discover_course", unavailable)
        failed = await client.post(
            f"/api/v1/courses/{confirmed.json()['confirmed_course']}/sync",
            headers=await _csrf(client),
            json={},
        )
        assert failed.status_code == 502
        assert failed.json()["code"] == "UNAVAILABLE"
        listed = await client.get("/api/v1/courses")
        assert listed.status_code == 200
        failed_course = next(
            row for row in listed.json() if row["id"] == confirmed.json()["confirmed_course"]
        )
        assert failed_course["sync_status"] == "FAILED"
        assert failed_course["sync_error_code"] == "UNAVAILABLE"
        assert failed_course["sync_error_message"] == "bounded connector outage"
        assert failed_course["sync_error_retryable"] is True
        assert failed_course["sync_error_at"] is not None

    async with session_factory() as db:
        refreshed = await db.get(MoodleCredential, credential.id)
        assert refreshed is not None
        assert refreshed.revision == 3
        assert refreshed.lease_owner is None and refreshed.lease_expires_at is None
        assert decrypt_moodle_browser_state(
            refreshed.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=refreshed.principal_id,
        ) == _state("refresh-2")
    assert calls == 2


async def test_manual_course_sync_uses_foreground_snapshot_while_history_worker_holds_lease(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    connection, bearer, credential = await _seed_teacher(session_factory, settings)
    assert credential is not None
    calls = 0

    async def discover_course(
        browser: MoodleBrowserClient,
        external_id: str,
        actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        nonlocal calls
        calls += 1
        assert external_id == "549"
        assert actor_external_subject == "42"
        assert interactive is True
        expected_state = "initial-session" if calls == 1 else "after-import"
        assert browser.storage_state == _state(expected_state)
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=calls),
            storage_state=_state(f"foreground-{calls}" if calls > 1 else "after-import"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover_course)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=await _csrf(client),
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text

        async with session_factory() as db:
            current = await db.get(MoodleCredential, credential.id)
            assert current is not None
            assert current.revision == 2
            current.lease_owner = "history-worker"
            current.lease_expires_at = utcnow() + timedelta(minutes=2)
            await db.commit()

        synced = await client.post(
            f"/api/v1/courses/{confirmed.json()['confirmed_course']}/sync",
            headers=await _csrf(client),
            json={},
        )

    assert synced.status_code == 200, synced.text
    assert synced.json()["title"] == "C++ course revision 2"
    assert calls == 2
    async with session_factory() as db:
        current = await db.get(MoodleCredential, credential.id)
        assert current is not None
        # The foreground crawl must not steal or overwrite the worker's state.
        assert current.lease_owner == "history-worker"
        assert current.lease_expires_at is not None
        assert current.revision == 2
        assert decrypt_moodle_browser_state(
            current.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=current.principal_id,
        ) == _state("after-import")


async def test_manual_course_sync_coalesces_with_queued_background_refresh(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    connection, bearer, _ = await _seed_teacher(session_factory, settings)
    calls = 0

    async def discover_course(
        browser: MoodleBrowserClient,
        external_id: str,
        actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        nonlocal calls
        calls += 1
        assert browser.storage_state == _state("initial-session")
        assert external_id == "549"
        assert actor_external_subject == "42"
        assert interactive is True
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=1),
            storage_state=_state("after-import"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover_course)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=await _csrf(client),
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text
        course_id = uuid.UUID(confirmed.json()["confirmed_course"])
        delayed_until = utcnow() + timedelta(minutes=10)
        async with session_factory() as db, db.begin():
            course = await db.get(Course, course_id)
            assert course is not None
            course.sync_status = "PENDING"
            queued = SyncOutbox(
                connection_id=connection.id,
                course_id=course.id,
                event_type="course.sync",
                aggregate_type="Course",
                aggregate_id=course.id,
                idempotency_key=f"queued-course-sync:{course.id}",
                payload={"course_id": course.external_id},
                state=SyncOutboxState.RETRY.value,
                next_attempt_at=delayed_until,
            )
            db.add(queued)
            await db.flush()
            queued_id = queued.id

        synced = await client.post(
            f"/api/v1/courses/{course_id}/sync",
            headers=await _csrf(client),
            json={},
        )

    assert synced.status_code == 200, synced.text
    assert synced.json()["sync_status"] == "SYNCING"
    # Only the import preview contacted Moodle.  The explicit refresh reused
    # the already durable background request instead of racing it.
    assert calls == 1
    async with session_factory() as db:
        queued = await db.get(SyncOutbox, queued_id)
    assert queued is not None and queued.state == SyncOutboxState.RETRY.value
    assert queued.next_attempt_at < delayed_until.replace(tzinfo=None)


async def test_course_import_materializes_idempotent_moodle_managed_drafts(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    _, bearer, _ = await _seed_teacher(session_factory, settings)
    calls = 0

    def activities(revision: int) -> list[dict[str, object]]:
        suffix = " changed upstream" if revision > 1 else ""
        return [
            {
                "cmid": 777,
                "instance_id": 70,
                "module": "assign",
                "name": f"Лабораторная работа 1{suffix}",
                "description": "Реализуйте обработку массива.",
                "visible": True,
                "uservisible": True,
                "url": "https://moodle.example.test/mod/assign/view.php?id=777",
                "opens_at": 1_787_600_000,
                "due_at": 1_787_607_200,
                "cutoff_at": 0,
                "grade_max": 10.0,
                "attempt_limit_unlimited": True,
                "import_supported": True,
                "answer_transport": "FUTURE_TRANSPORT",
                "title_confirmed": True,
                "settings_confirmed": True,
                "statement_confirmed": True,
                "schedule_confirmed": True,
                "duration_confirmed": True,
                "grade_confirmed": True,
                "attempt_policy_confirmed": True,
            },
            {
                "cmid": 778,
                "instance_id": 71,
                "module": "quiz",
                "name": "Экзаменационная работа",
                "description": "Напишите программу по условию.",
                "visible": True,
                "uservisible": True,
                "url": "https://moodle.example.test/mod/quiz/view.php?id=778",
                "opens_at": 1_787_600_000,
                "due_at": 1_787_607_200,
                "cutoff_at": 0,
                "grade_max": 20.0,
                "duration_seconds": 5400,
                "attempt_limit": 1,
                "quiz_grading_method": "LAST",
                "quiz_grading_method_confirmed": True,
                "question_count": 1,
                "essay_question_count": 1,
                "import_supported": True,
                "answer_transport": "ESSAY_ONLINE_TEXT",
                "title_confirmed": True,
                "settings_confirmed": True,
                "statement_confirmed": True,
                "schedule_confirmed": True,
                "duration_confirmed": True,
                "grade_confirmed": True,
                "attempt_policy_confirmed": True,
            },
        ]

    async def discover(
        _browser: MoodleBrowserClient,
        external_id: str,
        _actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        nonlocal calls
        calls += 1
        assert interactive is True
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=calls, activities=activities(calls)),
            storage_state=_state(f"draft-import-{calls}"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        headers = await _csrf(client)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=headers,
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=headers,
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text
        course_id = uuid.UUID(confirmed.json()["confirmed_course"])

        async with session_factory() as db, db.begin():
            items = list(
                (
                    await db.scalars(
                        select(TaskBankItem).where(TaskBankItem.course_id == course_id)
                    )
                ).all()
            )
            versions = list(
                (
                    await db.scalars(
                        select(TaskVersion)
                        .join(TaskBankItem, TaskBankItem.id == TaskVersion.item_id)
                        .where(TaskBankItem.course_id == course_id)
                    )
                ).all()
            )
            assessments = list(
                (
                    await db.scalars(select(Assessment).where(Assessment.course_id == course_id))
                ).all()
            )
            links = list(
                (
                    await db.scalars(
                        select(AssessmentItem)
                        .join(Assessment, Assessment.id == AssessmentItem.assessment_id)
                        .where(Assessment.course_id == course_id)
                    )
                ).all()
            )
            assert len(items) == len(versions) == len(assessments) == len(links) == 2
            assert all(item.tags[:2] == ["moodle-import", "moodle-managed"] for item in items)
            assert all(version.status == "DRAFT" for version in versions)
            assert all(
                version.ai_policy["moodle_metadata_read_only"] is True for version in versions
            )
            assert all(
                version.starter_files == [{"path": "main.cpp", "content": ""}]
                for version in versions
            )
            assert all(assessment.status == "DRAFT" for assessment in assessments)
            assert {assessment.type for assessment in assessments} == {"LAB", "EXAM"}
            local_version = next(
                version for version in versions if version.title.startswith("Лабораторная")
            )
            local_item_id = local_version.item_id
            local_version.statement = "Локально исправленное условие"
            local_version.content_hash = "0" * 64
            version_id = local_version.id
            local_assessment = next(
                assessment
                for assessment in assessments
                if assessment.title.startswith("Лабораторная")
            )
            local_assessment.title = "Локально исправленная лабораторная"
            local_assessment.closes_at = datetime.fromtimestamp(1_900_000_000, tz=UTC)
            assessment_id = local_assessment.id
            exam_assessment = next(
                assessment for assessment in assessments if assessment.type == "EXAM"
            )
            exam_assessment_id = exam_assessment.id
            teacher_membership = await db.scalar(
                select(CourseMembership).where(
                    CourseMembership.course_id == course_id,
                    CourseMembership.role == "TEACHER",
                )
            )
            assert teacher_membership is not None
            teacher_principal = await db.get(
                ExternalPrincipal,
                teacher_membership.principal_id,
            )
            imported_course = await db.get(Course, course_id)
            assert teacher_principal is not None and imported_course is not None
            saved_status = local_assessment.status
            saved_opens_at = local_assessment.opens_at
            saved_closes_at = local_assessment.closes_at
            local_assessment.status = "PUBLISHED"
            local_assessment.opens_at = None
            local_assessment.closes_at = None
            assert (
                await _student_available(
                    db,
                    local_assessment,
                    MembershipContext(
                        membership=teacher_membership,
                        principal=teacher_principal,
                        course=imported_course,
                        group_external_ids=frozenset(),
                    ),
                )
                is False
            )
            local_assessment.status = saved_status
            local_assessment.opens_at = saved_opens_at
            local_assessment.closes_at = saved_closes_at
            publication_group = CourseGroup(
                course_id=course_id,
                external_id="moodle-group-23",
                name="2.3 - подгруппа Ada T.",
            )
            unrelated_group = CourseGroup(
                course_id=course_id,
                external_id="moodle-group-99",
                name="9.9 - подгруппа Other T.",
            )
            db.add_all([publication_group, unrelated_group])
            await db.flush()
            db.add(
                CourseMembershipGroup(
                    coursemembership_id=teacher_membership.id,
                    coursegroup_id=publication_group.id,
                )
            )
            publication_group_id = publication_group.id
            unrelated_group_id = unrelated_group.id
            elevations = list((await db.scalars(select(AdminElevation))).all())
            for elevation in elevations:
                elevation.expires_at = utcnow() - timedelta(seconds=1)

        read_only = await client.patch(
            f"/api/v1/assessments/{exam_assessment_id}",
            headers=headers,
            json={"title": "Локальное название запрещено"},
        )
        assert read_only.status_code == 409, read_only.text
        assert read_only.json()["code"] == "MOODLE_METADATA_READ_ONLY"
        no_groups = await client.post(
            f"/api/v1/assessments/{exam_assessment_id}/publish",
            headers=headers,
            json={},
        )
        assert no_groups.status_code == 422, no_groups.text
        visible_groups = await client.get(f"/api/v1/courses/{course_id}/groups")
        assert visible_groups.status_code == 200, visible_groups.text
        assert [row["id"] for row in visible_groups.json()] == [str(publication_group_id)]
        forbidden_group = await client.post(
            f"/api/v1/assessments/{exam_assessment_id}/publish",
            headers=headers,
            json={"group_ids": [str(unrelated_group_id)]},
        )
        assert forbidden_group.status_code == 403, forbidden_group.text
        assert forbidden_group.json()["code"] == "GROUP_SCOPE_REQUIRED"
        enabled = await client.post(
            f"/api/v1/assessments/{exam_assessment_id}/publish",
            headers=headers,
            json={"group_ids": [str(publication_group_id)]},
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["status"] == "PUBLISHED"
        assert [rule["target_external_id"] for rule in enabled.json()["availability_rules"]] == [
            "moodle-group-23"
        ]

        validation = await client.post(
            f"/api/v1/task-versions/{version_id}/validate",
            headers=headers,
            json={},
        )
        assert validation.status_code == 200, validation.text
        assert {row["code"] for row in validation.json()["errors"]} >= {"CONTENT_HASH_MISMATCH"}

        configured = await client.put(
            f"/api/v1/task-versions/{version_id}",
            headers=headers,
            json={
                "title": "Лабораторная работа 1",
                "statement": "Локально исправленное условие",
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
        assert configured.status_code == 200, configured.text
        assert configured.json()["ai_policy"]["moodle_metadata_read_only"] is True
        configured_validation = await client.post(
            f"/api/v1/task-versions/{version_id}/validate",
            headers=headers,
            json={},
        )
        assert configured_validation.status_code == 200, configured_validation.text
        assert configured_validation.json()["valid"] is True
        extra_version = await client.post(
            f"/api/v1/task-bank/items/{local_item_id}/versions",
            headers=headers,
            json={
                "title": "Локальная версия запрещена",
                "statement": "Локальное условие",
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
        assert extra_version.status_code == 409, extra_version.text
        assert extra_version.json()["code"] == "MOODLE_METADATA_READ_ONLY"
        direct_task_publication = await client.post(
            f"/api/v1/task-versions/{version_id}/publish",
            headers=headers,
            json={},
        )
        assert direct_task_publication.status_code == 409, direct_task_publication.text
        assert direct_task_publication.json()["code"] == "MOODLE_GROUP_PUBLICATION_REQUIRED"
        assessment_validation = await client.post(
            f"/api/v1/assessments/{assessment_id}/validate",
            headers=headers,
            json={},
        )
        assert assessment_validation.status_code == 200, assessment_validation.text
        assert assessment_validation.json()["valid"] is True, assessment_validation.json()
        assert "MOODLE_ANSWER_TRANSPORT_UNSUPPORTED" not in {
            row["code"] for row in assessment_validation.json()["errors"]
        }
        lab_enabled = await client.post(
            f"/api/v1/assessments/{assessment_id}/publish",
            headers=headers,
            json={"group_ids": [str(publication_group_id)]},
        )
        assert lab_enabled.status_code == 200, lab_enabled.text
        assert lab_enabled.json()["status"] == "PUBLISHED"

        synced = await client.post(
            f"/api/v1/courses/{course_id}/sync",
            headers=headers,
            json={},
        )
        assert synced.status_code == 200, synced.text

    async with session_factory() as db:
        course = await db.get(Course, course_id)
        assert course is not None
        items = list(
            (
                await db.scalars(select(TaskBankItem).where(TaskBankItem.course_id == course_id))
            ).all()
        )
        versions = list(
            (
                await db.scalars(
                    select(TaskVersion)
                    .join(TaskBankItem, TaskBankItem.id == TaskVersion.item_id)
                    .where(TaskBankItem.course_id == course_id)
                )
            ).all()
        )
        assessments = list(
            (await db.scalars(select(Assessment).where(Assessment.course_id == course_id))).all()
        )
        local_version = await db.get(TaskVersion, version_id)
        local_assessment = await db.get(Assessment, assessment_id)
        mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.connection_id == course.connection_id
                    )
                )
            ).all()
        )
    assert len(items) == len(assessments) == 2
    # Publishing freezes the local task snapshot.  The later Moodle refresh
    # creates a successor instead of rewriting the version that could already
    # be assigned to an attempt.
    assert len(versions) == 3
    assert local_version is not None
    assert local_version.statement == "Локально исправленное условие"
    assert any(
        version.id != local_version.id and version.statement == "Реализуйте обработку массива."
        for version in versions
    )
    assert local_assessment is not None
    assert local_assessment.title == "Лабораторная работа 1 changed upstream"
    assert local_assessment.attempt_limit is None
    assert local_assessment.closes_at is not None
    assert local_assessment.closes_at.replace(tzinfo=UTC) == datetime.fromtimestamp(
        1_787_607_200, tz=UTC
    )
    imported_mappings = [
        row for row in mappings if row.metadata_json.get("managed_by") == "MOODLE_ACTIVITY_IMPORT"
    ]
    assert len(imported_mappings) == 2
    modes_by_module = {
        row.metadata_json["module"]: row.metadata_json["submission_mode"]
        for row in imported_mappings
    }
    assert modes_by_module == {
        "assign": "REQUIRES_CONFIGURATION",
        "quiz": "ESSAY_ONLINE_TEXT",
    }


async def test_global_course_catalog_is_admin_only_and_supports_add_list_remove(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    _, bearer, _ = await _seed_teacher(session_factory, settings)

    async def discover(
        _browser: MoodleBrowserClient,
        external_id: str,
        actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        assert external_id == "549"
        assert actor_external_subject == "42"
        assert interactive is True
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=1),
            storage_state=_state("catalog-add"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        headers = await _csrf(client)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=headers,
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=headers,
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text
        course_id = confirmed.json()["confirmed_course"]

        listed = await client.get("/api/v1/system/course-catalog")
        assert listed.status_code == 200, listed.text
        assert {row["external_id"] for row in listed.json()} == {"100", "549"}
        imported_row = next(row for row in listed.json() if row["external_id"] == "549")
        assert imported_row["id"] == course_id
        assert imported_row["added_at"] is not None

        removed = await client.delete(f"/api/v1/system/course-catalog/{course_id}", headers=headers)
        assert removed.status_code == 204, removed.text
        after_remove = await client.get("/api/v1/system/course-catalog")
        assert {row["external_id"] for row in after_remove.json()} == {"100"}

    async with session_factory() as db:
        course = await db.get(Course, uuid.UUID(course_id))
        membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == uuid.UUID(course_id),
                CourseMembership.principal_id
                == (
                    select(ExternalPrincipal.id)
                    .where(ExternalPrincipal.external_subject == "42")
                    .scalar_subquery()
                ),
                CourseMembership.role == "TEACHER",
            )
        )
        assert course is not None and course.catalog_enabled is False
        assert membership is not None and membership.active is False


async def test_course_catalog_and_import_reject_non_elevated_teacher(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    _, bearer, _ = await _seed_teacher(session_factory, settings, elevated=False)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        listed = await client.get("/api/v1/system/course-catalog")
        imported = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )

    assert listed.status_code == 403
    assert imported.status_code == 403
    assert imported.json()["code"] == "SYSTEM_SETTINGS_REQUIRED"


async def test_manual_course_sync_updates_quiz_essay_schedule_and_active_attempt(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    _, bearer, _ = await _seed_teacher(session_factory, settings)
    opens_at = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=5)
    due_at = opens_at + timedelta(hours=2)
    calls = 0

    async def discover_course(
        _browser: MoodleBrowserClient,
        external_id: str,
        _actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        nonlocal calls
        calls += 1
        assert interactive is True
        activities: list[dict[str, object]] = []
        if calls == 2:
            activities = [
                {
                    "cmid": 30354,
                    "instance_id": 9001,
                    "module": "quiz",
                    "name": "Independent work (Essay)",
                    "visible": True,
                    "uservisible": True,
                    "url": "https://moodle.example.test/mod/quiz/view.php?id=30354",
                    "opens_at": int(opens_at.timestamp()),
                    "due_at": int(due_at.timestamp()),
                    "cutoff_at": 0,
                    # A Quiz score is not coupled to the local teacher rubric.
                    "grade_max": 100.0,
                    "attempt_limit": 1,
                    "quiz_grading_method": "LAST",
                    "quiz_grading_method_confirmed": True,
                    "question_count": 1,
                    "essay_question_count": 1,
                    "import_supported": True,
                    "answer_transport": "ESSAY_ATTACHMENT",
                    "title_confirmed": True,
                    "settings_confirmed": True,
                    "statement_confirmed": True,
                    "schedule_confirmed": True,
                    "duration_confirmed": True,
                    "grade_confirmed": True,
                    "attempt_policy_confirmed": True,
                }
            ]
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=calls, activities=activities),
            storage_state=_state(f"manual-sync-{calls}"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover_course)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=await _csrf(client),
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text
        course_id = uuid.UUID(confirmed.json()["confirmed_course"])

        async with session_factory() as db:
            course = await db.get(Course, course_id)
            principal = await db.scalar(
                select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "42")
            )
            assert course is not None and principal is not None
            assessment = Assessment(
                course_id=course.id,
                title="Independent work",
                max_score=Decimal("10.00"),
                created_by_id=principal.id,
                status="PUBLISHED",
            )
            db.add(assessment)
            await db.flush()
            mapping = ExternalMapping(
                connection_id=course.connection_id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type="mod_quiz",
                external_id="30354",
                metadata_json={
                    "module": "quiz",
                    "cmid": 30354,
                    "submission_mode": "ESSAY_ATTACHMENT",
                    "sync_deadlines": True,
                },
            )
            attempt = Attempt(
                assessment_id=assessment.id,
                principal_id=principal.id,
                expected_end_at=due_at + timedelta(hours=1),
                deadline_at=due_at + timedelta(days=1),
                state="ACTIVE",
            )
            db.add_all([mapping, attempt])
            await db.commit()
            assessment_id = assessment.id
            mapping_id = mapping.id
            attempt_id = attempt.id

        synced = await client.post(
            f"/api/v1/courses/{course_id}/sync",
            headers=await _csrf(client),
            json={},
        )
        assert synced.status_code == 200, synced.text

    async with session_factory() as db:
        assessment = await db.get(Assessment, assessment_id)
        mapping = await db.get(ExternalMapping, mapping_id)
        attempt = await db.get(Attempt, attempt_id)
        assert assessment is not None and mapping is not None and attempt is not None
        assert assessment.opens_at.replace(tzinfo=UTC) == opens_at
        assert assessment.closes_at.replace(tzinfo=UTC) == due_at
        assert assessment.section_id is not None
        assert attempt.deadline_at.replace(tzinfo=UTC) == due_at
        assert mapping.metadata_json["sync_state"] == "CURRENT"
        assert mapping.metadata_json["submission_mode"] == "ESSAY_ATTACHMENT"
        assert mapping.metadata_json["activity"]["cmid"] == 30354
        assert mapping.external_revision == f"{2:064x}"
    assert calls == 2


async def test_playwright_import_safely_takes_over_expired_lease(app_bundle, monkeypatch) -> None:
    app, session_factory, settings = app_bundle
    connection, bearer, credential = await _seed_teacher(
        session_factory,
        settings,
        credential_mode="stale",
    )
    assert credential is not None

    async def discover(
        _browser: MoodleBrowserClient,
        external_id: str,
        _actor: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        assert interactive is True
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=1),
            storage_state=_state("after-stale-takeover"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )

    assert response.status_code == 201, response.text
    async with session_factory() as db:
        current = await db.get(MoodleCredential, credential.id)
        assert current is not None
        assert current.lease_owner is None and current.lease_expires_at is None
        assert current.revision == 2
        assert decrypt_moodle_browser_state(
            current.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=current.principal_id,
        ) == _state("after-stale-takeover")


@pytest.mark.parametrize(
    ("credential_mode", "expected_code"),
    [
        ("missing", "NOT_CONFIGURED"),
        ("expired", "NOT_CONFIGURED"),
        ("invalid_shape", "NOT_CONFIGURED"),
        ("busy", "BROWSER_BUSY"),
    ],
)
async def test_playwright_import_fails_closed_for_missing_expired_or_busy_session(
    app_bundle,
    monkeypatch,
    credential_mode: str,
    expected_code: str,
) -> None:
    app, session_factory, settings = app_bundle
    _, bearer, credential = await _seed_teacher(
        session_factory,
        settings,
        credential_mode=credential_mode,
    )
    upstream_calls = 0

    async def must_not_discover(*_args, **_kwargs) -> MoodleBrowserDiscoveryResult:
        nonlocal upstream_calls
        upstream_calls += 1
        raise AssertionError("Unavailable browser state must not reach Playwright")

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", must_not_discover)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )

    assert response.status_code == 502
    assert response.json()["code"] == expected_code
    assert upstream_calls == 0
    async with session_factory() as db:
        job = await db.scalar(select(CourseImportJob))
        assert job is not None and job.state == "FAILED"
        if credential_mode == "busy":
            assert credential is not None
            current = await db.get(MoodleCredential, credential.id)
            assert current is not None
            assert current.lease_owner == "another-request"
            assert current.lease_expires_at is not None
        elif credential is not None:
            current = await db.get(MoodleCredential, credential.id)
            assert current is not None
            assert current.lease_owner is None and current.lease_expires_at is None


async def test_playwright_discovery_error_releases_lease_and_keeps_previous_state(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    connection, bearer, credential = await _seed_teacher(session_factory, settings)
    assert credential is not None
    original_ciphertext = credential.encrypted_secret

    async def unavailable(*_args, **_kwargs) -> MoodleBrowserDiscoveryResult:
        raise IntegrationUnavailable("safe upstream outage")

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", unavailable)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )

    assert response.status_code == 502
    assert response.json()["code"] == "UNAVAILABLE"
    async with session_factory() as db:
        current = await db.get(MoodleCredential, credential.id)
        assert current is not None
        assert current.lease_owner is None and current.lease_expires_at is None
        assert current.revision == 1
        assert current.encrypted_secret == original_ciphertext
        assert decrypt_moodle_browser_state(
            current.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=current.principal_id,
        ) == _state("initial-session")


async def test_playwright_expired_upstream_session_requires_reauthentication_and_releases_lease(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    _, bearer, credential = await _seed_teacher(session_factory, settings)
    assert credential is not None

    async def expired(*_args, **_kwargs) -> MoodleBrowserDiscoveryResult:
        raise MoodleAuthenticationError("browser session expired")

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", expired)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )

    assert response.status_code == 502
    assert response.json()["code"] == "NOT_CONFIGURED"
    async with session_factory() as db:
        current = await db.get(MoodleCredential, credential.id)
        assert current is not None
        assert current.status == "EXPIRED"
        assert current.expires_at is not None
        assert current.lease_owner is None and current.lease_expires_at is None
