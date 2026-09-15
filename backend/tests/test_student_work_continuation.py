from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.api.ai import _student_context
from app.api.attempts import _attempt_read
from app.api.authoring import list_course_assessments, publish_assessment, update_assessment
from app.api.courses import _course_payload
from app.api.system import list_sync_outbox
from app.auth.context import AuthContext
from app.models.attempts import Attempt, Submission, Workspace, WorkspaceFile
from app.models.courses import Course, CourseMembership
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.tasks import Assessment
from app.schemas.assessments import AssessmentPublishRequest, AssessmentUpdateRequest
from app.services.common import DomainError
from app.services.moodle_history import _mark_managed_quiz_as_split_container
from app.services.workspace import replace_file_content, start_attempt
from tests.test_attempt_review_services import _seed_course, _start_multi_quiz


def auth(principal, role="TEACHER"):
    return AuthContext(
        principal_id=principal.id,
        display_name=principal.display_name,
        session_id=uuid.uuid4(),
        session_key="new-browser-session",
        roles=(role,),
        capabilities=(),
    )


async def test_untimed_lab_resumes_persisted_files_days_later(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    async with sessions() as db:
        student, _, _, assessment = await _seed_course(db)
        attempt = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
        file = await db.scalar(
            select(WorkspaceFile).where(
                WorkspaceFile.workspace_id == workspace.id,
                WorkspaceFile.path == "main.cpp",
            )
        )
        source = "int main() { return 7; }\n"
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=file.id,
            content=source,
            expected_revision=0,
            client_request_id="first-day-edit",
            source="TYPING",
        )
        ids = (
            student.id,
            assessment.id,
            attempt.id,
            workspace.id,
            file.id,
            attempt.assigned_task_version_id,
        )
        await db.commit()

    later = datetime.now(UTC) + timedelta(days=5)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: later)
    # A fresh database session represents another login/browser/server process.
    async with sessions() as db:
        student_id, assessment_id, attempt_id, workspace_id, file_id, version_id = ids
        resumed = await start_attempt(db, assessment_id=assessment_id, principal_id=student_id)
        workspace = await db.get(Workspace, workspace_id)
        file = await db.get(WorkspaceFile, file_id)
        assert resumed.id == attempt_id and resumed.sequence == 1 and resumed.state == "ACTIVE"
        assert resumed.assigned_task_version_id == version_id
        assert resumed.deadline_at is None and resumed.expected_end_at is None
        assert workspace.current_revision == 1 and file.content == source
        assert await db.scalar(select(func.count(Attempt.id))) == 1
        assert await db.scalar(select(func.count(Submission.id))) == 0
        read = await _attempt_read(
            db, resumed, await db.get(Assessment, assessment_id), workspace, settings
        )
        assert read.has_time_limit is False
        await replace_file_content(
            db,
            attempt_id=resumed.id,
            principal_id=student_id,
            file_id=file_id,
            content="int main() { return 8; }\n",
            expected_revision=1,
            client_request_id="fifth-day-edit",
            source="TYPING",
        )
        assert workspace.current_revision == 2


async def test_an_explicit_lab_time_limit_is_not_removed(db, app_bundle):
    _, _, settings = app_bundle
    student, _, _, assessment = await _seed_course(db)
    assessment.duration_seconds = 3600
    await db.flush()
    attempt = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    read = await _attempt_read(db, attempt, assessment, workspace, settings)
    assert read.has_time_limit is True
    assert read.deadline_at is not None and read.expected_end_at is not None


@pytest.mark.parametrize(
    ("transport", "duration", "expected"),
    [
        ("ASSIGN_FILE", None, False),
        ("ASSIGN_ONLINE_TEXT", None, False),
        ("ASSIGN_FILE", 3600, None),
        ("ESSAY_ATTACHMENT", None, None),
        (None, None, None),
    ],
)
async def test_unread_moodle_timer_is_not_assumed_unlimited(
    db, app_bundle, transport, duration, expected,
):
    _, _, settings = app_bundle
    student, _, _, assessment = await _seed_course(db)
    attempt = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    assessment.policy = {**assessment.policy, "moodle_metadata_read_only": True}
    assessment.duration_seconds = duration
    attempt.integrity_policy = {**attempt.integrity_policy, "moodle_answer_transport": transport}
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    read = await _attempt_read(db, attempt, assessment, workspace, settings)
    assert read.has_time_limit is expected


