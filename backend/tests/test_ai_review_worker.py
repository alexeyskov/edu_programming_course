from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.api import ai as ai_api
from app.integrations.ai import AIAnswer
from app.models.analysis import AICodeReview
from app.models.attempts import Submission
from app.models.courses import Course
from app.models.integration import SyncOutbox
from app.models.review import ReviewDecision
from app.models.tasks import Assessment
from app.services.moodle_history import materialize_historical_submissions
from app.services.workspace import start_attempt, submit_attempt
from app.workers import ai_review as ai_worker
from app.workers import sync as sync_worker
from tests.test_attempt_review_services import _seed_course, _start_multi_quiz
from tests.test_moodle_history import _multi_essay_item, _seed_history_target


@pytest.mark.parametrize("processed", [True, False])
async def test_once_processes_at_most_one_job_without_polling(
    app_bundle, monkeypatch, processed,
):
    _, session_factory, settings = app_bundle
    processor = AsyncMock(return_value=processed)
    sleep = AsyncMock()
    monkeypatch.setattr(ai_worker, "process_next_ai_code_review", processor)

    result = await ai_worker.run_ai_review_worker(
        session_factory, settings, once=True, sleep=sleep,
    )

    assert result == int(processed)
    processor.assert_awaited_once_with(session_factory, settings)
    sleep.assert_not_awaited()


async def test_worker_remains_sequential_and_polls_only_when_idle(app_bundle, monkeypatch):
    _, session_factory, settings = app_bundle
    stop = asyncio.Event()
    responses = iter([True, True, False])
    calls = 0
    active = 0
    maximum_active = 0

    async def process(*_args):
        nonlocal calls, active, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return next(responses)

    async def sleep(seconds):
        assert seconds == 1.0
        stop.set()

    monkeypatch.setattr(ai_worker, "process_next_ai_code_review", process)
    processed = await ai_worker.run_ai_review_worker(
        session_factory, settings, stop_event=stop, sleep=sleep,
    )

    assert processed == 2
    assert calls == 3
    assert maximum_active == 1


async def test_worker_recovers_from_iteration_failure(app_bundle, monkeypatch, caplog):
    _, session_factory, settings = app_bundle
    stop = asyncio.Event()
    calls = 0

    async def process(*_args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        stop.set()
        return True

    sleep = AsyncMock()
    monkeypatch.setattr(ai_worker, "process_next_ai_code_review", process)
    result = await ai_worker.run_ai_review_worker(
        session_factory, settings, stop_event=stop, sleep=sleep,
    )

    assert result == 1
    assert calls == 2
    sleep.assert_awaited_once_with(1.0)
    assert "AI review worker iteration failed" in caplog.text


async def test_worker_propagates_cancellation(app_bundle, monkeypatch):
    _, session_factory, settings = app_bundle
    processor = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr(ai_worker, "process_next_ai_code_review", processor)

    with pytest.raises(asyncio.CancelledError):
        await ai_worker.run_ai_review_worker(session_factory, settings, once=True)


async def test_lifespan_runs_independent_ai_and_terminal_workers(app_bundle, monkeypatch):
    app, session_factory, settings = app_bundle
    settings.sync_embedded_terminal_worker_enabled = True
    ai_started = asyncio.Event()
    terminal_started = asyncio.Event()
    ai_stopped = asyncio.Event()
    terminal_stopped = asyncio.Event()

    async def ai_loop(factory, config, *, stop_event):
        assert factory is session_factory and config is settings
        ai_started.set()
        try:
            await stop_event.wait()
        finally:
            ai_stopped.set()
        return 0

    async def terminal_loop(factory, config, *, stop_event, **options):
        assert factory is session_factory and config is settings
        assert options == {"terminal_checkpoints_only": True, "concurrency": 1}
        terminal_started.set()
        try:
            await stop_event.wait()
        finally:
            terminal_stopped.set()
        return 0

    monkeypatch.setattr(ai_worker, "run_ai_review_worker", ai_loop)
    monkeypatch.setattr(sync_worker, "run_sync_worker", terminal_loop)
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(ai_started.wait(), timeout=1)
        await asyncio.wait_for(terminal_started.wait(), timeout=1)
        assert app.state.ai_review_worker_task is not app.state.terminal_checkpoint_worker_task
        assert not app.state.ai_review_worker_task.done()
        assert not app.state.terminal_checkpoint_worker_task.done()
    assert ai_stopped.is_set() and terminal_stopped.is_set()
    assert app.state.ai_review_worker_task.done()
    assert app.state.terminal_checkpoint_worker_task.done()


@pytest.mark.parametrize("enabled", [None, True, False])
@pytest.mark.parametrize("source", ["MANUAL", "DEADLINE"])
async def test_new_submission_defaults_to_advisory_review_and_respects_opt_out(db, enabled, source):
    student, _, _, assessment = await _seed_course(db)
    assert assessment.ai_review_enabled is True
    assert assessment.student_ai_enabled is False
    if enabled is not None:
        assessment.ai_review_enabled = enabled
    attempt = await start_attempt(
        db, assessment_id=assessment.id, principal_id=student.id,
    )
    submission = await submit_attempt(
        db, attempt_id=attempt.id, principal_id=student.id, expected_revision=0, source=source,
    )
    repeated = await submit_attempt(
        db, attempt_id=attempt.id, principal_id=student.id, expected_revision=0, source=source,
    )
    reports = list((await db.scalars(select(AICodeReview))).all())

    assert repeated.id == submission.id
    assert len(reports) == int(enabled is not False)
    if reports:
        assert reports[0].submission_id == submission.id
        assert reports[0].snapshot_id == submission.snapshot_id
        assert reports[0].state == "QUEUED"
    assert await db.scalar(select(func.count()).select_from(SyncOutbox)) >= 1


async def test_advisory_job_rolls_back_with_submission(app_bundle):
    _, session_factory, _ = app_bundle
    async with session_factory() as db, db.begin():
        student, _, _, assessment = await _seed_course(db)
        assessment.ai_review_enabled = True
        attempt = await start_attempt(
            db, assessment_id=assessment.id, principal_id=student.id,
        )
        student_id, attempt_id = student.id, attempt.id

    async with session_factory() as db:
        await submit_attempt(
            db, attempt_id=attempt_id, principal_id=student_id, expected_revision=0,
        )
        assert await db.scalar(select(func.count()).select_from(AICodeReview)) == 1
        await db.rollback()

    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(AICodeReview)) == 0
        assert await db.scalar(select(func.count()).select_from(Submission)) == 0


