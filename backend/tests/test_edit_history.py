from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import select

from app.api.attempts import get_attempt_history
from app.api.reviews import _snapshot_history
from app.models.attempts import EditEvent, Snapshot, WorkspaceFile
from app.services.common import contiguous_delta, sha256_text
from app.services.edit_history import describe_delta, edit_details
from app.services.workspace import (
    create_snapshot,
    create_workspace_file,
    delete_workspace_file,
    replace_file_content,
)
from tests.test_api_integrity_ai_system import _context
from tests.test_clipboard_paste import prepare

FILE_ID = uuid.uuid4()


def _snapshot(content, revision=0):
    return Snapshot(revision=revision, files=[{"id": str(FILE_ID), "content": content}])


def _edit(before, after, sequence=1, *, detailed=False):
    change = {
        **contiguous_delta(before, after),
        "previous_content_hash": sha256_text(before), "content_hash": sha256_text(after),
    }
    if detailed:
        change.update(describe_delta(before, change))
    return EditEvent(
        id=uuid.uuid4(), sequence=sequence, file_id=FILE_ID,
        event_type="REPLACE_CONTENT", changes=[change], event_hash="original-hash",
    )


@pytest.mark.parametrize("before,after,expected", [
    ("a\ncode", "a\nXcode", ("insert", 2, 1, 2, 1, "", "X")),
    ("a\ncode", "a\node", ("delete", 2, 1, 2, 2, "c", "")),
    ("aa\nbb\ncc", "aa\nXcc", ("replace", 2, 1, 3, 1, "bb\n", "X")),
    ("a😀b", "a😀z", ("replace", 1, 4, 1, 5, "b", "z")),
    ("а\r\n\tb", "а\r\n\tЖ", ("replace", 2, 2, 2, 3, "b", "Ж")),
    ("", "new", ("insert", 1, 1, 1, 1, "", "new")),
    ("a\n", "", ("delete", 1, 1, 2, 1, "a\n", "")),
    ("same", "same", ("unchanged", 1, 5, 1, 5, "", "")),
])
@pytest.mark.parametrize("detailed", [False, True])
def test_exact_positions_and_text_for_new_and_reconstructable_legacy_events(
    before, after, expected, detailed,
):
    event = _edit(before, after, detailed=detailed)
    original = copy.deepcopy(event.changes)
    result = edit_details([event], [] if detailed else [_snapshot(before)])[event.id][0]
    assert tuple(result[key] for key in (
        "operation", "start_line", "start_column", "end_line", "end_column",
        "deleted_text", "insert_text",
    )) == expected
    assert event.changes == original
    assert event.event_hash == "original-hash"


def test_legacy_replay_tracks_each_revision_instead_of_using_final_file_content():
    first = _edit("int x;\n", "int xy;\n")
    second = _edit("int xy;\n", "int z;\n", 2)
    third = _edit("int z;\n", "int z;\nreturn;", 3)
    result = edit_details([third, second, first], [_snapshot("int x;\n")])
    assert result[second.id][0]["deleted_text"] == "xy"
    assert result[second.id][0]["start_column"] == 5
    assert result[third.id][0]["start_line"] == 2


@pytest.mark.parametrize("problem", ["missing", "wrong-before", "wrong-after", "gap"])
def test_incomplete_legacy_history_does_not_invent_deleted_text_or_positions(problem):
    event = _edit("abc", "aXc", 2)
    anchors = [_snapshot("abc")]
    if problem == "missing":
        anchors = []
    if problem in {"wrong-before", "gap"}:
        anchors = [_snapshot("old")]
    if problem == "wrong-after":
        event.changes[0]["content_hash"] = "0" * 64
    detail = edit_details([event], anchors)[event.id][0]
    assert detail["deleted_text"] is None
    assert "start_line" not in detail
    assert detail["offset"] == 1
    assert detail["insert_text"] == "X"


def test_a_later_snapshot_recovers_history_after_a_gap_without_crossing_files():
    missing = _edit("abc", "abcd", 2)
    recovered = _edit("abcd", "abcX", 4)
    other = _edit("abcd", "other", 5)
    other.file_id = uuid.uuid4()
    result = edit_details([missing, recovered, other], [_snapshot("abcd", 3)])
    assert "start_line" not in result[missing.id][0]
    assert result[recovered.id][0]["deleted_text"] == "d"
    assert result[other.id][0]["deleted_text"] is None


def test_new_file_creation_and_deletion_include_exact_content():
    created = EditEvent(id=uuid.uuid4(), sequence=1, file_id=FILE_ID, changes=[{
        "type": "file_create", "content": "#pragma once\n",
        "content_hash": sha256_text("#pragma once\n"),
    }])
    deleted = EditEvent(id=uuid.uuid4(), sequence=2, file_id=FILE_ID, changes=[{
        "type": "file_delete", "content_hash": sha256_text("#pragma once\n"),
    }])
    result = edit_details([created, deleted], [])
    assert result[created.id][0]["insert_text"] == "#pragma once\n"
    assert result[deleted.id][0]["deleted_text"] == "#pragma once\n"


async def test_saved_edit_log_and_both_history_apis_preserve_text_positions_and_cutoff(db):
    student, _, attempt, target, _, _ = await prepare(db, "a\n😀 old", "source")
    mutation = await replace_file_content(
        db, attempt_id=attempt.id, principal_id=student.id, file_id=target.id,
        content="a\n😀 new", expected_revision=2, client_request_id="history-detail",
    )
    raw = mutation.event.changes[0]
    assert raw["deleted_text"] == "old"
    assert (raw["start_line"], raw["start_column"]) == (2, 4)
    assert raw["insert_text"] == "new"
    frozen = await create_snapshot(db, workspace=mutation.workspace, reason="PERIODIC")
    teacher_rows = await _snapshot_history(db, submission=None, attempt=attempt, snapshot=frozen)
    row = next(item for item in teacher_rows if item.id == mutation.event.id)
    assert row.edit_details[0].deleted_text == "old"
    assert row.edit_details[0].start_column == 4
    raw_before_read = copy.deepcopy(mutation.event.changes)
    student_rows = await get_attempt_history(attempt.id, _context(student.id), db, limit=500)
    assert next(item for item in student_rows if item.id == mutation.event.id).edit_details == (
        row.edit_details
    )
    assert mutation.event.changes == raw_before_read
    later = await replace_file_content(
        db, attempt_id=attempt.id, principal_id=student.id, file_id=target.id,
        content="later code", expected_revision=3, client_request_id="later-change",
    )
    assert later.event.id not in {item.id for item in await _snapshot_history(
        db, submission=None, attempt=attempt, snapshot=frozen,
    )}


async def test_new_file_events_log_initial_and_deleted_contents(db):
    student, _, attempt, _, _, _ = await prepare(db)
    created = await create_workspace_file(
        db, attempt_id=attempt.id, principal_id=student.id, path="log.txt",
        content="first\nsecond", expected_revision=2, client_request_id="create-log",
    )
    assert created.event.changes[0]["content"] == "first\nsecond"
    deleted = await delete_workspace_file(
        db, attempt_id=attempt.id, principal_id=student.id, file_id=created.file.id,
        expected_revision=3, client_request_id="delete-log",
    )
    assert deleted.event.changes[0]["deleted_text"] == "first\nsecond"
    assert await db.scalar(select(WorkspaceFile).where(WorkspaceFile.id == created.file.id))
