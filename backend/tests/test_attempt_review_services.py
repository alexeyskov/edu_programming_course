from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import Request
from sqlalchemy import func, select

from app.api import attempts as attempts_api
from app.api.attempts import get_attempt_history
from app.api.reviews import _submission_history, list_submissions
from app.auth.context import AuthContext
from app.models.attempts import (
    Attempt,
    EditEvent,
    MoodleQuizQuestion,
    RunRequest,
    Snapshot,
    Submission,
    Workspace,
    WorkspaceFile,
)
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
)
from app.models.enums import AttemptState, RunOrigin
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ReviewClaim, ReviewDecision, ReviewDraft, TeacherExperimentFile
from app.models.tasks import (
    Assessment,
    AssessmentItem,
    AvailabilityRule,
    TaskBankItem,
    TaskVersion,
)
from app.schemas.attempts import AttemptStartRequest
from app.schemas.runs import RunCreateRequest
from app.services.build_profile import effective_attempt_build_profile
from app.services.common import DomainError, canonical_hash
from app.services.delivery_profile import resolve_assessment_workspace_delivery_profile
from app.services.moodle_materialization import materialize_moodle_activity_drafts
from app.services.moodle_quiz_runtime import (
    prepared_moodle_assignment,
    prepared_moodle_quiz_attempt,
)
from app.services.review import (
    claim_submission,
    create_teacher_experiment,
    finalize_review,
    release_claim,
    reset_teacher_experiment,
    save_review_draft,
    update_experiment_file,
)
from app.services.workspace import (
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_FILES,
    create_clipboard_receipt,
    create_snapshot,
    create_workspace_file,
    delete_workspace_file,
    enqueue_checkpoint,
    replace_file_content,
    retry_submission_checkpoint,
    start_attempt,
    submit_attempt,
)


async def _grant_teacher(db, principal: ExternalPrincipal) -> None:
    token = TeacherAccessToken(
        public_id=principal.id.hex[:16],
        label=f"Test grant: {principal.display_name}",
        secret_hash="$argon2id$test-fixture-not-used-for-login",
        created_by_id=principal.id,
    )
    db.add(token)
    await db.flush()
    db.add(TeacherTokenGrant(token_id=token.id, principal_id=principal.id))
    await db.flush()


async def _seed_course(db):
    connection = LMSConnection(
        name="Moodle",
        provider="MOCK",
        base_url="https://lms.services.test",
    )
    db.add(connection)
    await db.flush()
    student = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="student",
        display_name="Student",
    )
    teacher = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="teacher",
        display_name="Teacher",
    )
    other_teacher = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="teacher-2",
        display_name="Teacher 2",
    )
    db.add_all([student, teacher, other_teacher])
    await db.flush()
    course = Course(
        connection_id=connection.id,
        external_id="cpp",
        title="C++",
        catalog_enabled=True,
    )
    db.add(course)
    await db.flush()
    student_membership = CourseMembership(
        course_id=course.id, principal_id=student.id, role="STUDENT"
    )
    teacher_membership = CourseMembership(
        course_id=course.id, principal_id=teacher.id, role="TEACHER"
    )
    other_teacher_membership = CourseMembership(
        course_id=course.id,
        principal_id=other_teacher.id,
        role="TEACHER",
    )
    review_group = CourseGroup(
        course_id=course.id,
        external_id="reviewers",
        name="Reviewers",
    )
    db.add_all(
        [
            student_membership,
            teacher_membership,
            other_teacher_membership,
            review_group,
        ]
    )
    await db.flush()
    db.add_all(
        [
            CourseMembershipGroup(
                coursemembership_id=membership.id,
                coursegroup_id=review_group.id,
            )
            for membership in (
                student_membership,
                teacher_membership,
                other_teacher_membership,
            )
        ]
    )
    await _grant_teacher(db, teacher)
    await _grant_teacher(db, other_teacher)
    item = TaskBankItem(
        course_id=course.id,
        slug="hello",
        created_by_id=teacher.id,
    )
    db.add(item)
    await db.flush()
    version = TaskVersion(
        item_id=item.id,
        number=1,
        title="Hello",
        statement="Write a program",
        multi_file=True,
        starter_files=[
            {"path": "main.cpp", "content": "int main() {\n}\n"},
            {"path": "value.hpp", "content": "int value = 1;\n"},
        ],
        build_profile="cpp-clang-c++20-multi",
        content_hash="a" * 64,
        status="PUBLISHED",
        authored_by_id=teacher.id,
    )
    db.add(version)
    await db.flush()
    assessment = Assessment(
        course_id=course.id,
        type="LAB",
        title="Lab",
        max_score=Decimal("10"),
        multi_file=True,
        status="PUBLISHED",
        created_by_id=teacher.id,
    )
    db.add(assessment)
    await db.flush()
    db.add(
        AssessmentItem(
            assessment_id=assessment.id,
            task_version_id=version.id,
            points=Decimal("10"),
        )
    )
    await db.flush()
    return student, teacher, other_teacher, assessment


async def _create_submission(db):
    student, teacher, other_teacher, assessment = await _seed_course(db)
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        client_context={
            "ip_address": "203.0.113.10",
            "browser": "Google Chrome",
            "browser_version": "140.0",
            "operating_system": "Windows 10/11",
            "device_type": "DESKTOP",
        },
    )
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    assert workspace is not None
    files = list(
        (
            await db.scalars(
                select(WorkspaceFile)
                .where(WorkspaceFile.workspace_id == workspace.id)
                .order_by(WorkspaceFile.path)
            )
        ).all()
    )
    by_path = {file.path: file for file in files}
    receipt = await create_clipboard_receipt(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        source_file_id=by_path["value.hpp"].id,
        text="int value = 1;\n",
        revision=0,
    )
    paste = await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=by_path["main.cpp"].id,
        content="int main() {\nint value = 1;\n}\n",
        expected_revision=0,
        client_request_id="paste-1",
        source="INTERNAL_PASTE",
        receipt_id=receipt.id,
        client_context={"ip_address": "203.0.113.11", "browser": "Google Chrome"},
    )
    assert paste.event.source == "INTERNAL_PASTE"
    bulk = await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=by_path["main.cpp"].id,
        content=("x" * 300) + paste.file.content,
        expected_revision=1,
        client_request_id="bulk-1",
        source="TYPING",
        client_context={"ip_address": "203.0.113.11", "browser": "Google Chrome"},
    )
    assert bulk.event.source == "UNVERIFIED_BULK_EDIT"
    assert bulk.event.previous_hash == paste.event.event_hash
    submission = await submit_attempt(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        expected_revision=2,
        client_context={"ip_address": "203.0.113.12", "browser": "Firefox"},
    )
    return student, teacher, other_teacher, attempt, submission, by_path["main.cpp"]


async def test_internal_paste_history_and_submission_are_revision_locked(db):
    student, _teacher, _other_teacher, attempt, submission, main_file = await _create_submission(db)

    assert submission.source == "MANUAL"
    assert attempt.assigned_task_version_id is not None
    assert attempt.current_revision == 2
    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=main_file.id,
            content="changed after submit",
            expected_revision=2,
            client_request_id="too-late",
        )
    assert error.value.code == "ATTEMPT_READ_ONLY"


async def test_review_claim_sandbox_and_final_decision_are_human_controlled(db):
    _student, teacher, other_teacher, _attempt, submission, _main_file = await _create_submission(
        db
    )
    claim = await claim_submission(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
    )
    assert claim.owner_id == teacher.id
    with pytest.raises(DomainError) as error:
        await claim_submission(
            db,
            submission_id=submission.id,
            teacher_id=other_teacher.id,
        )
    assert error.value.code == "SUBMISSION_ALREADY_CLAIMED"

    experiment = await create_teacher_experiment(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
    )
    assert experiment.base_snapshot_hash

    request_key = "review-submit-button-request"
    decision = await finalize_review(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
        grade=Decimal("8.5"),
        comment="Checked by teacher",
        idempotency_key=request_key,
    )
    repeated = await finalize_review(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
        grade=Decimal("8.5"),
        comment="Checked by teacher",
        idempotency_key=request_key,
    )
    assert repeated.id == decision.id
    assert decision.status == "APPLIED"
    assert decision.grade == Decimal("8.5")


