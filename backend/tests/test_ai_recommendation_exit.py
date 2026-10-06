from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.ai import _code_review_read, process_next_ai_code_review
from app.auth.context import require_auth
from app.db.base import utcnow
from app.models.analysis import AICodeReview
from app.models.attempts import Snapshot, Submission
from app.models.courses import Course
from app.models.identity import ExternalPrincipal, MoodleCredential
from app.models.integration import SyncOutbox
from app.models.review import ReviewDecision, ReviewDraft
from app.models.tasks import Assessment
from app.services.ai_review import enqueue_ai_code_review, recommendation_status
from app.services.ai_review_exit import discard_ai_recommendation
from app.services.common import DomainError
from app.services.moodle_history import materialize_historical_submissions
from app.services.review import claim_submission, finalize_review, save_review_draft
from app.services.sync import _BrowserDeliveryResult, process_outbox_once, run_scheduler_iteration
from tests.test_ai_code_review import ReviewProvider
from tests.test_api_integrity_ai_system import _context, _csrf
from tests.test_moodle_history import (
    _browser_state,
    _finished_item,
    _multi_essay_item,
    _seed_history_target,
)


async def _seed_report(
    bundle, *, age=timedelta(), remote_attempt="142664", manual_requested=True,
):
    _, sessions, settings = bundle
    ids = await _seed_history_target(sessions, settings=settings)
    item = {
        **_finished_item(), "attempt_id": remote_attempt,
        "external_id": f"quiz:777:{remote_attempt}",
    }
    now = utcnow()
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db, course=course, assessment=assessment, actor_external_subject="42", items=[item],
        )
        submission = await db.scalar(select(Submission))
        report = AICodeReview(
            submission_id=submission.id, snapshot_id=submission.snapshot_id,
            requested_by_id=ids["teacher_id"], manual_requested=manual_requested,
            requested_with_system_access=True,
            manifest_hash=(await db.get(Snapshot, submission.snapshot_id)).manifest_hash,
            state="COMPLETED", completed_at=now - age, summary="Обзор",
            recommended_grade=Decimal("7.5"), recommended_comment="Проверьте пустой ввод.",
            findings=[{"path": "main.cpp", "line": 1, "severity": "warning", "message": "Ошибка"}],
        )
        db.add(report)
        await db.flush()
        ids.update(
            submission_id=submission.id, report_id=report.id,
            student_id=await db.scalar(select(ExternalPrincipal.id).where(
                ExternalPrincipal.connection_id == course.connection_id,
                ExternalPrincipal.external_subject == "99",
            )),
        )
    return ids, now, item


async def _discard(bundle, ids):
    async with bundle[1]() as db, db.begin():
        return await discard_ai_recommendation(
            db, submission_id=ids["submission_id"], report_id=ids["report_id"],
            actor_id=ids["teacher_id"], allow_system_settings_read=True,
        )


@pytest.mark.parametrize("state", ["QUEUED", "RUNNING", "COMPLETED", "FAILED"])
async def test_automatic_report_and_job_survive_exit_without_moodle_refresh(app_bundle, state):
    _, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle, manual_requested=False)
    async with sessions() as db, db.begin():
        report = await db.get(AICodeReview, ids["report_id"])
        report.state = state
        report.attempt_count = 2
        report.next_attempt_at = utcnow() + timedelta(seconds=30)
        await db.flush()
        original = (report.run_token, report.updated_at, report.next_attempt_at)
    # Repeated visits, even with AI disabled, must not revoke the cached result
    # or cancel/restart the automatic job and its remaining retry budget.
    settings.ai_enabled = False
    await _discard(app_bundle, ids)
    await _discard(app_bundle, ids)
    async with sessions() as db:
        report = await db.get(AICodeReview, ids["report_id"])
        assert report.state == state
        assert report.discarded_at is None
        assert report.approved_decision_id is None
        assert report.attempt_count == 2
        assert (report.run_token, report.updated_at, report.next_attempt_at) == tuple(
            value.replace(tzinfo=None) if isinstance(value, datetime) else value
            for value in original
        )
        assert report.recommended_grade == Decimal("7.5")
        assert report.recommended_comment == "Проверьте пустой ввод."
        assert report.findings[0]["message"] == "Ошибка"
        assert _code_review_read(report, settings).automatic is True
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
        assert (await db.scalar(select(ReviewDecision))).grade == Decimal("8.5")