async def test_all_quiz_solutions_follow_current_parent_ai_permission(db, app_bundle):
    _, _, settings = app_bundle
    settings.ai_mock_enabled = True
    student, _, parent, root, questions = await _start_multi_quiz(db)
    parent.student_ai_enabled = True
    await db.flush()
    for question in questions:
        attempt = await db.get(Attempt, question.attempt_id)
        assessment = await db.get(Assessment, attempt.assessment_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
        read = await _attempt_read(db, attempt, assessment, workspace, settings)
        assert read.ai_enabled
        context = await _student_context(
            db,
            principal_id=student.id,
            attempt_id=attempt.id,
            expected_course_id=parent.course_id,
            settings=settings,
        )
        assert context["attempt"]["id"] == str(attempt.id)
    parent.student_ai_enabled = False
    await db.flush()
    for question in questions:
        with pytest.raises(DomainError) as error:
            await _student_context(
                db,
                principal_id=student.id,
                attempt_id=question.attempt_id,
                expected_course_id=parent.course_id,
                settings=settings,
            )
        assert error.value.code == "STUDENT_AI_DISABLED"


async def test_teacher_can_change_only_local_ai_policy_on_published_work(db):
    student, teacher, _, assessment = await _seed_course(db)
    changed = await update_assessment(
        assessment.id,
        AssessmentUpdateRequest(student_ai_enabled=True),
        auth(teacher),
        db,
    )
    assert changed.student_ai_enabled
    with pytest.raises(HTTPException) as error:
        await update_assessment(
            assessment.id,
            AssessmentUpdateRequest(student_ai_enabled=False),
            auth(student, "STUDENT"),
            db,
        )
    assert getattr(error.value, "status_code", None) in {403, 404}


async def test_publication_ai_choice_is_optional_and_preserves_existing_setting(db):
    _, teacher, _, assessment = await _seed_course(db)
    await publish_assessment(
        assessment.id,
        AssessmentPublishRequest(student_ai_enabled=True),
        auth(teacher),
        db,
    )
    assert assessment.student_ai_enabled
    await publish_assessment(assessment.id, AssessmentPublishRequest(), auth(teacher), db)
    assert assessment.student_ai_enabled
    await publish_assessment(
        assessment.id,
        AssessmentPublishRequest(student_ai_enabled=False),
        auth(teacher),
        db,
    )
    assert not assessment.student_ai_enabled


async def test_history_split_keeps_parent_work_in_teacher_and_student_catalog(db):
    student, teacher, parent, _, questions = await _start_multi_quiz(db)
    course = await db.get(Course, parent.course_id)
    mapping = await db.scalar(select(ExternalMapping).where(ExternalMapping.local_id == parent.id))
    mapping.metadata_json = {**mapping.metadata_json, "managed_by": "MOODLE_ACTIVITY_IMPORT"}
    await _mark_managed_quiz_as_split_container(db, course=course, parent=parent)
    assert parent.policy["historical_quiz_split_container"] is True
    await db.flush()
    for principal, role in [(teacher, "TEACHER"), (student, "STUDENT")]:
        works = await list_course_assessments(parent.course_id, auth(principal, role), db)
        assert [work.id for work in works] == [parent.id]
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == teacher.id,
        )
    )
    payload = await _course_payload(db, membership, course, teacher=True)
    assert payload["active_count"] == 1
    # The internal questions still exist for editor switching and review.
    assert len(questions) == 2
    assert await db.scalar(select(func.count(Assessment.id))) == 2


async def test_history_status_selects_newest_job_before_pagination(db):
    _, teacher, _, assessment = await _seed_course(db)
    course = await db.get(Course, assessment.course_id)
    first = datetime(2026, 9, 9, tzinfo=UTC)
    jobs = []
    for index, state in enumerate(["FAILED", "DELIVERED"]):
        row = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=assessment.id,
            idempotency_key=f"import-{index}",
            payload={"actor_external_subject": teacher.external_subject},
            state=state,
            created_at=first + timedelta(hours=index),
            # The old failed job was updated after the new import finished.
            updated_at=first + timedelta(hours=3 - index),
            receipt={"complete": state == "DELIVERED", "created": 0},
        )
        jobs.append(row)
        db.add(row)
    db.add(
        SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="task.mirror",
            aggregate_type="TaskVersion",
            aggregate_id=uuid.uuid4(),
            idempotency_key="unrelated-newer-event",
            created_at=first + timedelta(days=1),
        )
    )
    await db.flush()
    rows = await list_sync_outbox(
        auth(teacher),
        db,
        limit=1,
        offset=0,
        event_type="moodle.history.import",
        latest_per_activity=True,
    )
    assert len(rows) == 1 and rows[0].id == jobs[1].id
    assert rows[0].state == "DELIVERED" and rows[0].aggregate_title == assessment.title
