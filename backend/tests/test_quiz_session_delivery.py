from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from app.models.attempts import Attempt, Snapshot, Submission
from app.models.courses import Course
from app.models.integration import LMSSubmissionFingerprint, SyncOutbox
from app.services import sync
from app.services.common import DomainError
from app.services.moodle_attempt_selection import latest_completed_moodle_submission_ids
from app.services.review import response_max_score
from app.services.workspace import (
    create_snapshot,
    enqueue_checkpoint,
    replace_file_content,
    submit_attempt,
)
from tests.test_attempt_review_services import (
    _multi_quiz_preparation,
    _start_multi_quiz,
    _workspace_and_files,
)


def _claim(row):
    return sync.ClaimedOutboxEvent(
        id=row.id,
        event_type=row.event_type,
        aggregate_type=row.aggregate_type,
        aggregate_id=row.aggregate_id,
        connection_id=row.connection_id,
        course_id=row.course_id,
        attempt_id=row.attempt_id,
        idempotency_key=row.idempotency_key,
        payload=dict(row.payload),
        attempts=1,
        locked_at=datetime.now(UTC),
    )


async def _delivery(db, app_bundle, monkeypatch, *, contents=None, all_attachments=False):
    prepared = _multi_quiz_preparation()
    if all_attachments:
        prepared = replace(prepared, questions=tuple(
            replace(q, answer_transport="ESSAY_ATTACHMENT",
                    available_answer_transports=("ESSAY_ATTACHMENT",))
            for q in prepared.questions
        ))
    student, teacher, assessment, root, questions = await _start_multi_quiz(db, prepared=prepared)
    for index, question in enumerate(questions, start=1):
        member = await db.get(Attempt, question.attempt_id)
        _, files = await _workspace_and_files(db, member)
        await replace_file_content(
            db,
            attempt_id=member.id,
            principal_id=student.id,
            file_id=files[0].id,
            content=contents[index - 1] if contents else f"int main() {{ return {index}; }}\n",
            expected_revision=0,
            client_request_id=f"question-{index}-edit",
        )
    await submit_attempt(
        db,
        attempt_id=questions[-1].attempt_id,
        principal_id=student.id,
        expected_revision=1,
    )
    row = next(
        row
        for row in (await db.scalars(select(SyncOutbox))).all()
        if row.payload.get("reason") == "SUBMISSION"
    )
    course = await db.get(Course, assessment.course_id)
    target = sync.ConnectionTarget(
        id=course.connection_id,
        base_url="https://moodle.example.test",
        service_token=None,
        mode="PLUGINLESS",
        transport="PLAYWRIGHT",
    )

    async def credential_target(_db, _settings, connection, _principal_id):
        return connection

    monkeypatch.setattr(sync, "_principal_credential_target", credential_target)
    context = await sync._prepare_checkpoint(db, app_bundle[2], _claim(row), target)
    return student, teacher, assessment, root, questions, row, target, context


@pytest.mark.parametrize("contents", [("", ""), ("int main() {}", ""), ("", "second answer")])
async def test_quiz_bundle_can_submit_one_or_both_empty_attachment_answers(
    db, app_bundle, monkeypatch, contents,
):
    *_, context = await _delivery(
        db, app_bundle, monkeypatch, contents=contents, all_attachments=True,
    )
    assert context.payload["finalize"] is True
    assert len(context.payload["answers"]) == 2
    for answer, source in zip(context.payload["answers"], contents, strict=True):
        assert answer["artifact_size"] > 0
        if source:
            assert answer["artifact_filename"] == "main.cpp"
            assert answer["artifact_bytes"] == source.encode()
        else:
            assert answer["artifact_filename"] == "submission.zip"
            with ZipFile(BytesIO(answer["artifact_bytes"])) as archive:
                assert archive.namelist() == ["main.cpp"]
                assert archive.read("main.cpp") == b""


