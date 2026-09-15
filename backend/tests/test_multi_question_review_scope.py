from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.api.reviews import list_submissions
from app.auth.context import AuthContext
from app.models.attempts import Attempt, Snapshot, Submission, Workspace
from app.models.courses import Course, CourseMembership
from app.models.identity import ExternalPrincipal
from app.models.review import ReviewDecision
from app.models.tasks import Assessment, TaskVersion
from app.services.moodle_history import (
    _ensure_quiz_question_context,
    materialize_historical_submissions,
)
from app.services.policy import assessment_review_scope_ids
from app.services.workspace import submit_attempt
from tests.test_attempt_review_services import _grant_teacher, _start_multi_quiz
from tests.test_moodle_history import _multi_essay_item, _seed_history_target


def _teacher_auth(teacher: ExternalPrincipal) -> AuthContext:
    return AuthContext(
        principal_id=teacher.id,
        display_name=teacher.display_name,
        session_id=uuid.uuid4(),
        session_key="teacher-session",
        roles=("TEACHER",),
        capabilities=(),
    )


async def _submitted_quiz(db):
    student, teacher, parent, root, questions = await _start_multi_quiz(db)
    await submit_attempt(db, attempt_id=root.id, principal_id=student.id, expected_revision=0)
    return student, teacher, parent, root, questions


async def test_parent_review_queue_includes_every_native_solution(db):
    student, teacher, parent, root, questions = await _submitted_quiz(db)
    rows = await list_submissions(str(parent.id), _teacher_auth(teacher), db, offset=0, limit=100)
    all_rows = await list_submissions("all", _teacher_auth(teacher), db, offset=0, limit=100)
    assert len(rows) == len(all_rows) == 1
    assert rows[0].review_group is not None
    assert rows[0].review_group == all_rows[0].review_group
    assert [item.max_score for item in rows[0].review_group.items] == [Decimal(3), Decimal(5)]
    assert rows[0].max_score == Decimal(8)
    expected_ids = set((await db.scalars(select(Submission.id))).all())
    assert {item.submission_id for item in rows[0].review_group.items} == expected_ids

    # Asking for a single managed question does not expand back to its parent.
    child = await db.get(Attempt, questions[1].attempt_id)
    child_rows = await list_submissions(
        str(child.assessment_id), _teacher_auth(teacher), db, offset=0, limit=100
    )
    assert len(child_rows) == 1 and child_rows[0].max_score == Decimal(5)
    assert child_rows[0].review_group is None
    assert await assessment_review_scope_ids(db, child.assessment_id) == {child.assessment_id}


async def test_parent_review_queue_keeps_native_sibling_during_partial_import(db):
    student, teacher, parent, root, questions = await _submitted_quiz(db)
    course = await db.get(Course, parent.course_id)
    native = await db.scalar(select(Submission).where(Submission.attempt_id == root.id))
    native_snapshot = await db.get(Snapshot, native.snapshot_id)
    native_version = await db.get(TaskVersion, root.assigned_task_version_id)
    imported_assessment, imported_version = await _ensure_quiz_question_context(
        db,
        course=course,
        parent=parent,
        response={
            "response_id": "1",
            "question_text": native_version.statement,
            "grade_max": 3,
        },
        position=1,
        cmid=30354,
        multi_file=True,
    )
    imported_attempt = Attempt(
        assessment_id=imported_assessment.id,
        assigned_task_version_id=imported_version.id,
        principal_id=student.id,
        state="SUBMITTED",
        submitted_at=native.submitted_at,
        submission_source="MOODLE_IMPORT",
    )
    db.add(imported_attempt)
    await db.flush()
    workspace = Workspace(attempt_id=imported_attempt.id)
    db.add(workspace)
    await db.flush()
    snapshot = Snapshot(
        workspace_id=workspace.id,
        revision=0,
        manifest_hash=native_snapshot.manifest_hash,
        files=list(native_snapshot.files),
        reason="MOODLE_IMPORT",
    )
    db.add(snapshot)
    await db.flush()
    imported = Submission(
        attempt_id=imported_attempt.id,
        snapshot_id=snapshot.id,
        source="MOODLE_IMPORT",
        submitted_at=native.submitted_at,
        external_receipt={
            **native.external_receipt,
            "historical_source_materialization_version": 9,
            "source_complete": True,
        },
    )
    db.add(imported)
    await db.flush()

    rows = await list_submissions(str(parent.id), _teacher_auth(teacher), db, offset=0, limit=100)
    assert len(rows) == 1 and rows[0].review_group is not None
    sibling = await db.scalar(
        select(Submission).where(Submission.attempt_id == questions[1].attempt_id)
    )
    assert {item.submission_id for item in rows[0].review_group.items} == {imported.id, sibling.id}
    assert rows[0].max_score == Decimal(8)