async def test_submission_without_required_review_is_visible_but_cannot_be_reviewed(db):
    _student, teacher, _other_teacher, attempt, submission, _main_file = await _create_submission(
        db
    )
    assessment = await db.get(Assessment, attempt.assessment_id)
    assert assessment is not None
    assessment.review_required = False
    await db.flush()
    auth = AuthContext(
        principal_id=teacher.id,
        display_name=teacher.display_name,
        session_id=uuid.uuid4(),
        session_key="teacher-session",
        roles=("TEACHER",),
        capabilities=(),
    )

    queue = await list_submissions(
        str(assessment.id),
        auth,
        db,
        offset=0,
        limit=100,
    )
    assert len(queue) == 1
    assert queue[0].id == submission.id
    assert queue[0].review_required is False
    assert queue[0].can_review is False

    with pytest.raises(DomainError) as claim_error:
        await claim_submission(db, submission_id=submission.id, teacher_id=teacher.id)
    assert claim_error.value.code == "REVIEW_NOT_REQUIRED"

    with pytest.raises(DomainError) as draft_error:
        await save_review_draft(
            db,
            submission_id=submission.id,
            teacher_id=teacher.id,
            grade=Decimal("7"),
            comment="Must not be persisted",
        )
    assert draft_error.value.code == "REVIEW_NOT_REQUIRED"

    with pytest.raises(DomainError) as decision_error:
        await finalize_review(
            db,
            submission_id=submission.id,
            teacher_id=teacher.id,
            grade=Decimal("7"),
            comment="Must remain a human-only decision path",
        )
    assert decision_error.value.code == "REVIEW_NOT_REQUIRED"
    assert await db.scalar(select(func.count()).select_from(ReviewClaim)) == 0
    assert await db.scalar(select(func.count()).select_from(ReviewDraft)) == 0
    assert await db.scalar(select(func.count()).select_from(ReviewDecision)) == 0


async def test_new_claim_owner_can_replace_stale_draft(db):
    _student, teacher, other_teacher, _attempt, submission, _main_file = await _create_submission(
        db
    )
    first_claim = await claim_submission(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
    )
    first = await save_review_draft(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
        grade=Decimal("6"),
        comment="First owner's private draft",
    )
    await release_claim(db, claim_id=first_claim.id, teacher_id=teacher.id)
    await claim_submission(
        db,
        submission_id=submission.id,
        teacher_id=other_teacher.id,
    )
    transferred = await save_review_draft(
        db,
        submission_id=submission.id,
        teacher_id=other_teacher.id,
        grade=Decimal("8"),
        comment="Current owner's replacement",
    )
    assert transferred.id == first.id
    assert transferred.owner_id == other_teacher.id
    assert transferred.revision == 2
    assert transferred.comment == "Current owner's replacement"


async def test_review_evidence_is_submission_bound_and_reexport_becomes_pending(db):
    _student, teacher, _other_teacher, _attempt, submission, _main_file = await _create_submission(
        db
    )
    await claim_submission(db, submission_id=submission.id, teacher_id=teacher.id)
    submission.lms_export_state = "DELIVERED"
    submission.external_receipt = {"old": "receipt"}
    await db.flush()

    with pytest.raises(DomainError) as error:
        await finalize_review(
            db,
            submission_id=submission.id,
            teacher_id=teacher.id,
            grade=Decimal("7"),
            comment="Invalid evidence",
            evidence_ids=[uuid.uuid4()],
        )
    assert error.value.code == "INVALID_REVIEW_EVIDENCE"

    decision = await finalize_review(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
        grade=Decimal("7"),
        comment="Snapshot verified",
        evidence_ids=[submission.snapshot_id],
    )
    assert decision.evidence_ids == [str(submission.snapshot_id)]
    assert submission.lms_export_state == "PENDING"
    assert submission.external_receipt == {}


async def test_grade_export_in_progress_serializes_new_review(db):
    _student, teacher, _other_teacher, _attempt, submission, _main_file = await _create_submission(
        db
    )
    await claim_submission(db, submission_id=submission.id, teacher_id=teacher.id)
    decision = ReviewDecision(
        submission_id=submission.id,
        reviewer_id=teacher.id,
        revision=1,
        grade=Decimal("6"),
    )
    db.add(decision)
    await db.flush()
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    course = await db.get(Course, assessment.course_id) if assessment else None
    assert attempt is not None and assessment is not None and course is not None
    db.add(
        SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
            idempotency_key=f"processing:{decision.id}",
            state="PROCESSING",
            locked_at=datetime.now(UTC),
        )
    )
    await db.flush()

    with pytest.raises(DomainError) as error:
        await finalize_review(
            db,
            submission_id=submission.id,
            teacher_id=teacher.id,
            grade=Decimal("8"),
            comment="Must wait",
        )
    assert error.value.code == "GRADE_EXPORT_IN_PROGRESS"


async def test_sandbox_reset_is_expiry_safe_and_revision_monotonic(db):
    _student, teacher, _other_teacher, _attempt, submission, _main_file = await _create_submission(
        db
    )
    await claim_submission(db, submission_id=submission.id, teacher_id=teacher.id)
    experiment = await create_teacher_experiment(
        db,
        submission_id=submission.id,
        teacher_id=teacher.id,
    )
    file = await db.scalar(
        select(TeacherExperimentFile)
        .where(TeacherExperimentFile.experiment_id == experiment.id)
        .order_by(TeacherExperimentFile.path)
    )
    assert file is not None
    experiment, _file = await update_experiment_file(
        db,
        experiment_id=experiment.id,
        teacher_id=teacher.id,
        file_id=file.id,
        content="changed",
        expected_revision=0,
    )

    with pytest.raises(DomainError) as error:
        await reset_teacher_experiment(
            db,
            experiment_id=experiment.id,
            teacher_id=teacher.id,
            expected_revision=0,
        )
    assert error.value.code == "REVISION_CONFLICT"

    reset = await reset_teacher_experiment(
        db,
        experiment_id=experiment.id,
        teacher_id=teacher.id,
        expected_revision=1,
    )
    assert reset.revision == 2

    # Exercise the SQLite-specific naive datetime representation explicitly.
    reset.expires_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    await db.flush()
    with pytest.raises(DomainError) as error:
        await reset_teacher_experiment(
            db,
            experiment_id=reset.id,
            teacher_id=teacher.id,
            expected_revision=2,
        )
    assert error.value.code == "EXPERIMENT_NOT_FOUND"


async def test_official_submission_history_excludes_teacher_experiment_runs(db):
    student, teacher, _other_teacher, attempt, submission, _main_file = await _create_submission(db)
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    assert snapshot is not None
    student_run = RunRequest(
        origin=RunOrigin.STUDENT_ATTEMPT.value,
        attempt_id=attempt.id,
        requested_by_id=student.id,
        revision=snapshot.revision,
        build_profile="cpp-clang-c++20-multi",
        client_context={"ip_address": "203.0.113.13", "browser": "Safari"},
    )
    private_run = RunRequest(
        origin=RunOrigin.TEACHER_EXPERIMENT.value,
        attempt_id=attempt.id,
        submission_id=submission.id,
        requested_by_id=teacher.id,
        revision=snapshot.revision,
        build_profile="cpp-clang-c++20-multi",
    )
    db.add_all([student_run, private_run])
    await db.flush()

    history = await _submission_history(
        db,
        submission=submission,
        attempt=attempt,
        snapshot=snapshot,
    )
    history_ids = {event.id for event in history}
    assert student_run.id in history_ids
    assert private_run.id not in history_ids
    run_history = next(event for event in history if event.id == student_run.id)
    assert run_history.client is not None
    assert run_history.client.ip_address == "203.0.113.13"


