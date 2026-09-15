from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select
from starlette.requests import Request

from app.api import attempts as attempts_api
from app.api.courses import _apply_activity_deadlines as foreground_sync_deadlines
from app.core.config import Settings
from app.models.attempts import Attempt, Snapshot, Submission
from app.models.courses import Course
from app.models.integration import SyncOutbox
from app.schemas.attempts import AttemptStartRequest
from app.services import sync
from app.services.common import DomainError
from app.services.moodle_quiz_runtime import apply_prepared_quiz_timer
from app.services.workspace import replace_file_content, start_attempt
from tests.test_attempt_review_services import (
    _map_deferred_random_quiz,
    _multi_quiz_preparation,
    _seed_course,
    _start_multi_quiz,
    _workspace_and_files,
)
from tests.test_student_work_continuation import auth


def test_reserve_defaults_to_five_minutes_and_reads_environment(monkeypatch):
    monkeypatch.delenv("MOODLE_SYNC_TIMEOUT", raising=False)
    assert Settings(_env_file=None).moodle_sync_timeout == 300
    monkeypatch.setenv("MOODLE_SYNC_TIMEOUT", "600")
    assert Settings(_env_file=None).moodle_sync_timeout == 600
    monkeypatch.setenv("MOODLE_SYNC_TIMEOUT", "0")
    assert Settings(_env_file=None).moodle_sync_timeout == 0


@pytest.mark.parametrize("value", ["-1", "86401", "abc", "0.5"])
def test_invalid_reserve_is_rejected(monkeypatch, value):
    monkeypatch.setenv("MOODLE_SYNC_TIMEOUT", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    ("remaining", "reserve", "editing_seconds"),
    [(900, 300, 600), (900, 600, 300), (900, 0, 900), (300, 300, 0), (60, 300, 0)],
)
async def test_timer_uses_live_remaining_time_not_course_duration(
    db,
    monkeypatch,
    remaining,
    reserve,
    editing_seconds,
):
    student, _, _, assessment = await _seed_course(db)
    await _map_deferred_random_quiz(db, assessment)
    assessment.duration_seconds = 1800  # Does not reflect this student's override.
    now = datetime.now(UTC)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: now)
    attempt = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=replace(
            _multi_quiz_preparation(),
            questions=(),
            remaining_seconds=remaining,
        ),
        moodle_sync_timeout=reserve,
    )
    assert attempt.expected_end_at == now + timedelta(seconds=remaining)
    assert attempt.deadline_at == now + timedelta(seconds=editing_seconds)
    assert attempt.integrity_policy["moodle_sync_timeout_seconds"] == reserve


async def test_api_passes_configured_reserve_and_reports_reduced_timer(db, app_bundle, monkeypatch):
    app, _, settings = app_bundle
    settings.moodle_sync_timeout = 600
    student, _, _, assessment = await _seed_course(db)
    await _map_deferred_random_quiz(db, assessment)
    monkeypatch.setattr(
        attempts_api,
        "_prepare_moodle_quiz_attempt",
        AsyncMock(
            return_value=replace(_multi_quiz_preparation(), questions=(), remaining_seconds=900)
        ),
    )
    monkeypatch.setattr(attempts_api, "_prepare_moodle_assignment", AsyncMock(return_value=None))
    request = Request({"type": "http", "app": app, "headers": [], "client": ("127.0.0.1", 123)})
    result = await attempts_api.create_attempt(
        assessment.id,
        AttemptStartRequest(),
        request,
        auth(student, "STUDENT"),
        db,
    )
    assert result.moodle_sync_timeout_seconds == 600
    assert result.has_time_limit is True
    assert result.expected_end_at - result.deadline_at == timedelta(seconds=600)


async def test_resume_preserves_reserve_without_deducting_twice_and_keeps_all_tasks(
    db, monkeypatch
):
    now = datetime.now(UTC)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: now)
    student, _, assessment, root, questions = await _start_multi_quiz(db)
    initial_deadline = root.deadline_at
    later = now + timedelta(seconds=60)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: later)
    resumed = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=replace(_multi_quiz_preparation(), remaining_seconds=1140),
        moodle_sync_timeout=600,  # A restart/configuration change is not retroactive.
    )
    assert resumed.id == root.id
    assert resumed.deadline_at == initial_deadline
    for binding in questions:
        member = await db.get(Attempt, binding.attempt_id)
        assert member.deadline_at == initial_deadline
        assert member.integrity_policy["moodle_sync_timeout_seconds"] == 300

    # A newly confirmed extension is measured from Moodle, not from a course date.
    await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=replace(_multi_quiz_preparation(), remaining_seconds=1740),
        moodle_sync_timeout=600,
    )
    assert root.deadline_at == initial_deadline + timedelta(seconds=600)