async def test_parent_review_scope_retains_course_and_teacher_group_boundaries(db):
    student, teacher, parent, root, questions = await _submitted_quiz(db)
    course = await db.get(Course, parent.course_id)
    foreign_course = Course(
        connection_id=course.connection_id,
        external_id="foreign-course",
        title="Other course",
        catalog_enabled=True,
    )
    db.add(foreign_course)
    await db.flush()
    foreign_child = Assessment(
        course_id=foreign_course.id,
        type=parent.type,
        title="Malformed cross-course child",
        created_by_id=teacher.id,
        policy={
            "moodle_quiz_question_split": True,
            "moodle_parent_assessment_id": str(parent.id),
        },
    )
    db.add(foreign_child)
    outsider = ExternalPrincipal(
        connection_id=course.connection_id,
        external_subject="unassigned-teacher",
        display_name="Unassigned Teacher",
    )
    db.add(outsider)
    await db.flush()
    await _grant_teacher(db, outsider)
    db.add(CourseMembership(course_id=course.id, principal_id=outsider.id, role="TEACHER"))
    await db.flush()
    scope_ids = await assessment_review_scope_ids(db, parent.id)
    child = await db.get(Attempt, questions[1].attempt_id)
    assert scope_ids == {parent.id, child.assessment_id}
    assert foreign_child.id not in scope_ids
    assert (
        await list_submissions(str(parent.id), _teacher_auth(outsider), db, offset=0, limit=100)
        == []
    )
    assert (
        await list_submissions(
            str(child.assessment_id), _teacher_auth(outsider), db, offset=0, limit=100
        )
        == []
    )


@pytest.mark.parametrize(
    ("remote_grade", "expected_grade"),
    [
        (None, None),
        ("0", Decimal("0.00")),
        ("0.000005", Decimal("0.50")),
        ("0.00001", Decimal("1.00")),
    ],
)
async def test_historical_tiny_question_scale_preserves_relative_grade(
    app_bundle, remote_grade, expected_grade
):
    _, factory, _ = app_bundle
    ids = await _seed_history_target(factory)
    item = _multi_essay_item()
    item["responses"][0].update(grade_max="0.00001", grade=remote_grade)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        parent = await db.get(Assessment, ids["assessment_id"])
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=parent,
            actor_external_subject="42",
            items=[item],
        )
        assert stats.created == 2
        rows = (
            await db.execute(
                select(Submission, Attempt, Assessment)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
            )
        ).all()
        submission, attempt, assessment = next(
            row for row in rows if row[0].external_receipt.get("moodle_response_position") == 1
        )
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        assert assessment.max_score == version.max_score == Decimal("1.00")
        decision = await db.scalar(
            select(ReviewDecision).where(ReviewDecision.submission_id == submission.id)
        )
        if expected_grade is None:
            assert decision is None
        else:
            assert decision is not None and decision.grade == expected_grade
            assert decision.criterion_scores["remote_grade"] == remote_grade
            assert decision.criterion_scores["remote_grade_max"] == "0.00001"
            assert decision.criterion_scores["local_grade_max"] == "1.00"
            assert decision.criterion_scores["normalization"] == "PROPORTIONAL_TO_REMOTE_MAX"