async def test_moodle_import_snapshot_has_clear_history_label(db):
    _student, _teacher, _other_teacher, attempt, submission, _main_file = await _create_submission(
        db
    )
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    assert snapshot is not None
    snapshot.reason = "LMS_IMPORT"
    await db.flush()

    history = await _submission_history(
        db,
        submission=submission,
        attempt=attempt,
        snapshot=snapshot,
    )
    imported = next(event for event in history if event.id == snapshot.id)

    assert imported.type == "snapshot"
    assert imported.label == "Импортировано из Moodle"
    assert imported.detail == "История редактирования в Moodle недоступна"


async def _task_version_for(db, assessment: Assessment) -> TaskVersion:
    version = await db.scalar(
        select(TaskVersion)
        .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
        .where(AssessmentItem.assessment_id == assessment.id)
    )
    assert version is not None
    return version


async def _workspace_and_files(db, attempt: Attempt) -> tuple[Workspace, list[WorkspaceFile]]:
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    assert workspace is not None
    files = list(
        (
            await db.scalars(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == workspace.id,
                    WorkspaceFile.deleted_revision.is_(None),
                )
                .order_by(WorkspaceFile.path)
            )
        ).all()
    )
    return workspace, files


async def _map_online_text_transport(db, assessment: Assessment) -> None:
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    connection.provider = "MOODLE"
    db.add(
        ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="moodle_assignment",
            external_id=f"assign-{assessment.id}",
            metadata_json={
                "module": "assign",
                "submission_mode": "ASSIGN_ONLINE_TEXT",
                "sync_state": "CURRENT",
            },
        )
    )
    await db.flush()


async def _map_file_transport(db, assessment: Assessment) -> None:
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    connection.provider = "MOODLE"
    db.add(
        ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="moodle_assignment",
            external_id=f"assign-{assessment.id}",
            metadata_json={
                "module": "assign",
                "submission_mode": "ASSIGN_FILE",
                "sync_state": "CURRENT",
            },
        )
    )
    await db.flush()


async def _map_deferred_random_quiz(db, assessment: Assessment) -> None:
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    course.external_id = "549"
    connection.provider = "MOODLE"
    connection.config = {
        "auth_mode": "PLUGINLESS",
        "pluginless_transport": "PLAYWRIGHT",
    }
    source_confirmation = {
        "title": True,
        "settings": True,
        "statement": False,
        "schedule": True,
        "duration": True,
        "grade": True,
        "attempt_policy": True,
        "statement_deferred": True,
    }
    db.add(
        ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="mod_quiz",
            external_id="30354",
            metadata_json={
                "module": "quiz",
                "cmid": 30354,
                "submission_mode": "REQUIRES_CONFIGURATION",
                "sync_state": "ANSWER_TRANSPORT_UNSUPPORTED",
                "moodle_source_confirmation": source_confirmation,
                "activity": {
                    "module": "quiz",
                    "cmid": 30354,
                    "question_count": 1,
                    "essay_question_count": 0,
                    "random_question_count": 1,
                    "random_essay_confirmed": True,
                    "statement_deferred": True,
                    "import_supported": True,
                    "statement_confirmed": False,
                },
            },
        )
    )
    await db.flush()


def _multi_quiz_preparation(
    remote_attempt_id="141716", second_statement="Напишите функцию.", question_marks=(3, 5)
):
    questions = [
        {
            "question_slot": "1",
            "question_text": "Создайте класс.",
            "answer_transport": "ESSAY_ATTACHMENT",
            "available_answer_transports": ["ESSAY_ATTACHMENT"],
            "question_max_mark": question_marks[0],
        },
        {
            "question_slot": "3",
            "question_text": second_statement,
            "answer_transport": "ESSAY_ONLINE_TEXT",
            "available_answer_transports": ["ESSAY_ONLINE_TEXT"],
            "question_max_mark": question_marks[1],
        },
    ]
    return prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id=remote_attempt_id,
        remaining_seconds=1200,
        questions=questions,
        **questions[0],
    )


async def _start_multi_quiz(db, prepared=None):
    student, teacher, _other_teacher, assessment = await _seed_course(db)
    base_version = await _task_version_for(db, assessment)
    base_version.starter_files = [{"path": "main.cpp", "content": ""}]
    base_version.statement = ""
    await _map_deferred_random_quiz(db, assessment)
    mapping = await db.scalar(
        select(ExternalMapping).where(ExternalMapping.local_id == assessment.id)
    )
    metadata = dict(mapping.metadata_json)
    metadata["activity"] = {
        **metadata["activity"],
        "question_count": 2,
        "essay_question_count": 1,
        "random_question_count": 1,
        "quiz_questions_confirmed": True,
    }
    mapping.metadata_json = metadata
    prepared = prepared or _multi_quiz_preparation()
    root = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=prepared,
    )
    questions = list(
        (
            await db.scalars(
                select(MoodleQuizQuestion)
                .where(MoodleQuizQuestion.root_attempt_id == root.id)
                .order_by(MoodleQuizQuestion.position)
            )
        ).all()
    )
    return student, teacher, assessment, root, questions


async def test_multi_quiz_creates_isolated_writable_solutions_and_shared_dto(db, app_bundle):
    student, _teacher, assessment, root, questions = await _start_multi_quiz(db)
    assert [question.question_slot for question in questions] == ["1", "3"]
    assert questions[0].attempt_id == root.id
    assert [question.question_max_mark for question in questions] == [Decimal(3), Decimal(5)]
    child = await db.get(Attempt, questions[1].attempt_id)
    root_workspace, root_files = await _workspace_and_files(db, root)
    child_workspace, child_files = await _workspace_and_files(db, child)
    assert root_workspace.id != child_workspace.id
    assert root_files[0].id != child_files[0].id
    assert root_files[0].path == child_files[0].path == "main.cpp"
    assert root_workspace.multi_file is True
    assert child_workspace.multi_file is False
    child_version = await db.get(TaskVersion, child.assigned_task_version_id)
    assert child_version.statement == "Напишите функцию."
    assert child_version.max_score == 5

    mutation = await replace_file_content(
        db,
        attempt_id=child.id,
        principal_id=student.id,
        file_id=child_files[0].id,
        content="int main() { return 2; }",
        expected_revision=0,
        client_request_id="multi-question-edit",
    )
    assert mutation.workspace.current_revision == 1
    assert root_files[0].content == ""
    assert child_files[0].content == "int main() { return 2; }"
    snapshot = await create_snapshot(db, child_workspace, "PERIODIC")
    event = await enqueue_checkpoint(db, attempt=child, snapshot=snapshot, reason="PERIODIC")
    assert event.attempt_id == root.id
    assert event.payload["quiz_session_revision"] == 1
    assert {item["question_slot"] for item in event.payload["quiz_questions"]} == {"1", "3"}

    _app, _factory, settings = app_bundle
    for attempt, workspace in [(root, root_workspace), (child, child_workspace)]:
        own_assessment = await db.get(Assessment, attempt.assessment_id)
        dto = await attempts_api._attempt_read(db, attempt, own_assessment, workspace, settings)
        assert dto.assessment_id == assessment.id
        assert dto.state == "ACTIVE"
        assert dto.quiz_session.root_attempt_id == root.id
        assert [item.attempt_id for item in dto.quiz_session.questions] == [root.id, child.id]
        assert dto.expected_end_at == root.expected_end_at
    replay = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=_multi_quiz_preparation(),
    )
    assert replay.id == root.id
    assert await db.scalar(select(func.count()).select_from(MoodleQuizQuestion)) == 2
    assert child_files[0].content == "int main() { return 2; }"