@pytest.mark.parametrize("provider_enabled", [True, False])
async def test_worker_analyzes_committed_submission_without_opening_review(
    app_bundle, monkeypatch, provider_enabled,
):
    _, session_factory, settings = app_bundle
    settings.ai_enabled = False
    settings.ai_mock_enabled = provider_enabled
    provider = AsyncMock()
    provider.answer.return_value = AIAnswer(
        content='{"summary":"Проверьте решение по условию.","findings":[]}',
        citations=[], model="test-advisory-model",
    )
    monkeypatch.setattr(ai_api, "AIProvider", lambda *_args: provider)
    async with session_factory() as db, db.begin():
        student, _, _, assessment = await _seed_course(db)
        assessment.ai_review_enabled = True
        attempt = await start_attempt(
            db, assessment_id=assessment.id, principal_id=student.id,
        )
        await submit_attempt(
            db, attempt_id=attempt.id, principal_id=student.id, expected_revision=0,
        )
        provider.answer.assert_not_awaited()

    assert await ai_worker.run_ai_review_worker(
        session_factory, settings, once=True,
    ) == int(provider_enabled)
    async with session_factory() as db:
        report = await db.scalar(select(AICodeReview))
        assert report.state == ("COMPLETED" if provider_enabled else "QUEUED")
        assert await db.scalar(select(func.count()).select_from(ReviewDecision)) == 0
    if provider_enabled:
        provider.answer.assert_awaited_once()
        assert provider.answer.call_args.kwargs["context"]["purpose"] == "ADVISORY_CODE_REVIEW"
    else:
        provider.answer.assert_not_awaited()


async def test_multi_question_submission_enqueues_each_snapshot_using_parent_policy(db):
    student, _, assessment, root, questions = await _start_multi_quiz(db)
    assessment.ai_review_enabled = True
    await submit_attempt(
        db, attempt_id=root.id, principal_id=student.id, expected_revision=0,
    )
    await submit_attempt(
        db, attempt_id=questions[-1].attempt_id, principal_id=student.id, expected_revision=0,
    )

    submissions = list((await db.scalars(select(Submission))).all())
    reports = list((await db.scalars(select(AICodeReview))).all())
    assert len(submissions) == len(reports) == len(questions) == 2
    assert {report.submission_id for report in reports} == {row.id for row in submissions}
    assert {report.snapshot_id for report in reports} == {row.snapshot_id for row in submissions}
    assert all(report.state == "QUEUED" for report in reports)


@pytest.mark.parametrize("reviewed_slots, expected_jobs", [
    ((), 2), (("11",), 1), (("11", "12"), 0),
])
async def test_history_import_and_changed_snapshot_enqueue_without_duplicate_jobs(
    app_bundle, reviewed_slots, expected_jobs,
):
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)

    def history_item(**kwargs):
        item = _multi_essay_item(**kwargs)
        for response in item["responses"]:
            if response["response_id"] not in reviewed_slots:
                response["grade"] = None
                response["comment"] = ""
        return item

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assessment.ai_review_enabled = True
        created = await materialize_historical_submissions(
            db, course=course, assessment=assessment, actor_external_subject="42",
            items=[history_item()],
        )
        assert created.created == 2
        assert await db.scalar(select(func.count()).select_from(AICodeReview)) == expected_jobs
        unchanged = await materialize_historical_submissions(
            db, course=course, assessment=assessment, actor_external_subject="42",
            items=[history_item()],
        )
        assert unchanged.unchanged == 2
        assert await db.scalar(select(func.count()).select_from(AICodeReview)) == expected_jobs
        revised = history_item(revision="multi-attempt-r2")
        for response in revised["responses"]:
            response["answer_text"] += "\n// corrected answer\n"
        updated = await materialize_historical_submissions(
            db, course=course, assessment=assessment, actor_external_subject="42",
            items=[revised],
        )
        assert updated.updated == 2
        assert await db.scalar(select(func.count()).select_from(Submission)) == 2
        assert await db.scalar(select(func.count()).select_from(AICodeReview)) == expected_jobs * 2