async def test_bundle_delivery_keeps_answers_and_attestations_separate(db, app_bundle, monkeypatch):
    _, _, _, root, questions, row, _, context = await _delivery(db, app_bundle, monkeypatch)
    answers = context.payload["answers"]
    assert context.payload["finalize"] is True
    assert [answer["question_slot"] for answer in answers] == ["1", "3"]
    assert answers[0]["artifact_bytes"] != answers[1]["artifact_bytes"]
    assert b"return 1" in answers[0]["artifact_bytes"]
    assert b"return 2" in answers[1]["artifact_bytes"]
    receipts = [
        {
            "course_id": "549",
            "cmid": 30354,
            "attempt_id": "141716",
            "question_slot": answer["question_slot"],
            "filename": answer["artifact_filename"],
            "sha256": answer["artifact_sha256"],
            "size_bytes": answer["artifact_size"],
            "idempotency_key": row.idempotency_key,
        }
        for answer in answers
    ]
    result = {"status": "FINALIZED", "receipts": receipts}
    await sync._record_lms_submission_fingerprint(db, row=row, context=context, result=result)
    await db.flush()
    await sync._record_lms_submission_fingerprint(db, row=row, context=context, result=result)
    await db.flush()
    fingerprints = list((await db.scalars(select(LMSSubmissionFingerprint))).all())
    assert len(fingerprints) == 2
    assert {item.outbox_id for item in fingerprints} == {row.id}
    assert {item.attempt_id for item in fingerprints} == {q.attempt_id for q in questions}
    assert {item.external_question_slot for item in fingerprints} == {"1", "3"}
    assert all(item.terminal and item.submission_id for item in fingerprints)
    assert root.state == "SUBMITTED"


@pytest.mark.parametrize("corruption", ["missing", "foreign_snapshot", "slot", "revision"])
async def test_bundle_delivery_rejects_incomplete_or_crossed_references(
    db,
    app_bundle,
    monkeypatch,
    corruption,
):
    _, _, _, _, _, row, target, _ = await _delivery(db, app_bundle, monkeypatch)
    claim = _claim(row)
    payload = dict(claim.payload)
    values = [dict(value) for value in payload["quiz_questions"]]
    payload["quiz_questions"] = values
    if corruption == "missing":
        values.pop()
    elif corruption == "foreign_snapshot":
        values[1]["snapshot_id"] = values[0]["snapshot_id"]
    elif corruption == "slot":
        values[1]["question_slot"] = "1"
    else:
        payload["quiz_session_revision"] += 1
    with pytest.raises(sync._BlockedDelivery):
        await sync._prepare_checkpoint(db, app_bundle[2], replace(claim, payload=payload), target)


async def test_native_quiz_review_is_one_group_with_per_question_scales(
    db, app_bundle, monkeypatch
):
    from app.api.reviews import _review_queue_group_key, _submission_review_group

    _, _, assessment, root, questions, _, _, _ = await _delivery(db, app_bundle, monkeypatch)
    submissions = list((await db.scalars(select(Submission))).all())
    keys = []
    rows = []
    from app.models.tasks import Assessment

    for question in questions:
        member = await db.get(Attempt, question.attempt_id)
        work = await db.get(Assessment, member.assessment_id)
        submission = next(item for item in submissions if item.attempt_id == member.id)
        keys.append(_review_queue_group_key(submission, member, work))
        rows.append((submission, member, work))
        assert await response_max_score(db, member, work) == question.question_max_mark
    assert keys[0] is not None and keys[0] == keys[1]
    root_submission = next(item for item in submissions if item.attempt_id == root.id)
    group = await _submission_review_group(
        db, submission=root_submission, attempt=root, assessment=assessment
    )
    assert group is not None and len(group.items) == 2
    assert [item.max_score for item in group.items] == [3, 5]
    assert latest_completed_moodle_submission_ids(rows) == {item.id for item in submissions}
    # Importing one response must not hide the still-native second response.
    imported = Submission(
        id=uuid.uuid4(),
        source="MOODLE_IMPORT",
        submitted_at=root_submission.submitted_at,
        external_receipt=dict(rows[0][0].external_receipt),
    )
    selected = latest_completed_moodle_submission_ids([*rows, (imported, rows[0][1], rows[0][2])])
    assert selected == {imported.id, rows[1][0].id}