async def test_multi_quiz_finish_any_question_freezes_all_and_retries_root(db, app_bundle):
    student, _teacher, assessment, root, questions = await _start_multi_quiz(db)
    child = await db.get(Attempt, questions[1].attempt_id)
    child_workspace, child_files = await _workspace_and_files(db, child)
    submitted = await submit_attempt(
        db, attempt_id=child.id, principal_id=student.id, expected_revision=0
    )
    assert submitted.attempt_id == child.id
    assert root.state == child.state == "SUBMITTED"
    submissions = list((await db.scalars(select(Submission))).all())
    assert len(submissions) == 2
    assert {item.external_receipt["moodle_response_id"] for item in submissions} == {"1", "3"}
    assert {item.external_receipt["moodle_response_position"] for item in submissions} == {1, 2}
    events = list((await db.scalars(select(SyncOutbox))).all())
    terminal = [event for event in events if event.payload.get("reason") == "SUBMISSION"]
    assert len(terminal) == 1
    event = terminal[0]
    assert event.attempt_id == root.id
    assert {item["snapshot_id"] for item in event.payload["quiz_questions"]} == {
        str(item.snapshot_id) for item in submissions
    }
    with pytest.raises(DomainError) as locked:
        await replace_file_content(
            db,
            attempt_id=child.id,
            principal_id=student.id,
            file_id=child_files[0].id,
            content="changed after submit",
            expected_revision=0,
            client_request_id="after-submit",
        )
    assert locked.value.code == "ATTEMPT_READ_ONLY"
    assert (await submit_attempt(
        db, attempt_id=child.id, principal_id=student.id, expected_revision=0
    )).id == submitted.id
    event.state = "FAILED"
    await retry_submission_checkpoint(db, attempt_id=child.id, principal_id=student.id)
    assert event.state == "RETRY"
    event.state = "DELIVERED"
    event.delivered_at = datetime.now(UTC)
    await db.flush()
    child_assessment = await db.get(Assessment, child.assessment_id)
    dto = await attempts_api._attempt_read(
        db, child, child_assessment, child_workspace, app_bundle[2]
    )
    assert dto.checkpoint_status == "SYNCED"
    assert dto.state == "SUBMITTED"
    second = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=_multi_quiz_preparation("141717"),
    )
    assert second.id != root.id
    assert second.sequence == 2
    assert await db.scalar(select(func.count()).select_from(MoodleQuizQuestion)) == 4


async def test_multi_quiz_rejects_changed_question_identity_on_resume(db):
    student, _teacher, assessment, root, questions = await _start_multi_quiz(db)
    with pytest.raises(DomainError) as changed:
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
            prepared_moodle_quiz=_multi_quiz_preparation(second_statement="A different question"),
        )
    assert changed.value.code == "MOODLE_ATTEMPT_BINDING_CONFLICT"
    assert root.state == "ACTIVE"
    assert await db.scalar(select(func.count()).select_from(MoodleQuizQuestion)) == len(questions)


async def test_multi_quiz_child_run_only_receives_its_own_solution(db, app_bundle, monkeypatch):
    student, _teacher, _assessment, root, questions = await _start_multi_quiz(db)
    child = await db.get(Attempt, questions[1].attempt_id)
    _, root_files = await _workspace_and_files(db, root)
    _, child_files = await _workspace_and_files(db, child)
    for attempt, file, code in [
        (root, root_files[0], "int main() { return 1; }"),
        (child, child_files[0], "int main() { return 2; }"),
    ]:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=file.id,
            content=code,
            expected_revision=0,
            client_request_id=f"edit-{attempt.id}",
        )
    dispatched = []

    async def dispatch(_adapter, **kwargs):
        dispatched.append(kwargs)
        return attempts_api._mock_result(uuid.UUID(kwargs["request_id"]))

    monkeypatch.setattr(attempts_api.RunnerAdapter, "dispatch", dispatch)
    app, _factory, settings = app_bundle
    settings.runner_mock_enabled = False
    settings.runner_url = "http://runner.example.test"
    request = Request({
        "type": "http", "app": app, "headers": [], "client": ("127.0.0.1", 1234)
    })
    auth = AuthContext(
        principal_id=student.id,
        display_name=student.display_name,
        session_id=uuid.uuid4(),
        session_key="student-session",
        roles=("STUDENT",),
        capabilities=(),
    )
    for attempt in (root, child):
        result = await attempts_api.create_student_run(
            attempt.id, RunCreateRequest(revision=1), request, auth, db
        )
        assert result.status == "COMPLETED"
    assert [entry["files"] for entry in dispatched] == [
        [{"path": "main.cpp", "content": "int main() { return 1; }"}],
        [{"path": "main.cpp", "content": "int main() { return 2; }"}],
    ]
    assert dispatched[0]["profile_id"].endswith("-multi")
    assert dispatched[1]["profile_id"].endswith("-single")


async def test_multi_quiz_stale_finish_rolls_back_every_solution(db):
    student, _teacher, _assessment, root, questions = await _start_multi_quiz(db)
    root_id, child_id, student_id = root.id, questions[1].attempt_id, student.id
    with pytest.raises(DomainError) as conflict:
        async with db.begin_nested():
            await submit_attempt(
                db, attempt_id=child_id, principal_id=student_id, expected_revision=10
            )
    assert conflict.value.code == "REVISION_CONFLICT"
    assert await db.scalar(select(func.count()).select_from(Submission)) == 0
    assert (await db.get(Attempt, root_id)).state == "ACTIVE"
    assert (await db.get(Attempt, child_id)).state == "ACTIVE"


async def test_multi_quiz_fractional_marks_keep_exact_binding_and_valid_review_scale(db):
    student, teacher, assessment, root, questions = await _start_multi_quiz(
        db, _multi_quiz_preparation(question_marks=(3.3333333, 0.00001))
    )
    assert [question.question_max_mark for question in questions] == [
        Decimal("3.3333333"), Decimal("0.00001")
    ]
    versions = []
    for question in questions:
        attempt = await db.get(Attempt, question.attempt_id)
        versions.append(await db.get(TaskVersion, attempt.assigned_task_version_id))
    assert [version.max_score for version in versions] == [Decimal("3.33"), Decimal("1.00")]
    await submit_attempt(db, attempt_id=root.id, principal_id=student.id, expected_revision=0)
    auth = AuthContext(
        principal_id=teacher.id,
        display_name=teacher.display_name,
        session_id=uuid.uuid4(),
        session_key="teacher-session",
        roles=("TEACHER",),
        capabilities=(),
    )
    rows = await list_submissions(str(assessment.id), auth, db, offset=0, limit=100)
    assert rows
    for row in rows:
        row.model_dump(mode="json")


async def test_multi_quiz_confirmed_mixed_formats_do_not_report_sync_error(db):
    _student, teacher, _other_teacher, assessment = await _seed_course(db)
    course = await db.get(Course, assessment.course_id)
    activity = {
        "cmid": 31529,
        "module": "quiz",
        "name": "Самостоятельная работа №1",
        "description": "",
        "grade_max": 8,
        "attempt_limit": 10,
        "quiz_grading_method": "HIGHEST",
        "quiz_grading_method_confirmed": True,
        "question_count": 2,
        "essay_question_count": 2,
        "random_question_count": 0,
        "quiz_questions_confirmed": True,
        "import_supported": True,
        "statement_deferred": True,
        "title_confirmed": True,
        "settings_confirmed": True,
        "statement_confirmed": False,
        "schedule_confirmed": True,
        "duration_confirmed": True,
        "grade_confirmed": True,
        "attempt_policy_confirmed": True,
    }
    for expected_created in (1, 0):
        created = await materialize_moodle_activity_drafts(
            db, course=course, activities=[activity], created_by_id=teacher.id
        )
        assert created == expected_created
        mapping = await db.scalar(
            select(ExternalMapping).where(ExternalMapping.external_id == "31529")
        )
        assert mapping.metadata_json["sync_state"] == "CURRENT"
        assert mapping.metadata_json["activity"]["quiz_questions_confirmed"] is True
        assert mapping.metadata_json["moodle_source_confirmation"]["statement_deferred"] is True


