from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from app.models.attempts import Attempt, EditEvent, Snapshot, Workspace, WorkspaceFile
from app.models.tasks import TaskVersion
from app.services.common import DomainError, sha256_text
from app.services.moodle_quiz_runtime import prepared_moodle_quiz_attempt
from app.services.moodle_statement_upgrade import STATEMENT_FORMAT, is_statement_math_upgrade
from app.services.workspace import replace_file_content, start_attempt
from tests.test_attempt_review_services import (
    _multi_quiz_preparation,
    _start_multi_quiz,
    _workspace_and_files,
)
from tests.test_statement_runtime_upgrade import _make_legacy_versions

FLAT = "∑ N k = 1 ( − 1 ) k k ( k + 1 )"
MATH = r"\[\sum_{k=1}^{N}{ \frac{(-1)^{k}}{k(k+1)} }\]"


def _old_preparation():
    prepared = _multi_quiz_preparation()
    questions = tuple(
        replace(question, question_text=f"{question.question_text} {FLAT}")
        for question in prepared.questions
    )
    return replace(prepared, question_text=questions[0].question_text, questions=questions)


def _rich_preparation(previous):
    questions = tuple(
        replace(
            question,
            question_text=question.question_text.replace(FLAT, MATH),
            question_text_legacy_hashes=(sha256_text(" ".join(question.question_text.split())),),
        )
        for question in previous.questions
    )
    return replace(
        previous,
        question_text=questions[0].question_text,
        questions=questions,
        question_text_legacy_hashes=questions[0].question_text_legacy_hashes,
    )


@pytest.mark.parametrize("previous_format", [None, "MOODLE_TEXT_IMAGES_V1"])
async def test_math_repair_keeps_every_question_workspace_and_code(db, previous_format):
    old = _old_preparation()
    student, _, assessment, root, bindings = await _start_multi_quiz(db, old)
    await _make_legacy_versions(db, bindings)
    retained = []
    for binding in bindings:
        attempt = await db.get(Attempt, binding.attempt_id)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        version.ai_policy = {**version.ai_policy, "statement_format": previous_format}
        workspace, files = await _workspace_and_files(db, attempt)
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=files[0].id,
            content=f"// keep task {binding.position}\nint main() {{return 0;}}",
            expected_revision=0,
            client_request_id=f"before-math-{binding.position}",
        )
        retained.append(
            (
                version.id,
                version.statement,
                workspace.id,
                files[0].id,
                files[0].content,
                workspace.current_revision,
                workspace.current_hash,
            )
        )
    snapshots = list((await db.execute(select(Snapshot.id, Snapshot.manifest_hash))).all())
    events = list((await db.execute(select(EditEvent.id, EditEvent.event_hash))).all())
    rich = _rich_preparation(old)
    for _ in range(2):
        resumed = await start_attempt(
            db, assessment_id=assessment.id, principal_id=student.id, prepared_moodle_quiz=rich
        )
        assert resumed.id == root.id
    for binding, before in zip(bindings, retained, strict=True):
        attempt = await db.get(Attempt, binding.attempt_id)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        assert version.statement == rich.questions[binding.position - 1].question_text
        assert version.ai_policy["statement_format"] == STATEMENT_FORMAT
        previous = await db.get(TaskVersion, before[0])
        assert previous.statement == before[1]
        workspace = await db.get(Workspace, before[2])
        file = await db.get(WorkspaceFile, before[3])
        assert (file.content, workspace.current_revision, workspace.current_hash) == before[4:]
    assert list((await db.execute(select(Snapshot.id, Snapshot.manifest_hash))).all()) == snapshots
    assert list((await db.execute(select(EditEvent.id, EditEvent.event_hash))).all()) == events
    assert await db.scalar(select(func.count(TaskVersion.id))) == 6  # No versions on second resume.


@pytest.mark.parametrize("proof", [(), ("0" * 64,)])
async def test_math_resume_without_exact_old_representation_proof_fails_closed(db, proof):
    old = _old_preparation()
    student, _, assessment, _, bindings = await _start_multi_quiz(db, old)
    await _make_legacy_versions(db, bindings)
    rich = _rich_preparation(old)
    questions = tuple(
        replace(question, question_text_legacy_hashes=proof) for question in rich.questions
    )
    rich = replace(rich, questions=questions, question_text_legacy_hashes=proof)
    with pytest.raises(DomainError, match="Moodle returned different data"):
        await start_attempt(
            db, assessment_id=assessment.id, principal_id=student.id, prepared_moodle_quiz=rich
        )


async def test_already_fixed_math_cannot_be_replaced_by_a_later_formula(db):
    old = _old_preparation()
    student, _, assessment, _, _ = await _start_multi_quiz(db, old)
    with pytest.raises(DomainError, match="Moodle returned different data"):
        await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
            prepared_moodle_quiz=_rich_preparation(old),
        )


@pytest.mark.parametrize("proof", ["wrong", ["x" * 64], ["0" * 64] * 3, [None]])
def test_invalid_connector_proof_is_rejected(proof):
    with pytest.raises(DomainError, match="repair proof"):
        prepared_moodle_quiz_attempt(
            course_external_id="549",
            cmid=777,
            external_attempt_id="144784",
            question_slot="2",
            question_text=MATH,
            question_text_legacy_hashes=proof,
            answer_transport="ESSAY_ATTACHMENT",
            available_answer_transports=["ESSAY_ATTACHMENT"],
        )


def test_hash_does_not_allow_other_changed_question_words_or_a_nonmath_replacement():
    assert is_statement_math_upgrade(
        f"Before {FLAT}", f"Before {MATH}", (sha256_text(f"Before {FLAT}"),)
    )
    assert not is_statement_math_upgrade(
        f"Before {FLAT}", f"Different {MATH}", (sha256_text(f"Different {FLAT}"),)
    )
    assert not is_statement_math_upgrade("Before", "Other", (sha256_text("Before"),))
