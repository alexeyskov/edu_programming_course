from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.db.base import utcnow
from app.models.attempts import Attempt, Workspace, WorkspaceFile
from app.schemas.attempts import WorkspaceFilePatchRequest
from app.services.common import DomainError
from app.services.workspace import create_clipboard_receipt, replace_file_content, start_attempt
from tests.test_attempt_review_services import _seed_course, _start_multi_quiz, _workspace_and_files


async def prepare(db, original="target", copied="copied"):
    student, teacher, _, assessment = await _seed_course(db)
    attempt = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    files = list(
        await db.scalars(select(WorkspaceFile).where(WorkspaceFile.workspace_id == workspace.id))
    )
    target, source = files
    for revision, (file, content) in enumerate(((target, original), (source, copied))):
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=file.id,
            content=content,
            expected_revision=revision,
            client_request_id=f"prepare-{revision}",
        )
    receipt = await create_clipboard_receipt(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        source_file_id=source.id,
        text=copied,
        revision=2,
    )
    return student, teacher, attempt, target, source, receipt


@pytest.mark.parametrize(
    "original, copied, offset, deleted",
    [
        ("abcd", "cdab", 2, 0),  # minimal diff would rotate the copied text
        ("abcXYZ", "abc123", 0, 6),  # minimal diff would trim the common prefix
        ("😀 начало", "код😀", 2, 6),
        ("line\nnext\n", "line\n", 5, 0),
        ("", "int main() {}\n", 0, 0),
    ],
)
async def test_exact_paste_range_preserves_verified_text(db, original, copied, offset, deleted):
    student, _, attempt, target, _, receipt = await prepare(db, original, copied)
    content = original[:offset] + copied + original[offset + deleted :]
    args = dict(
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=target.id,
        content=content,
        expected_revision=2,
        client_request_id="paste",
        source="INTERNAL_PASTE",
        receipt_id=receipt.id,
        paste_range={"offset": offset, "delete_count": deleted},
    )
    result = await replace_file_content(db, **args)
    assert result.file.content == content
    assert result.event.source == "INTERNAL_PASTE"
    delta = result.event.changes[0]
    assert delta["insert_text"] == copied
    assert delta["offset"] == offset
    assert delta["delete_count"] == deleted
    assert receipt.used_at is not None
    # Network retry is idempotent, but a different request cannot reuse a receipt.
    assert (await replace_file_content(db, **args)).event.id == result.event.id
    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            **{
                **args,
                "content": content + copied,
                "expected_revision": 3,
                "client_request_id": "reused",
                "paste_range": {"offset": len(content), "delete_count": 0},
            },
        )
    assert error.value.code == "INVALID_CLIPBOARD_RECEIPT"


async def test_verified_cut_can_be_pasted_after_source_is_deleted(db):
    student, _, attempt, target, source, receipt = await prepare(db)
    await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=source.id,
        content="",
        expected_revision=2,
        client_request_id="cut",
    )
    result = await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=target.id,
        content="targetcopied",
        expected_revision=3,
        client_request_id="paste-cut",
        source="INTERNAL_PASTE",
        receipt_id=receipt.id,
        paste_range={"offset": 6, "delete_count": 0},
    )
    assert result.file.content == "targetcopied"
    assert result.event.source == "INTERNAL_PASTE"


@pytest.mark.parametrize("copied, inserted", [("code\r\n", "code\n"), ("code\n", "code\r\n")])
async def test_paste_between_files_with_different_line_endings(db, copied, inserted):
    student, _, attempt, target, _, receipt = await prepare(db, "target", copied)
    result = await replace_file_content(
        db,
        attempt_id=attempt.id,
        principal_id=student.id,
        file_id=target.id,
        content="target" + inserted,
        expected_revision=2,
        client_request_id="eol-paste",
        source="INTERNAL_PASTE",
        receipt_id=receipt.id,
        paste_range={"offset": 6, "delete_count": 0},
    )
    assert result.event.changes[0]["insert_text"] == inserted
    assert result.file.content == "target" + inserted


@pytest.mark.parametrize("reason", ["missing", "expired", "principal", "text"])
async def test_invalid_receipts_cannot_authorize_paste(db, reason):
    student, teacher, attempt, target, _, receipt = await prepare(db)
    receipt_id = uuid.uuid4() if reason == "missing" else receipt.id
    if reason == "expired":
        receipt.expires_at = utcnow() - timedelta(seconds=1)
    if reason == "principal":
        receipt.principal_id = teacher.id
    await db.flush()
    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=target.id,
            content="target" + ("outside" if reason == "text" else "copied"),
            expected_revision=2,
            client_request_id="invalid-paste",
            source="INTERNAL_PASTE",
            receipt_id=receipt_id,
            paste_range={"offset": 6, "delete_count": 0},
        )
    assert error.value.code == "INVALID_CLIPBOARD_RECEIPT"
    assert target.content == "target"


@pytest.mark.parametrize(
    "paste_range, content",
    [
        ({"offset": -1, "delete_count": 0}, "targetcopied"),
        ({"offset": 7, "delete_count": 0}, "targetcopied"),
        ({"offset": 0, "delete_count": 7}, "copied"),
        ({"offset": 6, "delete_count": 0}, "changedcopied"),
        ({"offset": 0, "delete_count": 0}, "copiedchanged"),
    ],
)
async def test_paste_range_cannot_hide_other_edits(db, paste_range, content):
    student, _, attempt, target, _, receipt = await prepare(db)
    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=target.id,
            content=content,
            expected_revision=2,
            client_request_id="bad-range",
            source="INTERNAL_PASTE",
            receipt_id=receipt.id,
            paste_range=paste_range,
        )
    assert error.value.code == "INVALID_PASTE_RANGE"
    assert target.content == "target"
    assert receipt.used_at is None