@pytest.mark.parametrize(
    ("transport", "multi_file"),
    [("ESSAY_ATTACHMENT", True), ("ESSAY_ONLINE_TEXT", False)],
)
async def test_deferred_random_quiz_binds_runtime_question_and_transport(
    db,
    transport: str,
    multi_file: bool,
) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    base_version = await _task_version_for(db, assessment)
    base_version.multi_file = False
    base_version.build_profile = "cpp-clang-c++20-single"
    base_version.starter_files = [{"path": "main.cpp", "content": ""}]
    base_version.statement = ""
    await _map_deferred_random_quiz(db, assessment)

    with pytest.raises(DomainError) as error:
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
        )
    assert error.value.code == "MOODLE_RUNTIME_PREPARATION_REQUIRED"

    available = (
        ("ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT")
        if transport == "ESSAY_ATTACHMENT"
        else ("ESSAY_ONLINE_TEXT",)
    )
    prepared = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141716",
        question_slot="1",
        question_text="Создайте класс трёхмерного вектора.",
        answer_transport=transport,
        available_answer_transports=available,
    )
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=prepared,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    runtime_version = await db.get(TaskVersion, attempt.assigned_task_version_id)
    assert runtime_version is not None
    assert runtime_version.id != base_version.id
    assert runtime_version.statement == prepared.question_text
    assert runtime_version.multi_file is multi_file
    assert runtime_version.build_profile.endswith("-multi" if multi_file else "-single")
    assert runtime_version.ai_policy["source"] == "MOODLE_RUNTIME_ESSAY"
    assert runtime_version.ai_policy["moodle_attempt_id"] == "141716"
    assert workspace.multi_file is multi_file
    assert [file.path for file in files] == ["main.cpp"]
    assert attempt.integrity_policy["moodle_attempt_id"] == "141716"
    assert attempt.integrity_policy["moodle_question_slot"] == "1"
    assert attempt.integrity_policy["moodle_answer_transport"] == transport

    replay = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=prepared,
    )
    assert replay.id == attempt.id
    runtime_versions = list(
        (
            await db.scalars(select(TaskVersion).where(TaskVersion.item_id == base_version.item_id))
        ).all()
    )
    assert len(runtime_versions) == 2


async def test_deferred_quiz_repairs_legacy_active_attempt_with_runtime_statement(db) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    base_version = await _task_version_for(db, assessment)
    base_version.multi_file = False
    base_version.build_profile = "cpp-clang-c++20-single"
    base_version.starter_files = [
        {"path": "main.cpp", "content": "// сохранённый код студента\nint main() {}\n"}
    ]
    base_version.statement = ""
    assessment.multi_file = False
    await db.flush()

    # This is the shape left by versions which allowed an IDE attempt to be
    # created before the live Moodle Essay preparation was mandatory.
    legacy = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    legacy_workspace, legacy_files = await _workspace_and_files(db, legacy)
    assert legacy.assigned_task_version_id == base_version.id
    assert "moodle_attempt_id" not in legacy.integrity_policy
    assert legacy_workspace.multi_file is False

    await _map_deferred_random_quiz(db, assessment)
    prepared = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141716",
        question_slot="1",
        question_text="Создайте класс трёхмерного вектора.",
        answer_transport="ESSAY_ATTACHMENT",
        available_answer_transports=("ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"),
    )

    resumed = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=prepared,
    )

    assert resumed.id == legacy.id
    runtime_version = await db.get(TaskVersion, resumed.assigned_task_version_id)
    assert runtime_version is not None
    assert runtime_version.statement == prepared.question_text
    assert resumed.integrity_policy["moodle_runtime_prepared"] is True
    assert resumed.integrity_policy["moodle_attempt_id"] == "141716"
    repaired_workspace, repaired_files = await _workspace_and_files(db, resumed)
    assert repaired_workspace.id == legacy_workspace.id
    assert repaired_workspace.multi_file is True
    assert [(row.path, row.content) for row in repaired_files] == [
        (row.path, row.content) for row in legacy_files
    ]


async def test_deferred_quiz_repairs_legacy_active_attempt_with_base_statement_and_deadline(
    db,
) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    base_version = await _task_version_for(db, assessment)
    base_version.multi_file = False
    base_version.build_profile = "cpp-clang-c++20-single"
    base_version.starter_files = [
        {"path": "main.cpp", "content": "// работа уже начата\nint main() { return 0; }\n"}
    ]
    base_version.statement = "Общее условие, импортированное до открытия попытки."
    assessment.multi_file = False
    assessment.duration_seconds = 3_600
    assessment.closes_at = datetime.now(UTC) + timedelta(days=1)
    await db.flush()

    # Simulate an ACTIVE row created by a deployment which still treated the
    # imported activity as local. It carries the global window even though the
    # student can now have an individual Moodle override.
    legacy = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    legacy_workspace, legacy_files = await _workspace_and_files(db, legacy)
    assert legacy.expected_end_at is not None
    assert legacy.deadline_at is not None

    await _map_deferred_random_quiz(db, assessment)
    assessment.policy = {"moodle_metadata_read_only": True}
    db.add(
        AvailabilityRule(
            assessment_id=assessment.id,
            target_type="GROUP",
            target_external_id="reviewers",
            allowed=True,
            authored_by_id=assessment.created_by_id,
        )
    )
    await db.flush()
    prepared = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141716",
        question_slot="1",
        question_text="Персональное условие из открытой попытки Moodle.",
        answer_transport="ESSAY_ATTACHMENT",
        available_answer_transports=("ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"),
    )

    resumed = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=prepared,
    )

    assert resumed.id == legacy.id
    assert resumed.assigned_task_version_id != base_version.id
    assert resumed.expected_end_at is None
    assert resumed.deadline_at is None
    runtime_version = await db.get(TaskVersion, resumed.assigned_task_version_id)
    assert runtime_version is not None
    assert runtime_version.statement == prepared.question_text
    repaired_workspace, repaired_files = await _workspace_and_files(db, resumed)
    assert repaired_workspace.id == legacy_workspace.id
    assert [(row.path, row.content) for row in repaired_files] == [
        (row.path, row.content) for row in legacy_files
    ]


async def test_deferred_quiz_rejects_legacy_active_attempt_with_partial_binding(db) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    base_version = await _task_version_for(db, assessment)
    base_version.statement = "Общее условие."
    base_version.starter_files = [
        {"path": "main.cpp", "content": "// не потерять\nint main() {}\n"}
    ]
    legacy = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    legacy.integrity_policy = {
        **legacy.integrity_policy,
        "moodle_attempt_id": "141716",
    }
    legacy_workspace, legacy_files = await _workspace_and_files(db, legacy)

    await _map_deferred_random_quiz(db, assessment)
    prepared = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141716",
        question_slot="1",
        question_text="Персональное условие Moodle.",
        answer_transport="ESSAY_ATTACHMENT",
        available_answer_transports=("ESSAY_ATTACHMENT",),
    )

    with pytest.raises(DomainError) as error:
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
            prepared_moodle_quiz=prepared,
        )

    assert error.value.code == "MOODLE_ATTEMPT_BINDING_CONFLICT"
    assert legacy.assigned_task_version_id == base_version.id
    untouched_workspace, untouched_files = await _workspace_and_files(db, legacy)
    assert untouched_workspace.id == legacy_workspace.id
    assert [(row.path, row.content) for row in untouched_files] == [
        (row.path, row.content) for row in legacy_files
    ]


