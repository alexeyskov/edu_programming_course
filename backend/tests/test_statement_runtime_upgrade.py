from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.models.attempts import Attempt, EditEvent, Snapshot, Workspace, WorkspaceFile
from app.models.tasks import TaskVersion
from app.services.common import DomainError, canonical_hash
from app.services.moodle_quiz_runtime import _task_content
from app.services.moodle_statement_upgrade import STATEMENT_FORMAT, is_statement_image_upgrade
from app.services.statement_markup import statement_image_urls
from app.services.workspace import replace_file_content, start_attempt
from tests.test_attempt_review_services import (
    _multi_quiz_preparation,
    _start_multi_quiz,
    _workspace_and_files,
)

URL = "https://moodle.example.test/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png"
IMAGE = rf"![\\sum_{{k=1}}^{{N}}\\frac{{1}}{{k\\sqrt{{k}}}}](<{URL}>)"


@pytest.mark.parametrize(
    ("previous", "enriched", "expected"),
    [
        ("Вычислите сумму.", f"Вычислите сумму. {IMAGE}", True),
        ("Вычислите сумму. Вызовите функцию.",
         f"Вычислите сумму.\n{IMAGE}\nВызовите функцию.", True),
        ("int f(int);", f"int f(int); {IMAGE}", True),
        ("Вычислите сумму.", f"Другая задача. {IMAGE}", False),
        ("Вычислите сумму.", "Вычислите сумму.", False),
        (f"Вычислите сумму. {IMAGE}", f"Вычислите сумму. {IMAGE} {IMAGE}", False),
        ("", IMAGE, False),
        ("Вычислите сумму.", 'Вычислите сумму. <img src="image.png">', False),
    ],
)
def test_only_legacy_image_loss_can_be_upgraded(previous, enriched, expected):
    assert is_statement_image_upgrade(previous, enriched) is expected


def test_canonical_formula_url_survives_tex_and_alt_escaping():
    assert statement_image_urls(f"До {IMAGE} после {IMAGE}") == [URL]
    assert statement_image_urls(rf"![a\[0\] \\sqrt{{n}}](<{URL}>)") == [URL]
    assert statement_image_urls(f'<img src="{URL}">') == []
    assert statement_image_urls(f"![broken](<{URL}>") == []
    assert statement_image_urls(rf"![bad\escape](<{URL}>)") == []


async def _make_legacy_versions(db, questions):
    originals = []
    for question in questions:
        attempt = await db.get(Attempt, question.attempt_id)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        policy = dict(version.ai_policy)
        policy.pop("statement_format", None)
        version.ai_policy = policy
        version.content_hash = canonical_hash(_task_content(version))
        originals.append((version.id, version.statement, version.content_hash))
    await db.flush()
    return originals


async def test_resuming_legacy_quiz_restores_images_for_every_question_without_touching_code(db):
    prepared = _multi_quiz_preparation()
    student, _teacher, assessment, root, questions = await _start_multi_quiz(db, prepared)
    original_versions = await _make_legacy_versions(db, questions)
    workspace_state = []
    for question in questions:
        attempt = await db.get(Attempt, question.attempt_id)
        workspace, files = await _workspace_and_files(db, attempt)
        await replace_file_content(
            db, attempt_id=attempt.id, principal_id=student.id, file_id=files[0].id,
            content=f"int main() {{ return {question.position}; }}",
            expected_revision=0, client_request_id=f"before-media-fix-{question.position}",
        )
        await db.refresh(workspace)
        await db.refresh(files[0])
        workspace_state.append((workspace.id, workspace.current_revision, workspace.current_hash,
                                files[0].id, files[0].content, files[0].content_hash))
    snapshots = list((await db.execute(select(Snapshot.id, Snapshot.manifest_hash))).all())
    edit_events = list((await db.execute(select(EditEvent.id, EditEvent.event_hash))).all())
    before_count = await db.scalar(select(func.count(TaskVersion.id)))
    enriched_questions = tuple(replace(question, question_text=f"{question.question_text} {IMAGE}")
                               for question in prepared.questions)
    enriched = replace(prepared, question_text=enriched_questions[0].question_text,
                       questions=enriched_questions)
    resumed = await start_attempt(
        db, assessment_id=assessment.id, principal_id=student.id, prepared_moodle_quiz=enriched,
    )
    assert resumed.id == root.id
    assert await db.scalar(select(func.count(TaskVersion.id))) == before_count + 2
    for index, question in enumerate(questions):
        attempt = await db.get(Attempt, question.attempt_id)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        assert version.statement == enriched_questions[index].question_text
        assert version.ai_policy["statement_format"] == STATEMENT_FORMAT
        assert version.id != original_versions[index][0]
        old = await db.get(TaskVersion, original_versions[index][0])
        assert (old.id, old.statement, old.content_hash) == original_versions[index]
        workspace = await db.get(Workspace, workspace_state[index][0])
        source = await db.get(WorkspaceFile, workspace_state[index][3])
        assert (workspace.id, workspace.current_revision, workspace.current_hash,
                source.id, source.content, source.content_hash) == workspace_state[index]
    assert list((await db.execute(select(Snapshot.id, Snapshot.manifest_hash))).all()) == snapshots
    assert list((await db.execute(select(EditEvent.id, EditEvent.event_hash))).all()) == edit_events
    await start_attempt(
        db, assessment_id=assessment.id, principal_id=student.id, prepared_moodle_quiz=enriched,
    )
    assert await db.scalar(select(func.count(TaskVersion.id))) == before_count + 2


async def test_new_format_task_does_not_accept_later_image_only_task_changes(db):
    student, _teacher, assessment, _root, _questions = await _start_multi_quiz(db)
    prepared = _multi_quiz_preparation()
    enriched_questions = tuple(replace(question, question_text=f"{question.question_text} {IMAGE}")
                               for question in prepared.questions)
    enriched = replace(prepared, question_text=enriched_questions[0].question_text,
                       questions=enriched_questions)
    with pytest.raises(DomainError) as caught:
        await start_attempt(
            db, assessment_id=assessment.id, principal_id=student.id,
            prepared_moodle_quiz=enriched,
        )
    assert caught.value.code == "MOODLE_ATTEMPT_BINDING_CONFLICT"


async def test_legacy_resume_still_rejects_changed_question_words(db):
    student, _teacher, assessment, _root, questions = await _start_multi_quiz(db)
    await _make_legacy_versions(db, questions)
    with pytest.raises(DomainError) as caught:
        await start_attempt(
            db, assessment_id=assessment.id, principal_id=student.id,
            prepared_moodle_quiz=_multi_quiz_preparation(
                second_statement=f"Другая задача. {IMAGE}",
            ),
        )
    assert caught.value.code == "MOODLE_ATTEMPT_BINDING_CONFLICT"