def test_missing_timer_does_not_erase_a_confirmed_reserve_or_create_one_for_untimed_work():
    now = datetime.now(UTC)
    attempt = SimpleNamespace(integrity_policy={}, expected_end_at=None, deadline_at=None)
    apply_prepared_quiz_timer(attempt, remaining_seconds=None, reserve_seconds=300, now=now)
    assert attempt.deadline_at is None and attempt.expected_end_at is None
    apply_prepared_quiz_timer(attempt, remaining_seconds=900, reserve_seconds=300, now=now)
    deadline = attempt.deadline_at
    apply_prepared_quiz_timer(attempt, remaining_seconds=None, reserve_seconds=600, now=now)
    assert attempt.deadline_at == deadline
    assert attempt.integrity_policy["moodle_sync_timeout_seconds"] == 300


@pytest.mark.parametrize(
    "apply_deadlines", [foreground_sync_deadlines, sync._apply_activity_deadlines]
)
async def test_course_sync_cannot_reset_the_submission_reserve(db, apply_deadlines):
    _, _, assessment, root, _ = await _start_multi_quiz(db)
    assessment.policy = {**assessment.policy, "moodle_metadata_read_only": True}
    initial_deadline = root.deadline_at
    course = await db.get(Course, assessment.course_id)
    await apply_deadlines(
        db,
        course,
        [
            {
                "module": "quiz",
                "cmid": 30354,
                "opens_at_epoch": 0,
                "due_at_epoch": 1_600_000_000,
                "cutoff_at_epoch": 0,
                "schedule_confirmed": True,
                "settings_confirmed": True,
            }
        ],
    )
    assert assessment.closes_at == datetime.fromtimestamp(1_600_000_000, UTC)
    assert root.deadline_at == initial_deadline


@pytest.mark.parametrize("contents", [("first answer", "second answer"), ("", "")])
async def test_deadline_without_browser_locks_and_queues_all_solutions_once(
    db,
    app_bundle,
    monkeypatch,
    contents,
):
    started = datetime.now(UTC)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: started)
    student, _, _, root, questions = await _start_multi_quiz(
        db,
        prepared=replace(_multi_quiz_preparation(), remaining_seconds=900),
    )
    for index, binding in enumerate(questions):
        member = await db.get(Attempt, binding.attempt_id)
        _, files = await _workspace_and_files(db, member)
        await replace_file_content(
            db,
            attempt_id=member.id,
            principal_id=student.id,
            file_id=files[0].id,
            content=contents[index],
            expected_revision=0,
            client_request_id=f"answer-{index}",
        )
    deadline = started + timedelta(seconds=600)
    assert root.deadline_at == deadline
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: deadline)
    for binding in questions:
        member = await db.get(Attempt, binding.attempt_id)
        _, files = await _workspace_and_files(db, member)
        with pytest.raises(DomainError) as error:
            await replace_file_content(
                db,
                attempt_id=member.id,
                principal_id=student.id,
                file_id=files[0].id,
                content="too late",
                expected_revision=1,
                client_request_id=f"late-{member.id}",
            )
        assert error.value.code == "DEADLINE_PASSED"
    # The worker can wake a few seconds after the editing boundary, while the
    # student is still safely within the reserved Moodle delivery window.
    worker_time = deadline + timedelta(seconds=2)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: worker_time)
    result = await sync.maintain_attempts(db, app_bundle[2], now=worker_time)
    assert result.attempts_submitted == 1
    repeat = await sync.maintain_attempts(db, app_bundle[2], now=deadline + timedelta(seconds=5))
    assert repeat.attempts_submitted == 0
    submissions = list((await db.scalars(select(Submission))).all())
    assert len(submissions) == 2
    assert {item.source for item in submissions} == {"DEADLINE"}
    assert all(not item.late for item in submissions)
    for index, binding in enumerate(questions):
        member = await db.get(Attempt, binding.attempt_id)
        assert member.state == "AUTO_SUBMITTED"
        submission = next(item for item in submissions if item.attempt_id == member.id)
        snapshot = await db.get(Snapshot, submission.snapshot_id)
        assert [file["content"] for file in snapshot.files] == [contents[index]]
    events = list((await db.scalars(select(SyncOutbox))).all())
    assert len(events) == 1
    assert events[0].state == "PENDING"
    assert events[0].payload["reason"] == "DEADLINE"
    assert len(events[0].payload["quiz_questions"]) == 2
    assert root.expected_end_at - root.deadline_at == timedelta(seconds=300)


async def test_reserve_larger_than_remaining_time_submits_without_negative_timer(
    db,
    app_bundle,
    monkeypatch,
):
    now = datetime.now(UTC)
    monkeypatch.setattr("app.services.workspace.utcnow", lambda: now)
    _, _, _, root, _ = await _start_multi_quiz(
        db,
        prepared=replace(_multi_quiz_preparation(), remaining_seconds=60),
    )
    assert root.deadline_at == now
    assert root.expected_end_at == now + timedelta(seconds=60)
    result = await sync.maintain_attempts(db, app_bundle[2], now=now)
    assert result.attempts_submitted == 1
    assert root.state == "AUTO_SUBMITTED"