async def test_automatic_result_reopens_from_cache_without_another_model_call(app_bundle):
    app, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle, manual_requested=False)
    settings.ai_mock_enabled = True
    provider = ReviewProvider()
    app.state.ai_transport = provider
    app.dependency_overrides[require_auth] = lambda: _context(
        ids["teacher_id"], capabilities=("SYSTEM_SETTINGS",),
    )
    url = f'/api/v1/ai/submissions/{ids["submission_id"]}/code-review'
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            headers = await _csrf(client)
            first = await client.get(url)
            assert first.status_code == 200
            assert first.json()["automatic"] is True
            assert first.json()["recommendation_status"] == "PENDING"
            for _ in range(2):
                exited = await client.post(
                    f"{url}/discard", json={"review_id": str(ids["report_id"])}, headers=headers,
                )
                assert exited.status_code == 200
                assert exited.json() == first.json()
                reopened = await client.get(url)
                assert reopened.status_code == 200
                assert reopened.json() == first.json()
            # A duplicate explicit launch also reuses the completed automatic
            # report instead of turning it into another paid manual request.
            repeated = await client.post(url, json={}, headers=headers)
            assert repeated.status_code == 200
            assert repeated.json() == first.json()
        assert not await process_next_ai_code_review(sessions, settings, provider)
        assert provider.calls == 0
        async with sessions() as db:
            assert await db.scalar(select(func.count(AICodeReview.id))) == 1
            assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
    finally:
        app.dependency_overrides.clear()


async def test_automatic_exit_preserves_another_teachers_ai_draft(app_bundle):
    _, sessions, _ = app_bundle
    ids, _, _ = await _seed_report(app_bundle, manual_requested=False)
    async with sessions() as db, db.begin():
        teacher = await db.get(ExternalPrincipal, ids["teacher_id"])
        other_teacher = ExternalPrincipal(
            connection_id=teacher.connection_id, external_subject="other-teacher",
            display_name="Другой преподаватель", active=True,
        )
        db.add(other_teacher)
        await db.flush()
        other_id = other_teacher.id
        db.add(ReviewDraft(
            submission_id=ids["submission_id"], owner_id=other_id,
            ai_review_id=ids["report_id"], grade=Decimal("4"), comment="ИИ-черновик",
        ))
    await _discard(app_bundle, ids)
    async with sessions() as db:
        drafts = list((await db.scalars(select(ReviewDraft))).all())
        assert len(drafts) == 1
        assert drafts[0].owner_id == other_id
        assert drafts[0].ai_review_id == ids["report_id"]
        assert drafts[0].comment == "ИИ-черновик"
        assert recommendation_status(await db.get(AICodeReview, ids["report_id"])) == "PENDING"
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0


async def test_automatic_analysis_finishes_once_after_viewer_leaves_while_running(app_bundle):
    _, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle, manual_requested=False)
    settings.ai_mock_enabled = True
    async with sessions() as db, db.begin():
        report = await db.get(AICodeReview, ids["report_id"])
        report.state = "QUEUED"
        report.recommended_grade = None
        (await db.get(Assessment, ids["assessment_id"])).ai_review_enabled = True
        # This automatic job was scheduled for an as-yet ungraded submission.
        (await db.scalar(select(ReviewDecision))).status = "SUPERSEDED"
        submission = await db.get(Submission, ids["submission_id"])
        submission.external_receipt = {
            key: value for key, value in submission.external_receipt.items()
            if key != "moodle_review"
        }

    async def leave_while_running():
        await _discard(app_bundle, ids)
        async with sessions() as db:
            report = await db.get(AICodeReview, ids["report_id"])
            assert report.state == "RUNNING"
            assert report.discarded_at is None

    provider = ReviewProvider(callback=leave_while_running)
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 1
    async with sessions() as db:
        report = await db.get(AICodeReview, ids["report_id"])
        assert report.state == "COMPLETED"
        assert report.discarded_at is None
        assert recommendation_status(report) == "PENDING"
        assert _code_review_read(report, settings).findings
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
    await _discard(app_bundle, ids)
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 1