async def test_moodle_assignment_repairs_legacy_active_attempt_transport(db) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    base_version = await _task_version_for(db, assessment)
    base_version.multi_file = False
    base_version.build_profile = "cpp-clang-c++20-single"
    base_version.starter_files = [{"path": "main.cpp", "content": "int main() {}\n"}]
    assessment.instructions = "Прочитайте число и выведите ответ."
    assessment.multi_file = False
    assessment.duration_seconds = 3_600
    assessment.closes_at = datetime.now(UTC) + timedelta(days=1)
    await db.flush()
    legacy = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    assert legacy.expected_end_at is not None
    assert legacy.deadline_at is not None

    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    course.external_id = "549"
    connection.provider = "MOODLE"
    connection.config = {"auth_mode": "PLUGINLESS", "pluginless_transport": "PLAYWRIGHT"}
    assessment.policy = {"moodle_metadata_read_only": True}
    db.add_all(
        [
            AvailabilityRule(
                assessment_id=assessment.id,
                target_type="GROUP",
                target_external_id="reviewers",
                allowed=True,
                authored_by_id=assessment.created_by_id,
            ),
            ExternalMapping(
                connection_id=course.connection_id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type="mod_assign",
                external_id="23461",
                metadata_json={"module": "assign", "cmid": 23461},
            ),
        ]
    )
    await db.flush()

    resumed = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_assignment=prepared_moodle_assignment(
            course_external_id="549",
            cmid=23461,
            answer_transport="ASSIGN_FILE",
            available_answer_transports=("ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"),
        ),
    )

    assert resumed.id == legacy.id
    assert resumed.integrity_policy["moodle_runtime_prepared"] is True
    assert resumed.integrity_policy["moodle_answer_transport"] == "ASSIGN_FILE"
    assert resumed.expected_end_at is None
    assert resumed.deadline_at is None
    workspace, _files = await _workspace_and_files(db, resumed)
    assert workspace.multi_file is True


async def test_deferred_quiz_does_not_bind_one_moodle_attempt_twice(db) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    assessment.attempt_limit = 2
    base_version = await _task_version_for(db, assessment)
    base_version.statement = ""
    base_version.starter_files = [{"path": "main.cpp", "content": ""}]
    await _map_deferred_random_quiz(db, assessment)
    first_preparation = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141716",
        question_slot="1",
        question_text="Первый случайный вопрос",
        answer_transport="ESSAY_ONLINE_TEXT",
        available_answer_transports=("ESSAY_ONLINE_TEXT",),
    )
    first = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=first_preparation,
    )
    first.state = AttemptState.SUBMITTED.value
    await db.flush()

    with pytest.raises(DomainError) as error:
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
            prepared_moodle_quiz=first_preparation,
        )
    assert error.value.code == "MOODLE_ATTEMPT_STILL_FINALIZING"

    second_preparation = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141717",
        question_slot="1",
        question_text="Второй случайный вопрос",
        answer_transport="ESSAY_ATTACHMENT",
        available_answer_transports=("ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"),
    )
    second = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=second_preparation,
    )
    assert second.sequence == 2
    assert second.integrity_policy["moodle_attempt_id"] == "141717"


async def test_start_endpoint_opens_new_moodle_quiz_attempt_while_old_delivery_is_pending(
    db,
    app_bundle,
    monkeypatch,
) -> None:
    app, _session_factory, _settings = app_bundle
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    course.sync_status = "SYNCING"
    base_version = await _task_version_for(db, assessment)
    base_version.statement = ""
    base_version.starter_files = [{"path": "main.cpp", "content": ""}]
    await _map_deferred_random_quiz(db, assessment)

    first = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=prepared_moodle_quiz_attempt(
            course_external_id="549",
            cmid=30354,
            external_attempt_id="141716",
            question_slot="1",
            question_text="Первая попытка",
            answer_transport="ESSAY_ONLINE_TEXT",
            available_answer_transports=("ESSAY_ONLINE_TEXT",),
        ),
    )
    first.state = AttemptState.SUBMITTED.value
    first.submitted_at = datetime.now(UTC)
    # No delivered terminal checkpoint exists: this is the stale local state
    # that previously forced the endpoint to return the read-only first IDE.
    await db.flush()

    second_preparation = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141717",
        question_slot="1",
        question_text="Вторая попытка",
        answer_transport="ESSAY_ONLINE_TEXT",
        available_answer_transports=("ESSAY_ONLINE_TEXT",),
    )

    async def prepare_next(
        _db,
        _settings: Any,
        *,
        assessment_id: uuid.UUID,
        principal_id: uuid.UUID,
    ):
        assert assessment_id == assessment.id
        assert principal_id == student.id
        return second_preparation

    monkeypatch.setattr(attempts_api, "_prepare_moodle_quiz_attempt", prepare_next)
    request = Request(
        {
            "type": "http",
            "app": app,
            "method": "POST",
            "path": f"/api/v1/assessments/{assessment.id}/attempts",
            "query_string": b"",
            "headers": [(b"user-agent", b"pytest")],
            "client": ("203.0.113.42", 43120),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )
    auth = AuthContext(
        principal_id=student.id,
        display_name=student.display_name,
        session_id=uuid.uuid4(),
        session_key="student-next-moodle-attempt",
        roles=("STUDENT",),
        capabilities=(),
    )

    opened = await attempts_api.create_attempt(
        assessment.id,
        AttemptStartRequest(),
        request,
        auth,
        db,
    )

    attempts = list(
        (
            await db.scalars(
                select(Attempt)
                .where(
                    Attempt.assessment_id == assessment.id,
                    Attempt.principal_id == student.id,
                )
                .order_by(Attempt.sequence)
            )
        ).all()
    )
    assert opened.id != first.id
    assert opened.sequence == 2
    assert opened.state == AttemptState.ACTIVE.value
    assert [row.sequence for row in attempts] == [1, 2]
    assert attempts[1].integrity_policy["moodle_attempt_id"] == "141717"


async def test_managed_moodle_quiz_accepts_a_new_live_retry_beyond_stale_local_limit(db) -> None:
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    assessment.attempt_limit = 1
    assessment.policy = {
        "moodle_metadata_read_only": True,
        "moodle_source_confirmation": {
            "title": True,
            "settings": True,
            "statement": False,
            "schedule": True,
            "duration": True,
            "grade": True,
            "attempt_policy": True,
            "statement_deferred": True,
        },
    }
    base_version = await _task_version_for(db, assessment)
    base_version.statement = ""
    base_version.starter_files = [{"path": "main.cpp", "content": ""}]
    db.add(
        AvailabilityRule(
            assessment_id=assessment.id,
            target_type="GROUP",
            target_external_id="reviewers",
            allowed=True,
            authored_by_id=assessment.created_by_id,
        )
    )
    await _map_deferred_random_quiz(db, assessment)

    first_preparation = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141716",
        question_slot="1",
        question_text="Первая попытка",
        answer_transport="ESSAY_ONLINE_TEXT",
        available_answer_transports=("ESSAY_ONLINE_TEXT",),
    )
    first = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=first_preparation,
    )
    first.state = AttemptState.SUBMITTED.value
    await db.flush()

    second_preparation = prepared_moodle_quiz_attempt(
        course_external_id="549",
        cmid=30354,
        external_attempt_id="141717",
        question_slot="1",
        question_text="Повторная попытка",
        answer_transport="ESSAY_ONLINE_TEXT",
        available_answer_transports=("ESSAY_ONLINE_TEXT",),
    )
    second = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=second_preparation,
    )

    assert second.sequence == 2
    assert second.integrity_policy["moodle_attempt_id"] == "141717"


