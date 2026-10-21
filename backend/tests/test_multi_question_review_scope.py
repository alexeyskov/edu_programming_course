from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from app.api.reviews import (
    _review_group_identities,
    _review_queue_group_key,
    _review_queue_group_position,
    _submission_review_group,
    get_submission,
    list_submissions,
)
from app.auth.context import AuthContext
from app.models.attempts import Attempt, MoodleQuizQuestion, Snapshot, Submission, Workspace
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
    assert rows[0].parent_assessment_id == all_rows[0].parent_assessment_id == parent.id
    assert rows[0].parent_assessment_title == parent.title
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
    assert child_rows[0].assessment_id == child.assessment_id
    assert child_rows[0].parent_assessment_id == parent.id
    assert child_rows[0].parent_assessment_title == parent.title
    assert await assessment_review_scope_ids(db, child.assessment_id) == {child.assessment_id}


@pytest.mark.parametrize("root_source", ["MOODLE_IMPORT", "MANUAL", "DEADLINE"])
async def test_queue_and_detail_share_receipt_parent_binding_after_native_import(db, root_source):
    """Importing a native root must not strand it outside its child group."""

    _, teacher, parent, root, _ = await _submitted_quiz(db)
    submissions = list((await db.scalars(select(Submission))).all())
    native_root = next(item for item in submissions if item.attempt_id == root.id)
    native_root.source = root_source
    if root_source == "MOODLE_IMPORT":
        # Imported rows need not retain native-only integrity flags. The
        # receipt still binds the exact parent and remote attempt, so the root
        # belongs with its independently imported child.
        root.integrity_policy = {}
    # Root assessments are deliberately not question-split children. The
    # receipt still pins their exact parent, unlike child-only policy metadata.
    assert not dict(parent.policy or {}).get("moodle_parent_assessment_id")
    await db.flush()

    queue = await list_submissions("all", _teacher_auth(teacher), db, offset=0, limit=1)
    assert len(queue) == 1
    assert await list_submissions("all", _teacher_auth(teacher), db, offset=1, limit=1) == []
    assert queue[0].assessment_title == parent.title
    assert queue[0].parent_assessment_id == parent.id
    assert queue[0].max_score == Decimal(8)
    assert queue[0].review_group is not None
    expected_ids = {item.id for item in submissions}
    assert {item.submission_id for item in queue[0].review_group.items} == expected_ids
    for item in submissions:
        detail = await get_submission(item.id, _teacher_auth(teacher), db)
        assert detail.review_group == queue[0].review_group


async def test_queue_and_detail_group_legacy_numeric_attempt_aliases(db):
    _, teacher, _, root, _ = await _submitted_quiz(db)
    submissions = list((await db.scalars(select(Submission))).all())
    native_root = next(item for item in submissions if item.attempt_id == root.id)
    remote_id = str(native_root.external_receipt["moodle_parent_attempt_id"])
    assert remote_id.isdecimal()
    native_root.external_receipt = {
        **native_root.external_receipt,
        "moodle_parent_attempt_id": f"attempt:{remote_id}",
    }
    await db.flush()

    queue = await list_submissions("all", _teacher_auth(teacher), db, offset=0, limit=1)
    assert len(queue) == 1 and queue[0].review_group is not None
    assert await list_submissions("all", _teacher_auth(teacher), db, offset=1, limit=1) == []
    for item in submissions:
        detail = await get_submission(item.id, _teacher_auth(teacher), db)
        assert detail.review_group == queue[0].review_group


@pytest.mark.parametrize("export_state", ["PENDING", "FAILED", "DELIVERED"])
@pytest.mark.parametrize("empty_receipts", ["both", "child"])
@pytest.mark.parametrize("missing_receipt", [None, {}])
async def test_native_grouping_does_not_wait_for_delivery_receipts(
    db, export_state, empty_receipts, missing_receipt,
):
    """Regression for MANUAL NUC submissions with no external receipt yet."""

    _, teacher, parent, root, _ = await _submitted_quiz(db)
    submissions = list((await db.scalars(select(Submission))).all())
    for item in submissions:
        item.lms_export_state = export_state
        if empty_receipts == "both" or item.attempt_id != root.id:
            item.external_receipt = missing_receipt
    original_receipts = {
        item.id: dict(item.external_receipt) if item.external_receipt is not None else None
        for item in submissions
    }
    await db.flush()

    queue = await list_submissions("all", _teacher_auth(teacher), db, offset=0, limit=1)
    assert len(queue) == 1
    assert queue[0].parent_assessment_id == parent.id
    assert queue[0].assessment_title == parent.title
    assert queue[0].max_score == Decimal(8)
    assert queue[0].review_group is not None
    assert [item.position for item in queue[0].review_group.items] == [1, 2]
    assert {item.submission_id for item in queue[0].review_group.items} == {
        item.id for item in submissions
    }
    assert await list_submissions("all", _teacher_auth(teacher), db, offset=1, limit=1) == []
    for item in submissions:
        detail = await get_submission(item.id, _teacher_auth(teacher), db)
        assert detail.review_group == queue[0].review_group
        assert item.external_receipt == original_receipts[item.id]
        assert item.lms_export_state == export_state