async def test_receipt_requires_actual_text_in_the_current_source(db):
    student, _, attempt, _, source, _ = await prepare(db)
    with pytest.raises(DomainError) as error:
        await create_clipboard_receipt(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            source_file_id=source.id,
            text="outside",
            revision=2,
        )
    assert error.value.code == "CLIPBOARD_TEXT_NOT_IN_WORKSPACE"


def test_paste_range_is_optional_for_legacy_clients_but_forbidden_for_typing():
    assert (
        WorkspaceFilePatchRequest(
            content="x", source="INTERNAL_PASTE", receipt_id=uuid.uuid4()
        ).paste_range
        is None
    )
    with pytest.raises(ValidationError):
        WorkspaceFilePatchRequest(content="x", paste_range={"offset": 0, "delete_count": 0})


async def prepare_quiz_copy(db, source_index=0):
    student, teacher, assessment, root, questions = await _start_multi_quiz(db)
    source = await db.get(Attempt, questions[source_index].attempt_id)
    target = await db.get(Attempt, questions[1 - source_index].attempt_id)
    _, source_files = await _workspace_and_files(db, source)
    _, target_files = await _workspace_and_files(db, target)
    text = "int helper() { return 42; }\n"
    for revision in range(3):
        await replace_file_content(
            db,
            attempt_id=source.id,
            principal_id=student.id,
            file_id=source_files[0].id,
            content=f"// edit {revision}\n" + text,
            expected_revision=revision,
            client_request_id=f"prepare-quiz-{revision}",
        )
    receipt = await create_clipboard_receipt(
        db,
        attempt_id=source.id,
        principal_id=student.id,
        source_file_id=source_files[0].id,
        text=text,
        revision=3,
    )
    args = dict(
        attempt_id=target.id,
        principal_id=student.id,
        file_id=target_files[0].id,
        content=text,
        expected_revision=0,
        client_request_id="cross-question-paste",
        source="INTERNAL_PASTE",
        receipt_id=receipt.id,
        paste_range={"offset": 0, "delete_count": 0},
    )
    return student, teacher, root, questions, source, target, source_files[0], receipt, args


@pytest.mark.parametrize("source_index", [0, 1])
@pytest.mark.parametrize("cut", [False, True])
async def test_copy_between_quiz_questions_uses_the_source_revision(db, source_index, cut):
    student, _, _, _, source, target, source_file, receipt, args = await prepare_quiz_copy(
        db, source_index
    )
    if cut:
        await replace_file_content(
            db,
            attempt_id=source.id,
            principal_id=student.id,
            file_id=source_file.id,
            content="",
            expected_revision=3,
            client_request_id="cut-quiz-source",
        )
    result = await replace_file_content(db, **args)
    assert result.file.content == args["content"]
    assert target.current_revision == 1  # receipt revision 3 belongs to the other workspace
    assert result.event.source == "INTERNAL_PASTE"
    assert result.event.changes[0]["clipboard_source"] == {
        "attempt_id": str(source.id),
        "file_id": str(source_file.id),
        "revision": 3,
        "receipt_id": str(receipt.id),
    }
    assert receipt.used_at is not None
    assert (await replace_file_content(db, **args)).event.id == result.event.id
    with pytest.raises(DomainError) as error:
        await replace_file_content(
            db,
            **{
                **args,
                "content": args["content"] * 2,
                "expected_revision": 1,
                "client_request_id": "reused-cross-question-receipt",
                "paste_range": {"offset": len(args["content"]), "delete_count": 0},
            },
        )
    assert error.value.code == "INVALID_CLIPBOARD_RECEIPT"


@pytest.mark.parametrize(
    "reason",
    [
        "other_quiz",
        "unmapped",
        "source_owner",
        "receipt_owner",
        "future_revision",
        "expired",
        "used",
        "wrong_text",
    ],
)
async def test_cross_question_paste_does_not_weaken_receipt_boundaries(db, reason):
    student, teacher, _, questions, source, target, _, receipt, args = await prepare_quiz_copy(
        db, source_index=1 if reason == "source_owner" else 0
    )
    if reason == "other_quiz":
        questions[1].root_attempt_id = target.id
    elif reason == "unmapped":
        await db.delete(questions[1])  # integrity_policy still claims the original root
    elif reason == "source_owner":
        source.principal_id = teacher.id
    elif reason == "receipt_owner":
        receipt.principal_id = teacher.id
    elif reason == "future_revision":
        receipt.revision = 4
    elif reason == "expired":
        receipt.expires_at = utcnow() - timedelta(seconds=1)
    elif reason == "used":
        receipt.used_at = utcnow()
    else:
        args["content"] = "external code"
    await db.flush()
    with pytest.raises(DomainError) as error:
        await replace_file_content(db, **args)
    assert error.value.code == "INVALID_CLIPBOARD_RECEIPT"
    assert target.current_revision == 0


async def test_shared_receipt_cannot_edit_a_submitted_question(db):
    _, _, _, _, _, target, _, _, args = await prepare_quiz_copy(db)
    target.state = "SUBMITTED"
    await db.flush()
    with pytest.raises(DomainError) as error:
        await replace_file_content(db, **args)
    assert error.value.code == "ATTEMPT_READ_ONLY"