async def test_single_file_workspace_allows_only_additional_text_data_files(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    version = await _task_version_for(db, assessment)
    assessment.multi_file = False
    version.multi_file = False
    version.build_profile = "cpp-clang-c++20-single"
    version.starter_files = [
        {"path": "main.cpp", "content": "int main() {}\n"},
        {"path": "fixtures/starter.txt", "content": "starter data\n"},
    ]
    await db.flush()

    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    main = next(file for file in files if file.path == "main.cpp")
    starter_data = next(file for file in files if file.path == "fixtures/starter.txt")
    assert starter_data.language == "TEXT"

    created = await create_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        path="fixtures/input.txt",
        content="",
        expected_revision=0,
        client_request_id="single-create-text",
    )
    assert created.file.language == "TEXT"
    assert workspace.current_revision == 1

    with pytest.raises(DomainError) as error:
        await create_workspace_file(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            path="helper.cpp",
            content="",
            expected_revision=1,
            client_request_id="single-create-source",
        )
    assert error.value.code == "SINGLE_FILE_ASSESSMENT"

    with pytest.raises(DomainError) as error:
        await delete_workspace_file(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=main.id,
            expected_revision=1,
            client_request_id="single-delete-source",
        )
    assert error.value.code == "LAST_TRANSLATION_UNIT"

    deleted = await delete_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=created.file.id,
        expected_revision=1,
        client_request_id="single-delete-text",
    )
    assert deleted.file.deleted_revision == 2
    deleted_starter = await delete_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=starter_data.id,
        expected_revision=2,
        client_request_id="single-delete-starter-text",
    )
    assert deleted_starter.file.deleted_revision == 3
    _workspace, remaining = await _workspace_and_files(db, attempt)
    assert [file.path for file in remaining] == ["main.cpp"]


async def test_online_text_workspace_keeps_companion_text_files_for_program_input(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    assessment.multi_file = False
    version = await _task_version_for(db, assessment)
    version.multi_file = False
    version.build_profile = "cpp-clang-c++20-single"
    version.starter_files = [{"path": "main.cpp", "content": "int main() {}\n"}]
    await _map_online_text_transport(db, assessment)

    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    created = await create_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        path="input.txt",
        content="42\n",
        expected_revision=0,
        client_request_id="online-text-create-data",
    )
    assert created.file.path == "input.txt"
    assert created.file.content == "42\n"


async def test_online_text_attempt_allows_companion_text_starter_file(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    assessment.multi_file = False
    version = await _task_version_for(db, assessment)
    version.multi_file = False
    version.build_profile = "cpp-clang-c++20-single"
    version.starter_files = [
        {"path": "main.cpp", "content": "int main() {}\n"},
        {"path": "input.txt", "content": "42\n"},
    ]
    await _map_online_text_transport(db, assessment)

    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    _workspace, files = await _workspace_and_files(db, attempt)
    assert [file.path for file in files] == ["input.txt", "main.cpp"]


async def test_file_delivery_profile_opens_a_multifile_workspace_without_local_reconfiguration(
    db,
):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    assessment.multi_file = False
    version = await _task_version_for(db, assessment)
    version.multi_file = False
    version.build_profile = "cpp-clang-c++20-single"
    version.starter_files = [{"path": "main.cpp", "content": "int main() {}\n"}]
    await _map_file_transport(db, assessment)

    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, _files = await _workspace_and_files(db, attempt)
    assert workspace.multi_file is True

    header = await create_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        path="include/value.hpp",
        content="constexpr int value = 42;\n",
        expected_revision=0,
        client_request_id="file-profile-create-header",
    )
    assert header.file.path == "include/value.hpp"

    helper = await create_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        path="helper.cpp",
        content="int helper() { return 42; }\n",
        expected_revision=1,
        client_request_id="file-profile-create-second-source",
    )
    assert helper.file.path == "helper.cpp"
    assert (
        await effective_attempt_build_profile(
            db,
            attempt=attempt,
            configured_profile=version.build_profile,
        )
        == "cpp-clang-c++20-multi"
    )


async def test_unresolved_external_delivery_profile_stops_attempt_without_guessing(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    connection.provider = "MOODLE"
    db.add(
        ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="moodle_assignment",
            external_id=f"assign-{assessment.id}",
            metadata_json={
                "module": "assign",
                "submission_mode": "REQUIRES_CONFIGURATION",
            },
        )
    )
    await db.flush()

    with pytest.raises(DomainError) as error:
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
        )
    assert error.value.code == "LMS_DELIVERY_PROFILE_UNRESOLVED"
    assert (
        await db.scalar(
            select(func.count()).select_from(Attempt).where(Attempt.assessment_id == assessment.id)
        )
        == 0
    )


async def test_stale_external_delivery_profile_stops_attempt_until_course_refresh(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    connection.provider = "MOODLE"
    db.add(
        ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="moodle_assignment",
            external_id=f"stale-assign-{assessment.id}",
            metadata_json={
                "module": "assign",
                "submission_mode": "ASSIGN_FILE",
                "sync_state": "MISSING_IN_MOODLE",
            },
        )
    )
    await db.flush()

    with pytest.raises(DomainError) as error:
        await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    assert error.value.code == "LMS_DELIVERY_PROFILE_UNRESOLVED"


@pytest.mark.parametrize("second_transport", ["ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"])
async def test_duplicate_or_conflicting_current_mappings_never_guess_workspace_shape(
    db,
    second_transport: str,
):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    await _map_file_transport(db, assessment)
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    db.add(
        ExternalMapping(
            connection_id=course.connection_id,
            local_type="core.assessment",
            local_id=assessment.id,
            external_type="mod_assign",
            external_id=f"duplicate-{assessment.id}",
            metadata_json={
                "module": "assign",
                "submission_mode": second_transport,
                "sync_state": "CURRENT",
            },
        )
    )
    await db.flush()

    with pytest.raises(DomainError) as error:
        await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    assert error.value.code == "LMS_DELIVERY_PROFILE_UNRESOLVED"


async def test_delivery_profile_ignores_mapping_from_another_connection(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    unrelated = LMSConnection(
        name="Other Moodle",
        provider="MOODLE",
        base_url="https://other-moodle.services.test",
    )
    db.add(unrelated)
    await db.flush()
    db.add(
        ExternalMapping(
            connection_id=unrelated.id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="moodle_assignment",
            external_id=f"foreign-{assessment.id}",
            metadata_json={
                "module": "assign",
                "submission_mode": "ASSIGN_ONLINE_TEXT",
                "sync_state": "CURRENT",
            },
        )
    )
    await db.flush()

    attempt = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    workspace, _files = await _workspace_and_files(db, attempt)
    assert workspace.multi_file is True


async def test_delivery_profile_rejects_disabled_course_connection(db):
    _student, _teacher, _other_teacher, assessment = await _seed_course(db)
    await _map_file_transport(db, assessment)
    course = await db.get(Course, assessment.course_id)
    assert course is not None
    connection = await db.get(LMSConnection, course.connection_id)
    assert connection is not None
    connection.enabled = False
    await db.flush()

    resolution = await resolve_assessment_workspace_delivery_profile(db, assessment.id)
    assert resolution.external is True
    assert resolution.profile is None
    assert resolution.mapping is None


async def test_multi_file_workspace_keeps_its_last_translation_unit(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    main = next(file for file in files if file.path == "main.cpp")
    created = await create_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        path="input.txt",
        content="test data\n",
        expected_revision=0,
        client_request_id="multi-create-text",
    )
    assert created.file.language == "TEXT"

    with pytest.raises(DomainError) as error:
        await delete_workspace_file(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=main.id,
            expected_revision=1,
            client_request_id="multi-delete-last-source",
        )
    assert error.value.code == "LAST_TRANSLATION_UNIT"
    assert workspace.current_revision == 1

    deleted = await delete_workspace_file(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=created.file.id,
        expected_revision=1,
        client_request_id="multi-delete-text",
    )
    assert deleted.workspace.current_revision == 2


async def test_revoked_membership_blocks_mutation_but_deadline_autosubmit_survives(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == assessment.course_id,
            CourseMembership.principal_id == student.id,
            CourseMembership.role == "STUDENT",
        )
    )
    assert membership is not None
    membership.active = False
    attempt.deadline_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.flush()

    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=files[0].id,
            content="blocked",
            expected_revision=workspace.current_revision,
            client_request_id="revoked-edit",
        )
    assert error.value.code == "COURSE_MEMBERSHIP_REQUIRED"

    submission = await submit_attempt(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        expected_revision=workspace.current_revision,
        source="deadline",
    )
    assert submission.source == "DEADLINE"
    assert submission.late is True
    assert attempt.state == "AUTO_SUBMITTED"