@pytest.mark.parametrize(
    "broken_binding", ["root", "parent", "remote", "student", "relation", "receipt"],
)
async def test_native_receipt_fallback_requires_durable_matching_identity(db, broken_binding):
    student, _, parent, root, questions = await _submitted_quiz(db)
    submissions = list((await db.scalars(select(Submission))).all())
    child = await db.get(Attempt, questions[1].attempt_id)
    child_assessment = await db.get(Assessment, child.assessment_id)
    child_submission = next(item for item in submissions if item.attempt_id == child.id)
    for item in submissions:
        item.external_receipt = {}
    if broken_binding in {"root", "parent", "remote"}:
        child.integrity_policy = {
            **child.integrity_policy,
            {
                "root": "moodle_quiz_root_attempt_id",
                "parent": "moodle_parent_assessment_id",
                "remote": "moodle_attempt_id",
            }[broken_binding]: "99999" if broken_binding == "remote" else str(uuid.uuid4()),
        }
    elif broken_binding == "student":
        # Keep the mismatched owner transient: no unrelated principal or
        # timestamp can compensate for the actual root ownership binding.
        child.principal_id = uuid.uuid4()
    elif broken_binding == "relation":
        await db.execute(
            delete(MoodleQuizQuestion).where(MoodleQuizQuestion.attempt_id == child.id)
        )
    else:
        child_submission.external_receipt = {"moodle_parent_attempt_id": "99999"}
    with db.no_autoflush:
        identities = await _review_group_identities(db, [
            (item, root if item.attempt_id == root.id else child,
             parent if item.attempt_id == root.id else child_assessment)
            for item in submissions
        ])
    assert child_submission.id not in identities
    assert all(identity[0][2] == student.id for identity in identities.values())


@pytest.mark.parametrize("identity", ["parent", "remote", "source", "student", "course"])
async def test_receipt_grouping_never_merges_conflicting_ownership(db, identity):
    _, teacher, parent, root, questions = await _submitted_quiz(db)
    submissions = list((await db.scalars(select(Submission))).all())
    child = await db.get(Attempt, questions[1].attempt_id)
    child_assessment = await db.get(Assessment, child.assessment_id)
    child_submission = next(item for item in submissions if item.attempt_id == child.id)
    root_submission = next(item for item in submissions if item.attempt_id == root.id)
    root_key = _review_queue_group_key(root_submission, root, parent)
    assert root_key is not None
    if identity == "parent":
        child_submission.external_receipt = {
            **child_submission.external_receipt,
            "moodle_parent_assessment_id": str(uuid.uuid4()),
        }
    elif identity == "remote":
        child_submission.external_receipt = {
            **child_submission.external_receipt,
            "moodle_parent_attempt_id": "unrelated-opaque-attempt",
        }
    elif identity == "source":
        child.integrity_policy = {}
        child_submission.source = "UNVERIFIED"
    elif identity == "student":
        child.principal_id = teacher.id
    else:
        child_assessment.course_id = uuid.uuid4()
    assert _review_queue_group_key(child_submission, child, child_assessment) != root_key
    # This unit must not infer grouping from matching timestamps, question
    # names, a shared work id or a partially conflicting parent binding.


@pytest.mark.parametrize("position", [True, False, 1.5, None, 0, -1, 1001, "1.5"])
def test_review_group_requires_an_exact_bounded_question_position(position):
    submission = Submission(external_receipt={"moodle_response_position": position})
    assert _review_queue_group_position(submission) is None


async def test_detail_does_not_show_one_question_as_a_group_after_deduplication(db):
    _, _, parent, root, questions = await _submitted_quiz(db)
    submissions = list((await db.scalars(select(Submission))).all())
    child_submission = next(
        item for item in submissions if item.attempt_id == questions[1].attempt_id
    )
    child_submission.external_receipt = {
        **child_submission.external_receipt,
        "moodle_response_position": 1,
    }
    native_root = next(item for item in submissions if item.attempt_id == root.id)
    await db.flush()
    assert await _submission_review_group(
        db, submission=native_root, attempt=root, assessment=parent,
    ) is None


async def test_review_shows_the_pinned_statement_of_each_question(db):
    _, teacher, parent, _, _ = await _submitted_quiz(db)
    parent.instructions = "Общее описание курса, не условие вопроса"
    statements = set()
    for submission in (await db.scalars(select(Submission))).all():
        attempt = await db.get(Attempt, submission.attempt_id)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        result = await get_submission(submission.id, _teacher_auth(teacher), db)
        assert result.task_statement == version.statement
        assert result.task_statement != parent.instructions
        assert result.source_warnings == []
        statements.add(result.task_statement)
    assert len(statements) == 2


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
    assert rows[0].parent_assessment_id == parent.id


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