async def test_external_completion_locks_every_quiz_solution(db):
    student, _, _, root, questions = await _start_multi_quiz(db)
    await sync._mark_moodle_attempt_finalized(db, attempt=root, finalized_at=datetime.now(UTC))
    for question in questions:
        member = await db.get(Attempt, question.attempt_id)
        _, files = await _workspace_and_files(db, member)
        assert member.state == "LOCKED"
        with pytest.raises(DomainError) as caught:
            await replace_file_content(
                db,
                attempt_id=member.id,
                principal_id=student.id,
                file_id=files[0].id,
                content="changed",
                expected_revision=0,
                client_request_id=f"closed-{question.position}",
            )
        assert caught.value.code == "LMS_ATTEMPT_FINALIZED"


async def test_scheduler_submits_the_shared_quiz_once(db, app_bundle):
    _, _, _, root, questions = await _start_multi_quiz(db)
    deadline = datetime.now(UTC) - timedelta(seconds=1)
    for question in questions:
        member = await db.get(Attempt, question.attempt_id)
        member.deadline_at = deadline
    await db.flush()
    result = await sync.maintain_attempts(db, app_bundle[2], now=datetime.now(UTC))
    assert result.attempts_submitted == 1
    assert result.checkpoints_enqueued == 1
    assert root.state == "AUTO_SUBMITTED"
    terminal = [
        row
        for row in (await db.scalars(select(SyncOutbox))).all()
        if row.payload.get("reason") == "DEADLINE"
    ]
    assert len(terminal) == 1 and len(terminal[0].payload["quiz_questions"]) == 2


async def test_quiz_start_keeps_local_snapshot_without_blocking_quick_submission(db):
    student, _, _, root, _questions = await _start_multi_quiz(db)
    workspace, _ = await _workspace_and_files(db, root)
    assert await db.scalar(select(Snapshot).where(Snapshot.workspace_id == workspace.id))
    assert not (await db.scalars(select(SyncOutbox))).all()
    await submit_attempt(db, attempt_id=root.id, principal_id=student.id, expected_revision=0)
    events = (await db.scalars(select(SyncOutbox))).all()
    assert len(events) == 1
    assert events[0].payload["reason"] == "SUBMISSION"
    assert len(events[0].payload["quiz_questions"]) == 2


async def test_first_periodic_quiz_checkpoint_waits_interval_and_captures_latest_code(
    db, app_bundle,
):
    student, _, assessment, root, questions = await _start_multi_quiz(db)
    started = sync._as_utc(root.started_at)
    early = await sync.maintain_attempts(db, app_bundle[2], now=started + timedelta(seconds=1))
    assert early.checkpoints_enqueued == 0
    child = await db.get(Attempt, questions[1].attempt_id)
    _, files = await _workspace_and_files(db, child)
    await replace_file_content(
        db, attempt_id=child.id, principal_id=student.id, file_id=files[0].id,
        content="// Edited second solution\n", expected_revision=0,
        client_request_id="first-periodic-latest-code",
    )
    interval = sync.checkpoint_interval_seconds(root, assessment, now=started)
    result = await sync.maintain_attempts(
        db, app_bundle[2], now=started + timedelta(seconds=interval)
    )
    assert result.checkpoints_enqueued == 1
    events = (await db.scalars(select(SyncOutbox))).all()
    assert len(events) == 1 and events[0].payload["reason"] == "PERIODIC"
    assert events[0].payload["quiz_session_revision"] == 1
    entry = next(
        item for item in events[0].payload["quiz_questions"]
        if item["attempt_id"] == str(child.id)
    )
    snapshot = await db.get(Snapshot, uuid.UUID(entry["snapshot_id"]))
    assert snapshot.files[0]["content"] == "// Edited second solution\n"