def test_recommendation_has_no_time_limit_and_requires_explicit_discard():
    completed = datetime(2000, 1, 1, tzinfo=UTC)
    report = AICodeReview(
        state="COMPLETED", completed_at=completed, recommended_grade=Decimal("0"),
        recommended_comment="Не решено",
    )
    assert recommendation_status(report) == "PENDING"
    report.discarded_at = utcnow()
    assert recommendation_status(report) == "DISCARDED"
    report.approved_decision_id = uuid.uuid4()
    assert recommendation_status(report) == "APPROVED"


async def test_exit_queues_one_exact_read_even_when_ai_disabled_and_retains_the_report(
    app_bundle,
):
    _, sessions, settings = app_bundle
    ids, now, _ = await _seed_report(app_bundle)
    settings.ai_enabled = False
    # Merely waiting or restarting the scheduler must never discard a report.
    await run_scheduler_iteration(sessions, settings, now=now + timedelta(hours=24))
    await run_scheduler_iteration(sessions, settings, now=now + timedelta(days=3))
    async with sessions() as db:
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
        assert (await db.get(AICodeReview, ids["report_id"])).discarded_at is None
    await _discard(app_bundle, ids)
    await _discard(app_bundle, ids)
    async with sessions() as db:
        events = list((await db.scalars(select(SyncOutbox))).all())
        assert len(events) == 1
        event = events[0]
        assert event.event_type == "moodle.history.import"
        assert event.aggregate_id == ids["assessment_id"]
        assert event.payload["actor_external_subject"] == "42"
        assert event.payload["attempt_refs"][0]["attempt_id"] == "142664"
        assert event.payload["attempt_refs"][0]["user_id"] == "99"
        assert event.payload["attempt_refs"][0]["submitted_at_epoch"] == 1_777_000_000
        assert "scan_only" not in event.payload
        report = await db.get(AICodeReview, ids["report_id"])
        assert report.discarded_at is not None
        assert report.moodle_refresh_outbox_id == event.id
        assert report.recommended_grade == Decimal("7.5")
        assert report.findings[0]["message"] == "Ошибка"
        decision = await db.scalar(select(ReviewDecision))
        assert decision.grade == Decimal("8.5")
        assert decision.lms_export_state == "IMPORTED"


