from __future__ import annotations

import copy
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.attempts import Attempt, Snapshot, Submission, Workspace, WorkspaceFile
from app.models.courses import Course
from app.models.integration import ExternalMapping
from app.models.review import ReviewDecision, ReviewDraft
from app.models.tasks import Assessment, AssessmentItem, TaskBankItem, TaskVersion
from app.services.moodle_history import materialize_historical_submissions
from app.services.moodle_statement_upgrade import STATEMENT_FORMAT
from tests.test_moodle_history import _finished_item, _seed_history_target

PLAIN = "Вычислить сумму. Вызвать функцию."
RICH = (
    "Вычислить сумму.\n\n![Формула](<https://moodle.example.test/filter/tex/pix.php/"
    "94e2a3db3e7df256861affa0e53fd454.png>)\n\nВызвать функцию."
)
_VERSION_FIELDS = (
    "title",
    "language",
    "language_standard",
    "multi_file",
    "starter_files",
    "build_profile",
    "public_examples",
    "hidden_test_manifest",
    "max_score",
    "difficulty",
    "authored_by_id",
)


async def _seed_import(sessions, *, module):
    ids = await _seed_history_target(sessions)
    item = _finished_item()
    item["module"] = module
    item["external_id"] = f"{module}:777:attempt:123"
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assessment.instructions = PLAIN
        attached = await db.scalar(select(AssessmentItem))
        previous = await db.get(TaskVersion, attached.task_version_id)
        previous.statement = PLAIN
        previous.ai_policy = {"source": "MOODLE_ACTIVITY", "moodle_metadata_read_only": True}
        mapping = await db.scalar(select(ExternalMapping))
        mapping.external_type = f"mod_{module}"
        mapping.metadata_json = {
            **dict(mapping.metadata_json or {}),
            "module": module,
            "managed_by": "MOODLE_ACTIVITY_IMPORT",
        }
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        submission = await db.scalar(select(Submission))
        db.add(
            ReviewDraft(
                submission_id=submission.id,
                owner_id=ids["teacher_id"],
                grade=Decimal("7"),
                comment="Private unsent teacher draft",
            )
        )
        ids["previous_id"] = previous.id
    return ids, item


async def _new_version(db, ids):
    previous = await db.get(TaskVersion, ids["previous_id"])
    current = TaskVersion(
        **{field: copy.deepcopy(getattr(previous, field)) for field in _VERSION_FIELDS},
        item_id=previous.item_id,
        number=2,
        statement=RICH,
        status="PUBLISHED",
        content_hash="b" * 64,
        ai_policy={**previous.ai_policy, "statement_format": STATEMENT_FORMAT},
    )
    db.add(current)
    await db.flush()
    attached = await db.scalar(select(AssessmentItem))
    attached.task_version_id = current.id
    assessment = await db.get(Assessment, ids["assessment_id"])
    assessment.instructions = RICH
    return current


async def _retained_state(db):
    submission = await db.scalar(select(Submission))
    workspace = await db.scalar(select(Workspace))
    decision = await db.scalar(select(ReviewDecision))
    draft = await db.scalar(select(ReviewDraft))
    files = (await db.scalars(select(WorkspaceFile))).all()
    return (
        submission.id,
        submission.snapshot_id,
        submission.revision,
        submission.submitted_at,
        submission.lms_export_state,
        workspace.current_revision,
        workspace.current_hash,
        decision.id,
        decision.revision,
        decision.grade,
        decision.comment,
        decision.status,
        decision.lms_export_state,
        draft.id,
        draft.revision,
        draft.grade,
        draft.comment,
        [(row.id, row.content, row.content_hash) for row in files],
        await db.scalar(select(func.count(Snapshot.id))),
    )


@pytest.mark.parametrize("module", ["assign", "quiz"])
async def test_unchanged_import_restores_only_statement_images_and_preserves_answer_and_review(
    app_bundle,
    module,
):
    _, sessions, _ = app_bundle
    ids, item = await _seed_import(sessions, module=module)
    async with sessions() as db, db.begin():
        current = await _new_version(db, ids)
        before = await _retained_state(db)
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        for _ in range(2):
            stats = await materialize_historical_submissions(
                db,
                course=course,
                assessment=assessment,
                actor_external_subject="42",
                items=[item],
                restore_evicted=True,
            )
            assert stats.unchanged == 1 and stats.updated == 0
        await db.flush()
        assert await _retained_state(db) == before
        attempt = await db.scalar(select(Attempt))
        assert attempt.assigned_task_version_id == current.id
        previous = await db.get(TaskVersion, ids["previous_id"])
        assert previous.statement == PLAIN  # Published predecessor is untouched.
        assert previous.content_hash == "a" * 64


@pytest.mark.parametrize(
    "change",
    [
        "task_words",
        "grade",
        "build",
        "policy",
        "title",
        "task_item",
        "native_attempt",
        "unmanaged_activity",
        "different_activity",
        "already_formatted",
        "already_has_images",
        "missing_source_proof",
    ],
)
async def test_image_repair_does_not_switch_a_different_or_unproved_task(app_bundle, change):
    _, sessions, _ = app_bundle
    ids, item = await _seed_import(sessions, module="assign")
    async with sessions() as db, db.begin():
        current = await _new_version(db, ids)
        previous = await db.get(TaskVersion, ids["previous_id"])
        attempt = await db.scalar(select(Attempt))
        activity = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == "mod_assign",
            )
        )
        if change == "task_words":
            current.statement = RICH.replace("сумму", "произведение")
        elif change == "grade":
            current.max_score = Decimal("20")
        elif change == "build":
            current.build_profile = "different-build-profile"
        elif change == "policy":
            current.ai_policy = {**current.ai_policy, "different_source_policy": True}
        elif change == "title":
            current.title = "Changed assignment title"
        elif change == "task_item":
            other_item = TaskBankItem(
                course_id=ids["course_id"],
                slug="other-task",
                created_by_id=ids["teacher_id"],
            )
            db.add(other_item)
            await db.flush()
            current.item_id = other_item.id
        elif change == "native_attempt":
            attempt.submission_source = "USER"
        elif change == "unmanaged_activity":
            activity.metadata_json = {"managed_by": "LOCAL_MANUAL_MAPPING"}
        elif change == "different_activity":
            activity.external_id = "778"
        elif change == "already_formatted":
            previous.ai_policy = {**previous.ai_policy, "statement_format": STATEMENT_FORMAT}
        elif change == "already_has_images":
            previous.statement = RICH
        elif change == "missing_source_proof":
            previous.ai_policy = {}
        await db.flush()
        before = await _retained_state(db)
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
            restore_evicted=True,
        )
        await db.flush()
        assert stats.unchanged == 1
        assert attempt.assigned_task_version_id == previous.id
        assert await _retained_state(db) == before