async def test_manual_submit_is_strictly_rejected_at_naive_sqlite_deadline(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, _files = await _workspace_and_files(db, attempt)
    # SQLite returns timezone columns as naive datetimes.  The service must still
    # compare this value against an aware UTC clock without accepting a late submit.
    attempt.deadline_at = datetime.now(UTC).replace(tzinfo=None) - timedelta(seconds=1)
    await db.flush()

    with pytest.raises(DomainError) as error:
        await submit_attempt(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            expected_revision=workspace.current_revision,
            source="MANUAL",
        )
    assert error.value.code == "DEADLINE_PASSED"
    assert (
        await db.scalar(
            select(func.count()).select_from(Submission).where(Submission.attempt_id == attempt.id)
        )
        == 0
    )


async def test_starter_and_file_create_limits_are_transactional(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    version = await _task_version_for(db, assessment)
    version.starter_files = [
        {"path": f"file_{index}.hpp", "content": ""} for index in range(MAX_WORKSPACE_FILES + 1)
    ]
    await db.flush()

    with pytest.raises(DomainError) as error:
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
        )
    assert error.value.code == "WORKSPACE_FILE_LIMIT_EXCEEDED"
    assert (
        await db.scalar(
            select(func.count()).select_from(Attempt).where(Attempt.assessment_id == assessment.id)
        )
        == 0
    )

    version.starter_files = [
        {"path": f"file_{index}.hpp", "content": ""} for index in range(MAX_WORKSPACE_FILES)
    ]
    await db.flush()
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    assert len(files) == MAX_WORKSPACE_FILES

    with pytest.raises(DomainError) as error:
        await create_workspace_file(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            path="overflow.hpp",
            content="",
            expected_revision=0,
            client_request_id="overflow-create",
        )
    assert error.value.code == "WORKSPACE_FILE_LIMIT_EXCEEDED"
    assert workspace.current_revision == 0
    assert len((await _workspace_and_files(db, attempt))[1]) == MAX_WORKSPACE_FILES


async def test_replace_enforces_total_utf8_workspace_size_without_partial_write(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    version = await _task_version_for(db, assessment)
    initial_size = 170_000
    version.starter_files = [
        {"path": "a.cpp", "content": "a" * initial_size},
        {"path": "b.hpp", "content": "b" * initial_size},
        {"path": "c.hpp", "content": "c" * initial_size},
    ]
    await db.flush()
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    target = next(file for file in files if file.path == "a.cpp")
    original_hash = target.content_hash
    original_size = workspace.aggregate_size
    assert original_size <= MAX_WORKSPACE_BYTES

    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=target.id,
            content="x" * 190_000,
            expected_revision=0,
            client_request_id="workspace-too-large",
        )
    assert error.value.code == "WORKSPACE_SIZE_LIMIT_EXCEEDED"
    assert workspace.current_revision == 0
    assert workspace.aggregate_size == original_size
    assert target.content_hash == original_hash
    assert (
        await db.scalar(
            select(func.count())
            .select_from(EditEvent)
            .where(EditEvent.workspace_id == workspace.id)
        )
        == 0
    )


async def test_idempotency_payload_conflict_and_event_hash_cover_request_metadata(db):
    student, _teacher, _other_teacher, assessment = await _seed_course(db)
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
    )
    workspace, files = await _workspace_and_files(db, attempt)
    target = next(file for file in files if file.path == "main.cpp")
    mutation = await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=target.id,
        content="int main() { return 0; }\n",
        expected_revision=0,
        client_request_id="stable-request",
        client_id="browser-tab-1",
        client_context={
            "ip_address": "203.0.113.42",
            "browser": "Firefox",
            "browser_version": "142.0",
            "operating_system": "Linux",
            "device_type": "DESKTOP",
        },
    )
    replay = await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=target.id,
        content="int main() { return 0; }\n",
        expected_revision=0,
        client_request_id="stable-request",
        client_id="browser-tab-1",
        client_context={"ip_address": "198.51.100.8"},
    )
    assert replay.event.id == mutation.event.id
    assert replay.event.client_context["ip_address"] == "203.0.113.42"

    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=target.id,
            content="int main() { return 1; }\n",
            expected_revision=0,
            client_request_id="stable-request",
            client_id="browser-tab-1",
        )
    assert error.value.code == "IDEMPOTENCY_CONFLICT"

    event = mutation.event
    expected_event_hash = canonical_hash(
        {
            "previous": event.previous_hash,
            "epoch": event.epoch,
            "sequence": event.sequence,
            "client_id": event.client_id,
            "client_request_id": event.client_request_id,
            "file_id": str(event.file_id),
            "source": event.source,
            "event_type": event.event_type,
            "changes": event.changes,
            "received_at": event.received_at.isoformat(),
            "client_context": event.client_context,
        }
    )
    assert event.event_hash == expected_event_hash
    assert event.changes[0]["request_hash"]
    assert workspace.event_chain_head == expected_event_hash


async def test_submission_has_immutable_history_event_and_external_chain_anchor(db):
    student, _teacher, _other_teacher, attempt, submission, _main_file = await _create_submission(
        db
    )
    auth = AuthContext(
        principal_id=student.id,
        display_name=student.display_name,
        session_id=uuid.uuid4(),
        session_key="test-session",
        roles=("STUDENT",),
        capabilities=(),
    )
    history = await get_attempt_history(attempt.id, auth, db)
    terminal = [row for row in history if row.type == "submit"]
    assert len(terminal) == 1
    assert terminal[0].id == submission.id
    assert terminal[0].detail == "MANUAL"
    assert terminal[0].client is not None
    assert terminal[0].client.ip_address == "203.0.113.12"
    started = next(row for row in history if row.label == "Начало попытки")
    assert started.client is not None
    assert started.client.browser == "Google Chrome"
    edits = [row for row in history if row.type in {"edit", "internal_paste"}]
    assert edits
    assert all(row.client and row.client.ip_address == "203.0.113.11" for row in edits)

    snapshot = await db.get(Snapshot, submission.snapshot_id)
    assert snapshot is not None
    checkpoints = list(
        (await db.scalars(select(SyncOutbox).where(SyncOutbox.aggregate_id == snapshot.id))).all()
    )
    checkpoint = next(
        (row for row in checkpoints if row.payload.get("reason") == "SUBMISSION"),
        None,
    )
    assert checkpoint is not None
    assert checkpoint.payload["manifest_hash"] == snapshot.manifest_hash
    assert checkpoint.payload["event_chain_head"] == snapshot.event_chain_head
    assert checkpoint.payload["epoch"] == attempt.epoch