async def test_discarded_report_hides_suggestion_and_findings_and_shows_refresh_failure(app_bundle):
    app, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle, age=timedelta(days=2))
    await _discard(app_bundle, ids)
    async with sessions() as db, db.begin():
        report = await db.get(AICodeReview, ids["report_id"])
        event = await db.get(SyncOutbox, report.moodle_refresh_outbox_id)
        event.state = "BLOCKED"
        event.last_error = "MOODLE_AUTHENTICATION_FAILED: private internal response"
    settings.ai_mock_enabled = True
    app.dependency_overrides[require_auth] = lambda: _context(
        ids["teacher_id"], capabilities=("SYSTEM_SETTINGS",),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            response = await client.get(
                f'/api/v1/ai/submissions/{ids["submission_id"]}/code-review',
            )
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["recommendation_status"] == "DISCARDED"
        assert data["state"] == "NOT_STARTED"
        assert data["recommended_grade"] is None
        assert data["recommended_comment"] == ""
        assert data["findings"] == []
        assert "сессия Moodle" in data["moodle_refresh_error"]
        assert "private internal" not in response.text
    finally:
        app.dependency_overrides.clear()


async def test_two_discarded_question_recommendations_share_one_moodle_attempt_read(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        item = {**_multi_essay_item(), "attempt_id": "142664", "external_id": "quiz:777:142664"}
        await materialize_historical_submissions(
            db, course=course, assessment=assessment, actor_external_subject="42", items=[item],
        )
        for submission in (await db.scalars(select(Submission))).all():
            db.add(AICodeReview(
                submission_id=submission.id, snapshot_id=submission.snapshot_id,
                requested_by_id=ids["teacher_id"], manual_requested=True,
                requested_with_system_access=True, manifest_hash="a" * 64,
                state="COMPLETED", completed_at=utcnow() - timedelta(days=2),
                recommended_grade=Decimal("1"), recommended_comment="Предложение",
            ))
    async with sessions() as db:
        reports = (await db.scalars(select(AICodeReview))).all()
    for report in reports:
        await _discard(app_bundle, {
            **ids, "submission_id": report.submission_id, "report_id": report.id,
        })
    async with sessions() as db:
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 1
        references = (await db.scalars(select(AICodeReview.moodle_refresh_outbox_id))).all()
        assert references[0] is not None and len(set(references)) == 1


async def test_approval_before_exit_pins_decision_and_never_schedules_a_rollback(app_bundle):
    _, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle, age=timedelta(days=2))
    async with sessions() as db, db.begin():
        await claim_submission(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        decision = await finalize_review(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            grade=Decimal("7.5"), comment="Проверьте пустой ввод.",
            ai_review_id=ids["report_id"], idempotency_key="approve-ai",
            allow_system_settings_read=True,
        )
        decision_id = decision.id
    await _discard(app_bundle, ids)
    async with sessions() as db, db.begin():
        report = await db.get(AICodeReview, ids["report_id"])
        assert report.approved_decision_id == decision_id
        assert report.discarded_at is None
        assert _code_review_read(report, settings).recommendation_status == "APPROVED"
        event = await db.scalar(select(SyncOutbox))
        assert event.event_type == "review.decision"
        assert event.payload["grade"] == "7.5"
        assert event.payload["comment"] == "Проверьте пустой ввод."
        repeated = await finalize_review(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            grade=Decimal("7.5"), comment="Проверьте пустой ввод.",
            ai_review_id=ids["report_id"], idempotency_key="approve-ai",
            allow_system_settings_read=True,
        )
        assert repeated.id == decision_id
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 1


@pytest.mark.parametrize(
    "invalid", ["discarded", "different-snapshot", "foreign"],
)
async def test_invalid_or_stale_ai_approval_never_changes_official_decision(app_bundle, invalid):
    _, sessions, _ = app_bundle
    ids, _, _ = await _seed_report(app_bundle)
    async with sessions() as db, db.begin():
        await claim_submission(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
    async with sessions() as db:
        if invalid == "discarded":
            (await db.get(AICodeReview, ids["report_id"])).discarded_at = utcnow()
            await db.flush()
        if invalid == "different-snapshot":
            report = await db.get(AICodeReview, ids["report_id"])
            original = await db.get(Snapshot, report.snapshot_id)
            changed = Snapshot(
                workspace_id=original.workspace_id, revision=original.revision + 1,
                manifest_hash="b" * 64, files=original.files, reason="LMS_IMPORT",
            )
            db.add(changed)
            await db.flush()
            report.snapshot_id = changed.id
            await db.flush()
        with pytest.raises(DomainError) as error:
            await finalize_review(
                db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
                grade=Decimal("7.5"),
                comment="Проверьте пустой ввод.",
                ai_review_id=uuid.uuid4() if invalid == "foreign" else ids["report_id"],
                allow_system_settings_read=True,
            )
        assert error.value.code in {"AI_RECOMMENDATION_DISCARDED", "AI_RECOMMENDATION_CHANGED"}
        await db.rollback()
    async with sessions() as db:
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
        assert (await db.scalar(select(ReviewDecision))).grade == Decimal("8.5")


async def test_moodle_read_after_exit_updates_official_grade_but_keeps_ai_audit(app_bundle):
    _, sessions, settings = app_bundle
    ids, _, item = await _seed_report(app_bundle, age=timedelta(days=2))
    await _discard(app_bundle, ids)

    class FakeMoodle:
        async def discover_historical_submissions(self, payload):
            assert payload["attempt_refs"][0]["attempt_id"] == "142664"
            return _BrowserDeliveryResult(
                value={
                    "course_id": "549", "activity": {"module": "quiz", "cmid": 777},
                    "items": [{**item, "grade": "0", "comment": "Новое решение из Moodle"}],
                    "next_cursor": None, "complete": True, "warnings": [],
                },
                storage_state=_browser_state("after-read"),
            )

    async with AsyncClient() as client:
        assert await process_outbox_once(
            sessions, settings, bridge_factory=lambda *_args: FakeMoodle(), client=client,
        )
    async with sessions() as db:
        current = await db.scalar(select(ReviewDecision).where(ReviewDecision.status == "APPLIED"))
        assert current.grade == Decimal("0")
        assert current.comment == "Новое решение из Moodle"
        report = await db.get(AICodeReview, ids["report_id"])
        assert report.recommended_grade == Decimal("7.5")
        assert report.recommended_comment == "Проверьте пустой ввод."
        assert await db.scalar(select(func.count(AICodeReview.id))) == 1
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 1
        assert (await db.scalar(select(SyncOutbox))).state == "DELIVERED"


@pytest.mark.parametrize("problem", ["no-identity", "revoked-actor", "expired-session"])
async def test_refresh_failure_never_erases_moodle_feedback_or_exports_ai(app_bundle, problem):
    _, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(
        app_bundle, age=timedelta(days=2),
        remote_attempt="unknown" if problem == "no-identity" else "142664",
    )
    async with sessions() as db, db.begin():
        if problem == "revoked-actor":
            (await db.get(ExternalPrincipal, ids["teacher_id"])).active = False
        if problem == "expired-session":
            (await db.scalar(select(MoodleCredential))).status = "EXPIRED"
    await _discard(app_bundle, ids)
    await _discard(app_bundle, ids)
    if problem == "expired-session":
        def no_external_call(*_args):
            pytest.fail("An expired Moodle session must be rejected before any network call")

        async with AsyncClient() as client:
            assert await process_outbox_once(
                sessions, settings, bridge_factory=no_external_call, client=client,
            )
    async with sessions() as db:
        report = await db.get(AICodeReview, ids["report_id"])
        if problem != "expired-session":
            assert report.moodle_refresh_outbox_id is None
            assert report.moodle_refresh_error
        else:
            event = await db.get(SyncOutbox, report.moodle_refresh_outbox_id)
            assert event.event_type == "moodle.history.import"
            assert event.state == "BLOCKED"
            assert event.last_error.startswith("LMS_REAUTH_REQUIRED:")
        assert (await db.scalar(select(ReviewDecision))).grade == Decimal("8.5")
        assert await db.scalar(select(func.count(SyncOutbox.id)).where(
            SyncOutbox.event_type == "review.decision",
        )) == 0


async def test_discard_endpoint_requires_csrf_teacher_scope_and_current_report(app_bundle):
    app, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle)
    settings.ai_enabled = False  # Cancel does not depend on provider availability.
    actor = {"auth": _context(ids["teacher_id"], capabilities=("SYSTEM_SETTINGS",))}
    app.dependency_overrides[require_auth] = lambda: actor["auth"]
    url = f'/api/v1/ai/submissions/{ids["submission_id"]}/code-review/discard'
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            body = {"review_id": str(ids["report_id"])}
            assert (await client.post(url, json=body)).status_code == 403
            headers = await _csrf(client)
            actor["auth"] = _context(ids["student_id"], roles=("STUDENT",))
            assert (await client.post(url, json=body, headers=headers)).status_code in {403, 404}
            actor["auth"] = _context(ids["teacher_id"], capabilities=("SYSTEM_SETTINGS",))
            assert (await client.post(
                url, json={"review_id": str(uuid.uuid4())}, headers=headers,
            )).status_code == 409
            response = await client.post(url, json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["recommendation_status"] == "DISCARDED"
            assert response.json()["moodle_refresh_state"] == "PENDING"
            assert response.json()["recommended_grade"] is None
            assert response.json()["findings"] == []
            assert (await client.post(url, json=body, headers=headers)).status_code == 200
        async with sessions() as db:
            assert await db.scalar(select(func.count(SyncOutbox.id))) == 1
            assert (await db.scalar(select(ReviewDecision))).grade == Decimal("8.5")
    finally:
        app.dependency_overrides.clear()


async def test_queued_analysis_is_cancelled_and_only_explicit_retry_restarts_it(app_bundle):
    app, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle)
    settings.ai_mock_enabled = True
    async with sessions() as db, db.begin():
        report = await db.get(AICodeReview, ids["report_id"])
        report.state = "QUEUED"
        old_token = report.run_token
        # An ungraded Moodle answer with automatic opt-in must not revive the
        # cancelled job on every subsequent history synchronization.
        (await db.scalar(select(ReviewDecision))).status = "SUPERSEDED"
        submission = await db.get(Submission, ids["submission_id"])
        submission.external_receipt = {
            k: v for k, v in submission.external_receipt.items() if k != "moodle_review"
        }
        (await db.get(Assessment, ids["assessment_id"])).ai_review_enabled = True
    await _discard(app_bundle, ids)
    async with sessions() as db, db.begin():
        report = await enqueue_ai_code_review(db, await db.get(Submission, ids["submission_id"]))
        assert report.state == "SKIPPED"
        assert report.discarded_at is not None
    provider = ReviewProvider()
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0
    app.dependency_overrides[require_auth] = lambda: _context(
        ids["teacher_id"], capabilities=("SYSTEM_SETTINGS",),
    )
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            response = await client.post(
                f'/api/v1/ai/submissions/{ids["submission_id"]}/code-review',
                json={}, headers=await _csrf(client),
            )
            assert response.status_code == 200, response.text
            assert response.json()["state"] == "QUEUED"
        async with sessions() as db:
            report = await db.get(AICodeReview, ids["report_id"])
            assert report.discarded_at is None
            assert report.run_token != old_token
            assert report.moodle_refresh_outbox_id is None
    finally:
        app.dependency_overrides.clear()


async def test_result_arriving_after_exit_stays_discarded_and_cannot_be_applied(app_bundle):
    _, sessions, settings = app_bundle
    ids, _, _ = await _seed_report(app_bundle)
    settings.ai_mock_enabled = True
    async with sessions() as db, db.begin():
        (await db.get(AICodeReview, ids["report_id"])).state = "QUEUED"

    async def leave_while_running():
        await _discard(app_bundle, ids)
        async with sessions() as db:
            report = await db.get(AICodeReview, ids["report_id"])
            result = _code_review_read(report, settings)
            assert result.state == "RUNNING"
            assert result.recommendation_status == "DISCARDED"
            assert result.findings == []

    provider = ReviewProvider(callback=leave_while_running)
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 1
    async with sessions() as db:
        report = await db.get(AICodeReview, ids["report_id"])
        assert report.state == "COMPLETED"
        result = _code_review_read(report, settings)
        assert result.state == "NOT_STARTED"
        assert result.recommendation_status == "DISCARDED"
        assert result.recommended_grade is None
        assert result.findings == []
        assert (await db.scalar(select(ReviewDecision))).grade == Decimal("8.5")
        assert await db.scalar(select(func.count(SyncOutbox.id)).where(
            SyncOutbox.event_type == "review.decision",
        )) == 0


async def test_moodle_refresh_uses_exiting_teacher_not_old_ai_requester(app_bundle):
    _, sessions, _ = app_bundle
    ids, _, _ = await _seed_report(app_bundle)
    async with sessions() as db, db.begin():
        original = await db.get(ExternalPrincipal, ids["teacher_id"])
        current = ExternalPrincipal(
            connection_id=original.connection_id, external_subject="43",
            display_name="Current teacher", active=True,
        )
        db.add(current)
        await db.flush()
        await discard_ai_recommendation(
            db, submission_id=ids["submission_id"], report_id=ids["report_id"],
            actor_id=current.id, allow_system_settings_read=True,
        )
    async with sessions() as db:
        event = await db.scalar(select(SyncOutbox))
        assert event.payload["actor_external_subject"] == "43"
        assert (await db.get(AICodeReview, ids["report_id"])).requested_by_id == ids["teacher_id"]


@pytest.mark.parametrize("approve", [False, True])
@pytest.mark.parametrize("manual_requested", [False, True])
async def test_ai_draft_is_not_a_decision_and_only_main_approval_exports_edited_fields(
    app_bundle, approve, manual_requested,
):
    _, sessions, _ = app_bundle
    ids, _, _ = await _seed_report(app_bundle, manual_requested=manual_requested)
    async with sessions() as db, db.begin():
        await claim_submission(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        await save_review_draft(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            grade=Decimal("4"), comment="Исправлено преподавателем", ai_review_id=ids["report_id"],
            allow_system_settings_read=True,
        )
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
        assert (await db.scalar(select(ReviewDecision))).grade == Decimal("8.5")
        assert (await db.get(AICodeReview, ids["report_id"])).approved_decision_id is None
        if approve:
            await finalize_review(
                db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
                grade=Decimal("4"), comment="Исправлено преподавателем",
                ai_review_id=ids["report_id"], allow_system_settings_read=True,
            )
    await _discard(app_bundle, ids)
    async with sessions() as db:
        current = await db.scalar(select(ReviewDecision).where(ReviewDecision.status == "APPLIED"))
        event = await db.scalar(select(SyncOutbox))
        if approve:
            assert current.grade == Decimal("4")
            assert current.comment == "Исправлено преподавателем"
            assert event.event_type == "review.decision"
            assert event.payload["grade"] == "4"
            assert (await db.get(AICodeReview, ids["report_id"])).discarded_at is None
        else:
            assert current.grade == Decimal("8.5")
            assert await db.scalar(select(ReviewDraft)) is None
            if manual_requested:
                assert event.event_type == "moodle.history.import"
            else:
                assert event is None
                report = await db.get(AICodeReview, ids["report_id"])
                assert recommendation_status(report) == "PENDING"
                assert report.recommended_grade == Decimal("7.5")


@pytest.mark.parametrize("manual_requested", [False, True])
async def test_discard_preserves_unrelated_handwritten_draft(app_bundle, manual_requested):
    _, sessions, _ = app_bundle
    ids, _, _ = await _seed_report(app_bundle, manual_requested=manual_requested)
    async with sessions() as db, db.begin():
        db.add(ReviewDraft(
            submission_id=ids["submission_id"], owner_id=ids["teacher_id"],
            grade=Decimal("6"), comment="Личный черновик",
        ))
    await _discard(app_bundle, ids)
    async with sessions() as db:
        assert (await db.scalar(select(ReviewDraft))).comment == "Личный черновик"


async def test_stale_ai_draft_cannot_be_saved_after_exit(app_bundle):
    _, sessions, _ = app_bundle
    ids, _, _ = await _seed_report(app_bundle)
    await _discard(app_bundle, ids)
    async with sessions() as db, db.begin():
        await claim_submission(
            db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        with pytest.raises(DomainError, match="Рекомендация ИИ больше неактуальна"):
            await save_review_draft(
                db, submission_id=ids["submission_id"], teacher_id=ids["teacher_id"],
                grade=Decimal("4"), comment="Старая вкладка", ai_review_id=ids["report_id"],
                allow_system_settings_read=True,
            )
        assert await db.scalar(select(ReviewDraft)) is None