async def test_editing_only_second_solution_supersedes_old_bundle(db, app_bundle, monkeypatch):
    student, _, assessment, root, questions = await _start_multi_quiz(db)
    root_workspace, _ = await _workspace_and_files(db, root)
    initial = await create_snapshot(db, root_workspace, "PERIODIC")
    old = await enqueue_checkpoint(db, attempt=root, snapshot=initial, reason="PERIODIC")
    child = await db.get(Attempt, questions[1].attempt_id)
    workspace, files = await _workspace_and_files(db, child)
    await replace_file_content(
        db,
        attempt_id=child.id,
        principal_id=student.id,
        file_id=files[0].id,
        content="int main() { return 42; }\n",
        expected_revision=0,
        client_request_id="second-only",
    )
    updated = await create_snapshot(db, workspace, "PERIODIC")
    new = await enqueue_checkpoint(db, attempt=child, snapshot=updated, reason="PERIODIC")
    assert new.id != old.id
    assert new.aggregate_id == old.aggregate_id
    assert new.attempt_id == root.id
    assert new.payload["quiz_session_revision"] == old.payload["quiz_session_revision"] + 1
    course = await db.get(Course, assessment.course_id)
    target = sync.ConnectionTarget(
        id=course.connection_id,
        base_url="https://moodle.example.test",
        service_token=None,
        mode="PLUGINLESS",
        transport="PLAYWRIGHT",
    )

    async def credential_target(_db, _settings, connection, _principal_id):
        return connection

    monkeypatch.setattr(sync, "_principal_credential_target", credential_target)
    with pytest.raises(sync._BlockedDelivery) as caught:
        await sync._prepare_checkpoint(db, app_bundle[2], _claim(old), target)
    assert caught.value.code == "SUPERSEDED"
    context = await sync._prepare_checkpoint(db, app_bundle[2], _claim(new), target)
    assert b"return 42" in context.payload["answers"][1]["artifact_bytes"]


async def test_each_native_question_grade_targets_its_exact_moodle_slot(
    db, app_bundle, monkeypatch
):
    from decimal import Decimal

    from app.models.integration import ExternalMapping
    from app.models.review import ReviewDecision

    student, teacher, assessment, root, questions, row, target, context = await _delivery(
        db, app_bundle, monkeypatch
    )
    student.external_subject = "123"
    mapping = await db.scalar(
        select(ExternalMapping).where(ExternalMapping.local_id == assessment.id)
    )
    mapping.metadata_json = {
        **mapping.metadata_json,
        "activity": {
            **mapping.metadata_json["activity"],
            "grade_confirmed": True,
            "grade_max": 8,
            "quiz_grading_method_confirmed": True,
            "quiz_grading_method": "HIGHEST",
        },
    }
    receipts = [
        {"attempt_id": "141716", "question_slot": answer["question_slot"]}
        for answer in context.payload["answers"]
    ]
    await sync._record_lms_submission_fingerprint(
        db,
        row=row,
        context=context,
        result={"receipts": receipts},
    )
    await db.flush()
    for question in questions:
        submission = await db.scalar(
            select(Submission).where(Submission.attempt_id == question.attempt_id)
        )
        decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=teacher.id,
            revision=1,
            grade=Decimal("1.00"),
            status="APPLIED",
        )
        db.add(decision)
        await db.flush()
        claim = replace(
            _claim(row),
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
        )
        grade = await sync._prepare_grade(db, app_bundle[2], claim, target)
        assert grade.payload["attempt_id"] == 141716
        assert str(grade.payload["question_slot"]) == question.question_slot
        assert Decimal(grade.payload["grade_scale_max"]) == question.question_max_mark
        assert Decimal(grade.payload["quiz_overall_grade_max"]) == 8
