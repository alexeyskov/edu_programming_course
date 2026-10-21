from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.api.ai import _validated_code_review, process_next_ai_code_review
from app.auth.context import require_auth
from app.db.base import utcnow
from app.integrations.ai import AIAnswer
from app.integrations.errors import IntegrationResponseTooLarge, IntegrationTimeout
from app.models.analysis import AICodeReview
from app.models.attempts import Attempt, MoodleQuizQuestion, Snapshot, Submission
from app.models.courses import CourseMembership
from app.models.integration import SystemSetting
from app.models.review import ChatMessage, ReviewDecision
from app.models.tasks import Assessment
from app.services.ai_review import (
    ai_review_policy_assessment,
    enqueue_ai_code_review,
    has_teacher_review,
)
from tests.test_api_integrity_ai_system import _context, _csrf, _seed


@pytest_asyncio.fixture
async def review_bundle(app_bundle):
    app, sessions, settings = app_bundle
    seed = await _seed(sessions)
    # Exercise a saved administrative opt-out, independent of the new-work default.
    async with sessions() as db, db.begin():
        (await db.get(Assessment, seed.assessment_id)).ai_review_enabled = False
    settings.ai_mock_enabled = True
    holder = {"auth": _context(seed.teacher_id, roles=("TEACHER",))}

    async def auth():
        return holder["auth"]

    app.dependency_overrides[require_auth] = auth
    yield app, sessions, settings, seed, holder
    app.dependency_overrides.clear()


class ReviewProvider:
    def __init__(self, *, line=1, path="main.cpp", callback=None):
        self.calls = 0
        self.context = None
        self.line = line
        self.path = path
        self.callback = callback

    async def answer(self, **kwargs):
        self.calls += 1
        self.context = kwargs["context"]
        if self.callback:
            await self.callback()
        return AIAnswer(
            content=json.dumps(
                {
                    "summary": "Проверьте граничный случай.",
                    "recommended_grade": 7.5,
                    "recommended_comment": "Исправьте обработку пустого ввода.",
                    "findings": [
                        {
                            "path": self.path,
                            "line": self.line,
                            "end_line": self.line,
                            "severity": "warning",
                            "message": "Возможна ошибка при пустом вводе.",
                        }
                    ],
                }
            ),
            citations=[],
            model="test-model",
        )


async def _enable(sessions, seed, *, ungraded=True):
    async with sessions() as db, db.begin():
        assessment = await db.get(Assessment, seed.assessment_id)
        assessment.ai_review_enabled = True
        if ungraded:
            await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
        submission = await db.get(Submission, seed.submission_id)
        await enqueue_ai_code_review(db, submission)


async def _make_retry_due(sessions):
    async with sessions() as db, db.begin():
        await db.execute(update(AICodeReview).values(
            next_attempt_at=utcnow() - timedelta(seconds=1),
        ))


@pytest.mark.parametrize("successful_attempt", [2, 3])
async def test_transient_failures_retry_with_durable_budget_then_cache_success(
    review_bundle, successful_attempt,
):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    provider = AsyncMock()
    provider.answer.side_effect = IntegrationTimeout()
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        for number in range(1, successful_attempt):
            assert await process_next_ai_code_review(sessions, settings, provider)
            pending = (await client.get(url)).json()
            assert pending["state"] == "QUEUED"
            assert pending["attempt_count"] == number
            assert pending["max_attempts"] == 3
            assert pending["error"] == ""
            assert pending["recommended_grade"] is None
            assert pending["findings"] == []
            # No instant retry, no extra call from reload, double click, or another worker.
            assert not await process_next_ai_code_review(sessions, settings, provider)
            for _ in range(2):
                repeated = await client.post(url, json={"retry": True}, headers=headers)
                assert repeated.json() == pending
            assert provider.answer.await_count == number
            async with sessions() as db:
                job = await db.scalar(select(AICodeReview))
                assert job.completed_at is None
                assert job.next_attempt_at is not None
            await _make_retry_due(sessions)
        successful = ReviewProvider()
        assert await process_next_ai_code_review(sessions, settings, successful)
        completed = (await client.get(url)).json()
        assert completed["state"] == "COMPLETED"
        assert completed["attempt_count"] == successful_attempt
        assert completed["error"] == ""
        assert completed["recommended_grade"] == "7.50"
        assert not await process_next_ai_code_review(sessions, settings, successful)
        assert successful.calls == 1


@pytest.mark.parametrize("reason", ["timeout", "size", "schema", "json"])
async def test_three_failures_stop_and_diagnostics_do_not_leak_response(
    review_bundle, reason, caplog,
):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    provider = AsyncMock()
    secret_text = "DO_NOT_LOG_PRIVATE_CODE_OR_KEY"
    if reason in {"timeout", "size"}:
        provider.answer.side_effect = (IntegrationTimeout if reason == "timeout"
                                       else IntegrationResponseTooLarge)(secret_text)
    else:
        content = json.dumps({"summary": secret_text, "findings": secret_text,
                              secret_text: "unexpected"}) if reason == "schema" else secret_text
        provider.answer.return_value = AIAnswer(content=content, citations=[], model="test")
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    for _ in range(3):
        await _make_retry_due(sessions)
        assert await process_next_ai_code_review(sessions, settings, provider)
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.answer.await_count == 3
    assert "attempt 3/3 failed" in caplog.text
    assert secret_text not in caplog.text
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        failed = (await client.get(url)).json()
        assert failed["state"] == "FAILED"
        assert failed["attempt_count"] == 3
        assert failed["error"]
        assert secret_text not in json.dumps(failed)
        response = await client.post(url, json={"retry": True}, headers=await _csrf(client))
        restarted = response.json()
        assert restarted["state"] == "QUEUED"
        assert restarted["attempt_count"] == 0
        assert restarted["error"] == ""


@pytest.mark.parametrize(
    "reason", ["manual_exit", "automatic_exit", "policy", "moodle", "snapshot"],
)
async def test_retry_rechecks_exit_policy_moodle_and_snapshot_before_another_call(
    review_bundle, reason,
):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    if reason == "manual_exit":
        async with sessions() as db, db.begin():
            (await db.scalar(select(AICodeReview))).manual_requested = True
    provider = AsyncMock()
    provider.answer.side_effect = IntegrationTimeout()
    assert await process_next_ai_code_review(sessions, settings, provider)
    if reason in {"manual_exit", "automatic_exit"}:
        async with sessions() as db:
            report_id = (await db.scalar(select(AICodeReview))).id
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            response = await client.post(
                f"/api/v1/ai/submissions/{seed.submission_id}/code-review/discard",
                json={"review_id": str(report_id)}, headers=await _csrf(client),
            )
            assert response.status_code == 200
    elif reason == "snapshot":
        await _replace_snapshot(sessions, seed)
    else:
        async with sessions() as db, db.begin():
            if reason == "policy":
                (await db.get(Assessment, seed.assessment_id)).ai_review_enabled = False
            else:
                submission = await db.get(Submission, seed.submission_id)
                submission.external_receipt = {"moodle_review": {"grade": "0", "comment": ""}}
    await _make_retry_due(sessions)
    await process_next_ai_code_review(sessions, settings, provider)
    assert provider.answer.await_count == (2 if reason == "automatic_exit" else 1)
    if reason == "automatic_exit":
        async with sessions() as db:
            report = await db.scalar(select(AICodeReview))
            assert report.discarded_at is None
            assert report.state == "QUEUED"
            assert report.attempt_count == 2


async def test_discard_during_failed_call_prevents_automatic_retry(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    provider = AsyncMock()

    async def fail_after_exit(**_kwargs):
        async with sessions() as db, db.begin():
            (await db.scalar(select(AICodeReview))).discarded_at = utcnow()
        raise IntegrationTimeout()

    provider.answer.side_effect = fail_after_exit
    assert await process_next_ai_code_review(sessions, settings, provider)
    await _make_retry_due(sessions)
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.answer.await_count == 1


async def test_stale_third_attempt_is_terminal_without_a_fourth_call(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    async with sessions() as db, db.begin():
        job = await db.scalar(select(AICodeReview))
        job.state = "RUNNING"
        job.attempt_count = 3
        job.updated_at = utcnow() - timedelta(hours=1)
    provider = ReviewProvider()
    assert not await process_next_ai_code_review(sessions, settings, provider)
    async with sessions() as db:
        job = await db.scalar(select(AICodeReview))
        assert job.state == "FAILED"
        assert job.attempt_count == 3
        assert job.completed_at is not None
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("human_feedback", ["decision", "zero", "comment"])
async def test_teacher_feedback_prevents_automatic_analysis(review_bundle, human_feedback):
    _app, sessions, settings, seed, _ = review_bundle
    async with sessions() as db, db.begin():
        assessment = await db.get(Assessment, seed.assessment_id)
        assessment.ai_review_enabled = True
        submission = await db.get(Submission, seed.submission_id)
        if human_feedback != "decision":
            await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
            submission.external_receipt = {"moodle_review": {
                "grade": "0" if human_feedback == "zero" else None,
                "comment": "Алгоритм неверный" if human_feedback == "comment" else "",
            }}
        assert await enqueue_ai_code_review(db, submission) is None
    provider = ReviewProvider()
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "different", [None, "student", "course", "parent", "attempt", "slot", "retired"],
)
@pytest.mark.parametrize("binding", ["receipt", "root", "child"])
@pytest.mark.parametrize("imported_remote", ["142664", "attempt:142664"])
async def test_native_answer_uses_only_its_own_moodle_feedback(
    review_bundle, different, binding, imported_remote,
):
    _app, sessions, _settings, seed, _ = review_bundle
    async with sessions() as db, db.begin():
        await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
        native = await db.get(Submission, seed.submission_id)
        native.external_receipt = {
            "moodle_parent_assessment_id": str(seed.assessment_id),
            "moodle_parent_attempt_id": "142664", "moodle_response_id": "1",
        }
        if binding != "receipt":
            await _bind_receiptless_native_question(db, seed, native, child=binding == "child")
        parent_id = seed.assessment_id
        if different == "parent":
            other_parent = Assessment(
                course_id=seed.course_id, title="Another Quiz", created_by_id=seed.teacher_id,
            )
            db.add(other_parent)
            await db.flush()
            parent_id = other_parent.id
        assessment = Assessment(
            course_id=seed.other_course_id if different == "course" else seed.course_id,
            type="LAB", title="Imported question", created_by_id=seed.teacher_id,
            max_score=Decimal("10"),
            policy={"moodle_parent_assessment_id": str(parent_id)},
        )
        db.add(assessment)
        await db.flush()
        imported_attempt = Attempt(
            assessment_id=assessment.id,
            principal_id=seed.admin_id if different == "student" else seed.student_id,
            state="VOID" if different == "retired" else "SUBMITTED",
        )
        db.add(imported_attempt)
        await db.flush()
        db.add(Submission(
            attempt_id=imported_attempt.id, snapshot_id=native.snapshot_id,
            revision=1, source="MOODLE_IMPORT", external_receipt={
                "moodle_parent_attempt_id": "999" if different == "attempt" else imported_remote,
                "moodle_response_id": "2" if different == "slot" else "1",
                "moodle_review": {"grade": None, "comment": "Алгоритм неверный"},
            },
        ))
        await db.flush()
        assert await has_teacher_review(db, native) is (different is None)


async def _bind_receiptless_native_question(db, seed, native, *, child=False):
    attempt = await db.get(Attempt, native.attempt_id)
    attempt.state = "SUBMITTED"
    root = attempt
    if child:
        root = Attempt(
            assessment_id=seed.assessment_id, principal_id=seed.student_id,
            sequence=attempt.sequence + 1, state="SUBMITTED",
        )
        assessment = Assessment(
            course_id=seed.course_id, title="Native child", created_by_id=seed.teacher_id,
            policy={"moodle_parent_assessment_id": str(seed.assessment_id)},
        )
        db.add_all([root, assessment])
        await db.flush()
        attempt.assessment_id = assessment.id
    integrity = {
        "moodle_quiz_root_attempt_id": str(root.id),
        "moodle_parent_assessment_id": str(seed.assessment_id),
        "moodle_attempt_id": "142664",
        "moodle_question_slot": "1",
    }
    root.integrity_policy = dict(integrity)
    attempt.integrity_policy = dict(integrity)
    native.external_receipt = None
    db.add(MoodleQuizQuestion(
        root_attempt_id=root.id, attempt_id=attempt.id, question_slot="1",
        position=2 if child else 1, title="Question", question_max_mark=10,
    ))
    await db.flush()
    return attempt, root


@pytest.mark.parametrize("conflict", ["remote", "slot", "parent", "root", "student"])
async def test_receiptless_feedback_fallback_never_overrides_conflicting_bindings(
    review_bundle, conflict,
):
    _app, sessions, _settings, seed, _ = review_bundle
    async with sessions() as db, db.begin():
        await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
        native = await db.get(Submission, seed.submission_id)
        _attempt, root = await _bind_receiptless_native_question(db, seed, native, child=True)
        if conflict == "root":
            root.integrity_policy = {**root.integrity_policy, "moodle_attempt_id": "999"}
        elif conflict == "student":
            root.principal_id = seed.admin_id
        else:
            native.external_receipt = {
                {"remote": "moodle_parent_attempt_id", "slot": "moodle_response_id",
                 "parent": "moodle_parent_assessment_id"}[conflict]:
                str(uuid.uuid4()) if conflict == "parent" else "999",
            }
        imported_attempt = Attempt(
            assessment_id=seed.assessment_id, principal_id=seed.student_id,
            sequence=3, state="SUBMITTED",
        )
        db.add(imported_attempt)
        await db.flush()
        db.add(Submission(
            attempt_id=imported_attempt.id, snapshot_id=native.snapshot_id,
            revision=1, source="MOODLE_IMPORT", external_receipt={
                "moodle_parent_attempt_id": "142664", "moodle_response_id": "1",
                "moodle_review": {"grade": "0", "comment": "Проверено"},
            },
        ))
        await db.flush()
        assert not await has_teacher_review(db, native)
        if conflict in {"root", "student"}:
            assert native.external_receipt is None


@pytest.mark.parametrize("stage", ["backfill", "queued", "retry"])
async def test_native_null_receipt_skips_paid_auto_review_after_moodle_feedback(
    review_bundle, stage,
):
    _app, sessions, settings, seed, _ = review_bundle
    if stage != "backfill":
        await _enable(sessions, seed)
    async with sessions() as db, db.begin():
        await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
        native = await db.get(Submission, seed.submission_id)
        await _bind_receiptless_native_question(db, seed, native, child=True)
        (await db.get(Assessment, seed.assessment_id)).ai_review_enabled = True
        imported_attempt = Attempt(
            assessment_id=seed.assessment_id, principal_id=seed.student_id,
            sequence=3, state="SUBMITTED",
        )
        db.add(imported_attempt)
        await db.flush()
        db.add(Submission(
            attempt_id=imported_attempt.id, snapshot_id=native.snapshot_id,
            revision=1, source="MOODLE_IMPORT", external_receipt={
                "moodle_parent_attempt_id": "attempt:142664", "moodle_response_id": "1",
                "moodle_review": {"grade": "0", "comment": "Проверено в Moodle"},
            },
        ))
        await db.flush()
        if stage == "backfill":
            assert await enqueue_ai_code_review(db, native) is None
        elif stage == "retry":
            report = await db.scalar(select(AICodeReview))
            report.attempt_count = 1
            report.next_attempt_at = utcnow() - timedelta(seconds=1)
    provider = ReviewProvider()
    assert await process_next_ai_code_review(sessions, settings, provider) is (stage != "backfill")
    assert provider.calls == 0
    async with sessions() as db:
        report = await db.scalar(select(AICodeReview))
        if stage == "backfill":
            assert report is None
        else:
            assert report.state == "SKIPPED" and report.approved_decision_id is None
        assert (await db.get(Submission, seed.submission_id)).external_receipt is None


@pytest.mark.asyncio
async def test_feedback_arriving_after_enqueue_skips_paid_call_but_manual_check_still_works(
    review_bundle,
):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    async with sessions() as db, db.begin():
        submission = await db.get(Submission, seed.submission_id)
        submission.external_receipt = {"moodle_review": {
            "grade": "0", "comment": "Проверено преподавателем в Moodle",
        }}
    provider = ReviewProvider()
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0
    async with sessions() as db:
        assert (await db.scalar(select(AICodeReview))).state == "SKIPPED"
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        assert (await client.get(url)).json()["state"] == "NOT_STARTED"
        response = await client.post(url, json={}, headers=await _csrf(client))
        assert response.json()["state"] == "QUEUED"
        assert await process_next_ai_code_review(sessions, settings, provider)
        report = (await client.get(url)).json()
        assert Decimal(report["recommended_grade"]) == Decimal("7.5")
        assert report["recommended_comment"] == "Исправьте обработку пустого ввода."
    assert provider.calls == 1
    async with sessions() as db:
        submission = await db.get(Submission, seed.submission_id)
        assert submission.external_receipt["moodle_review"]["grade"] == "0"
        assert await db.scalar(select(func.count(ReviewDecision.id))) == 2


@pytest.mark.parametrize("grade", [-1, 10.01, "NaN", True, 1.234])
def test_ai_recommended_mark_must_fit_the_exact_question_scale(grade):
    with pytest.raises(ValueError):
        _validated_code_review(json.dumps({
            "summary": "Обзор", "findings": [], "recommended_grade": grade,
            "recommended_comment": "Комментарий",
        }), {"files": [], "grading": {"max_score": "10"}})


def test_ai_zero_mark_is_valid_but_requires_explanation():
    content = {
        "summary": "Обзор", "findings": [], "recommended_grade": 0,
        "recommended_comment": "Решение не соответствует условию.",
    }
    context = {"files": [], "grading": {"max_score": "3"}}
    assert _validated_code_review(json.dumps(content), context).recommended_grade == Decimal(0)
    with pytest.raises(ValueError):
        _validated_code_review(json.dumps({**content, "recommended_comment": ""}), context)


@pytest.mark.asyncio
async def test_moodle_feedback_arriving_during_ai_analysis_remains_authoritative(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)

    async def import_feedback():
        async with sessions() as db, db.begin():
            decision = await db.scalar(
                select(ReviewDecision).order_by(ReviewDecision.revision.desc())
            )
            decision.status = "APPLIED"
            decision.lms_export_state = "IMPORTED"
            decision.grade = Decimal("4")
            decision.comment = "Официальная оценка Moodle"

    assert await process_next_ai_code_review(
        sessions, settings, ReviewProvider(callback=import_feedback),
    )
    async with sessions() as db:
        report = await db.scalar(select(AICodeReview))
        assert report.recommended_grade == Decimal("7.5")
        decision = await db.scalar(select(ReviewDecision).where(ReviewDecision.status == "APPLIED"))
        assert decision.grade == Decimal("4")
        assert decision.comment == "Официальная оценка Moodle"
        assert decision.lms_export_state == "IMPORTED"


@pytest.mark.asyncio
async def test_automatic_ai_review_policy_is_administrator_only(review_bundle):
    app, sessions, settings, seed, holder = review_bundle
    provider = ReviewProvider()
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    policy_url = f"/api/v1/assessments/{seed.assessment_id}/ai-review-policy"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        # Reading an opt-out submission offers a one-off check, but never queues it.
        assert (await client.get(url)).json()["state"] == "NOT_STARTED"
        assert (
            await client.patch(policy_url, json={"enabled": True}, headers=headers)
        ).status_code == 403
        generic = await client.patch(
            f"/api/v1/assessments/{seed.assessment_id}",
            json={"ai_review_enabled": True},
            headers=headers,
        )
        assert generic.status_code == 422
        holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
        enabled = await client.patch(policy_url, json={"enabled": True}, headers=headers)
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["ai_review_enabled"] is True
        assert enabled.json()["student_ai_enabled"] is True
        assert enabled.json()["status"] == "PUBLISHED"
        # The seed is already approved: enabling must not re-analyze it automatically.
        assert (await client.get(url)).json()["state"] == "NOT_STARTED"
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_enabling_backfills_only_ungraded_once_and_worker_caches(review_bundle):
    app, sessions, settings, seed, holder = review_bundle
    async with sessions() as db, db.begin():
        await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
    holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        for _ in range(2):
            response = await client.patch(
                f"/api/v1/assessments/{seed.assessment_id}/ai-review-policy",
                json={"enabled": True},
                headers=headers,
            )
            assert response.status_code == 200, response.text
        assert (await client.get(url)).json()["state"] == "QUEUED"
        provider = ReviewProvider()
        assert await process_next_ai_code_review(sessions, settings, provider)
        completed = (await client.get(url)).json()
        assert completed["state"] == "COMPLETED"
        assert Decimal(completed["recommended_grade"]) == Decimal("7.5")
        assert completed["recommended_comment"] == "Исправьте обработку пустого ввода."
        assert completed["findings"][0]["path"] == "main.cpp"
        assert completed["model"] == "test-model"
        assert (await client.post(url, json={}, headers=headers)).json() == completed
        assert not await process_next_ai_code_review(sessions, settings, provider)
        assert provider.calls == 1
        assert provider.context["task"]["statement"] == "Return zero for the fixture"
        assert Decimal(provider.context["grading"]["max_score"]) == Decimal("10")
        assert provider.context["files"][0]["content"] == "int main() { return 0; }\n"
        assert "teacher_comment" not in provider.context
    async with sessions() as db:
        assert await db.scalar(select(func.count(AICodeReview.id))) == 1
        assert await db.scalar(select(func.count(ChatMessage.id))) == 0
        assert await db.scalar(select(func.count(ReviewDecision.id))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("line,path", [(99, "main.cpp"), (1, "foreign.cpp")])
async def test_invalid_findings_fail_safely_and_only_explicit_retry_requeues(
    review_bundle, line, path
):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    provider = ReviewProvider(line=line, path=path)
    for _ in range(3):
        await _make_retry_due(sessions)
        assert await process_next_ai_code_review(sessions, settings, provider)
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        failed = (await client.get(url)).json()
        assert failed["state"] == "FAILED"
        assert failed["findings"] == []
        assert failed["attempt_count"] == failed["max_attempts"] == 3
        assert provider.calls == 3
        assert "некорректный" in failed["error"]
        assert (await client.post(url, json={}, headers=headers)).json()["state"] == "FAILED"
        assert not await process_next_ai_code_review(sessions, settings, provider)
        response = await client.post(url, json={"retry": True}, headers=headers)
        assert response.json()["state"] == "QUEUED"
        assert await process_next_ai_code_review(sessions, settings, ReviewProvider())
        assert (await client.get(url)).json()["state"] == "COMPLETED"


@pytest.mark.asyncio
async def test_global_disable_preserves_queue_and_policy_disable_prevents_provider(review_bundle):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    async with sessions() as db, db.begin():
        db.add(
            SystemSetting(
                key="effective",
                value={"ai_enabled": False},
                updated_by_id=seed.admin_id,
            )
        )
    provider = ReviewProvider()
    assert not await process_next_ai_code_review(sessions, settings, provider)
    async with sessions() as db, db.begin():
        assert (await db.scalar(select(AICodeReview))).state == "QUEUED"
        setting = await db.scalar(select(SystemSetting))
        setting.value = {"ai_enabled": True}
        assessment = await db.get(Assessment, seed.assessment_id)
        assessment.ai_review_enabled = False
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_disabling_during_provider_call_discards_findings(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)

    async def disable():
        async with sessions() as db, db.begin():
            assessment = await db.get(Assessment, seed.assessment_id)
            assessment.ai_review_enabled = False

    assert await process_next_ai_code_review(sessions, settings, ReviewProvider(callback=disable))
    async with sessions() as db:
        report = await db.scalar(select(AICodeReview))
        assert report.state == "FAILED"
        assert report.findings == []
        assert report.summary == ""


@pytest.mark.asyncio
async def test_running_job_cannot_be_processed_twice_and_stale_can_retry(review_bundle):
    app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)

    async def concurrent_worker():
        assert not await process_next_ai_code_review(sessions, settings, ReviewProvider())

    provider = ReviewProvider(callback=concurrent_worker)
    assert await process_next_ai_code_review(sessions, settings, provider)
    async with sessions() as db, db.begin():
        report = await db.scalar(select(AICodeReview))
        report.state = "RUNNING"
        report.updated_at = utcnow() - timedelta(hours=1)
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get(url)).json()["state"] == "QUEUED"
        headers = await _csrf(client)
        assert (await client.post(url, json={"retry": True}, headers=headers)).json()[
            "state"
        ] == "QUEUED"
        # Polling/manual clicks do not reset a stale job's paid-call budget.
        assert not await process_next_ai_code_review(sessions, settings, provider)
        await _make_retry_due(sessions)
        assert await process_next_ai_code_review(sessions, settings, provider)
        assert (await client.get(url)).json()["attempt_count"] == 2


@pytest.mark.asyncio
async def test_report_access_stays_scoped_to_student_group_and_admin(review_bundle):
    app, sessions, settings, seed, holder = review_bundle
    await _enable(sessions, seed)
    assert await process_next_ai_code_review(sessions, settings, ReviewProvider())
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        for principal, role in [(seed.student_id, "STUDENT"), (seed.other_teacher_id, "TEACHER")]:
            holder["auth"] = _context(principal, roles=(role,))
            assert (await client.get(url)).status_code == 403
            assert (await client.post(url, json={}, headers=headers)).status_code == 403
        holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
        assert (await client.get(url)).json()["state"] == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [True, False])
@pytest.mark.parametrize("enabled", [None, True, False])
async def test_child_question_uses_current_parent_policy(review_bundle, runtime, enabled):
    _app, sessions, _settings, seed, _ = review_bundle
    async with sessions() as db, db.begin():
        await db.execute(update(ReviewDecision).values(status="SUPERSEDED"))
        child = await db.get(Assessment, seed.assessment_id)
        parent = Assessment(
            course_id=seed.course_id,
            title="Parent Quiz",
            created_by_id=seed.teacher_id,
        )
        if enabled is not None:
            parent.ai_review_enabled = enabled
        db.add(parent)
        await db.flush()
        assert parent.ai_review_enabled is (enabled is not False)
        # Child-local flags can be stale after import or parent policy changes.
        # Only the currently visible parent activity controls automatic jobs.
        child.ai_review_enabled = not parent.ai_review_enabled
        child.policy = {
            "moodle_quiz_question_split": True,
            "moodle_parent_assessment_id": str(parent.id),
        }
        attempt = await db.get(Attempt, seed.attempt_id)
        if runtime:
            root = Attempt(
                assessment_id=parent.id,
                principal_id=seed.student_id,
                assigned_task_version_id=attempt.assigned_task_version_id,
                state="SUBMITTED",
            )
            db.add(root)
            await db.flush()
            db.add(
                MoodleQuizQuestion(
                    root_attempt_id=root.id,
                    attempt_id=attempt.id,
                    question_slot="1",
                    position=1,
                    title="Question 1",
                    question_max_mark=10,
                )
            )
            child.policy = {}
            await db.flush()
        policy = await ai_review_policy_assessment(db, child, attempt)
        assert policy.id == parent.id
        submission = await db.get(Submission, seed.submission_id)
        report = await enqueue_ai_code_review(db, submission)
        assert (report is not None) is (enabled is not False)
        if report is not None:
            assert report.manual_requested is False
            assert report.approved_decision_id is None


@pytest.mark.asyncio
async def test_mock_review_is_explicitly_not_a_real_analysis(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    assert await process_next_ai_code_review(sessions, settings)
    async with sessions() as db:
        report = await db.scalar(select(AICodeReview))
        assert report.state == "COMPLETED"
        assert report.findings == []
        assert report.model == "mock-code-review-v1"
        assert "анализ кода не выполнялся" in report.summary


async def _replace_snapshot(sessions, seed):
    async with sessions() as db, db.begin():
        submission = await db.get(Submission, seed.submission_id)
        old = await db.get(Snapshot, submission.snapshot_id)
        new = Snapshot(
            workspace_id=old.workspace_id,
            revision=old.revision + 1,
            manifest_hash="f" * 64,
            event_chain_head=old.event_chain_head,
            files=[{"path": "main.cpp", "content": "int main() { return 1; }\n"}],
            reason="SUBMISSION",
        )
        db.add(new)
        await db.flush()
        submission.snapshot_id = new.id
        await enqueue_ai_code_review(db, submission)


@pytest.mark.asyncio
async def test_replaced_snapshot_invalidates_older_queued_report(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)
    await _replace_snapshot(sessions, seed)
    provider = ReviewProvider()
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 1
    assert provider.context["files"][0]["content"] == "int main() { return 1; }\n"
    async with sessions() as db:
        reports = (await db.scalars(select(AICodeReview).order_by(AICodeReview.created_at))).all()
        assert [report.state for report in reports] == ["FAILED", "COMPLETED"]


@pytest.mark.asyncio
async def test_replaced_snapshot_during_analysis_discards_old_findings(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    await _enable(sessions, seed)

    async def replace():
        await _replace_snapshot(sessions, seed)

    assert await process_next_ai_code_review(sessions, settings, ReviewProvider(callback=replace))
    async with sessions() as db:
        old = await db.scalar(select(AICodeReview).order_by(AICodeReview.created_at))
        assert old.state == "FAILED"
        assert old.findings == []


@pytest.mark.asyncio
async def test_reenable_requeues_failed_ungraded_but_repeated_enable_does_not(review_bundle):
    app, sessions, settings, seed, holder = review_bundle
    await _enable(sessions, seed, ungraded=True)
    for _ in range(3):
        await _make_retry_due(sessions)
        assert await process_next_ai_code_review(sessions, settings, ReviewProvider(line=999))
    holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
    policy_url = f"/api/v1/assessments/{seed.assessment_id}/ai-review-policy"
    report_url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        await client.patch(policy_url, json={"enabled": True}, headers=headers)
        assert (await client.get(report_url)).json()["state"] == "FAILED"
        await client.patch(policy_url, json={"enabled": False}, headers=headers)
        await client.patch(policy_url, json={"enabled": True}, headers=headers)
        assert (await client.get(report_url)).json()["state"] == "QUEUED"


@pytest.mark.asyncio
async def test_advisory_opt_in_is_independent_of_disabled_chat_and_legacy_support(review_bundle):
    _app, sessions, settings, seed, _ = review_bundle
    async with sessions() as db, db.begin():
        assessment = await db.get(Assessment, seed.assessment_id)
        assessment.student_ai_enabled = False
        assessment.teacher_ai_enabled = False
        assessment.decision_support_enabled = False
    await _enable(sessions, seed)
    provider = ReviewProvider()
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 1
    async with sessions() as db:
        assessment = await db.get(Assessment, seed.assessment_id)
        assert not assessment.student_ai_enabled
        assert not assessment.teacher_ai_enabled
        assert not assessment.decision_support_enabled
        assert (await db.scalar(select(AICodeReview))).state == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize("admin", [False, True])
async def test_manual_check_without_auto_opt_in_runs_once_and_preserves_policy(
    review_bundle, admin
):
    app, sessions, settings, seed, holder = review_bundle
    if admin:
        holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        initial = (await client.get(url)).json()
        assert initial["enabled"] is True
        assert initial["state"] == "NOT_STARTED"
        for _ in range(2):
            queued = await client.post(url, json={}, headers=headers)
            assert queued.status_code == 200, queued.text
            assert queued.json()["state"] == "QUEUED"
        assert (await client.get(url)).json()["state"] == "QUEUED"

        async def while_running():
            assert (await client.get(url)).json()["state"] == "RUNNING"
            assert (await client.post(url, json={"retry": True}, headers=headers)).json()[
                "state"
            ] == "RUNNING"
            assert not await process_next_ai_code_review(sessions, settings, ReviewProvider())

        provider = ReviewProvider(callback=while_running)
        assert await process_next_ai_code_review(sessions, settings, provider)
        completed = (await client.get(url)).json()
        assert completed["state"] == "COMPLETED"
        assert completed["findings"][0]["path"] == "main.cpp"
        assert (await client.post(url, json={"retry": True}, headers=headers)).json() == completed
        assert not await process_next_ai_code_review(sessions, settings, provider)
        assert provider.calls == 1
        assert provider.context["task"]["statement"] == "Return zero for the fixture"
    async with sessions() as db:
        report = await db.scalar(select(AICodeReview))
        assert report.manual_requested is True
        assert report.requested_with_system_access is admin
        assert report.requested_by_id == (seed.admin_id if admin else seed.teacher_id)
        assert await db.scalar(select(func.count(AICodeReview.id))) == 1
        assert (await db.get(Assessment, seed.assessment_id)).ai_review_enabled is False
        assert await db.scalar(select(func.count(ChatMessage.id))) == 0
        assert await db.scalar(select(func.count(ReviewDecision.id))) == 2


@pytest.mark.asyncio
async def test_manual_failure_retries_three_times_with_auto_policy_still_off(review_bundle):
    app, sessions, settings, seed, _ = review_bundle
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        await client.post(url, json={}, headers=headers)
        for _ in range(3):
            await _make_retry_due(sessions)
            assert await process_next_ai_code_review(sessions, settings, ReviewProvider(line=999))
        assert (await client.get(url)).json()["state"] == "FAILED"
        assert (await client.post(url, json={}, headers=headers)).json()["state"] == "FAILED"
        assert not await process_next_ai_code_review(sessions, settings, ReviewProvider())
        assert (await client.post(url, json={"retry": True}, headers=headers)).json()[
            "state"
        ] == "QUEUED"
        assert await process_next_ai_code_review(sessions, settings, ReviewProvider())
        assert (await client.get(url)).json()["state"] == "COMPLETED"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["global_disabled", "unconfigured", "empty_source"])
async def test_manual_check_honors_global_availability_without_queuing(review_bundle, reason):
    app, sessions, settings, seed, _ = review_bundle
    async with sessions() as db, db.begin():
        if reason == "global_disabled":
            db.add(
                SystemSetting(
                    key="effective", value={"ai_enabled": False}, updated_by_id=seed.admin_id
                )
            )
        elif reason == "unconfigured":
            settings.ai_mock_enabled = False
        else:
            submission = await db.get(Submission, seed.submission_id)
            (await db.get(Snapshot, submission.snapshot_id)).files = []
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        assert (await client.get(url)).json()["state"] == "UNAVAILABLE"
        assert (await client.post(url, json={}, headers=headers)).json()["state"] == "UNAVAILABLE"
    async with sessions() as db:
        assert await db.scalar(select(func.count(AICodeReview.id))) == 0


@pytest.mark.asyncio
async def test_manual_check_requires_csrf_and_current_teacher_review_scope(review_bundle):
    app, sessions, _settings, seed, holder = review_bundle
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.post(url, json={})).status_code == 403
        headers = await _csrf(client)
        for principal in (
            seed.student_id,
            seed.other_teacher_id,
            seed.unassigned_course_teacher_id,
        ):
            holder["auth"] = _context(principal, roles=("TEACHER",))
            assert (await client.get(url)).status_code == 403
            assert (await client.post(url, json={}, headers=headers)).status_code == 403
    async with sessions() as db:
        assert await db.scalar(select(func.count(AICodeReview.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("during_call", [False, True])
async def test_manual_worker_rechecks_requesting_teachers_scope(review_bundle, during_call):
    app, sessions, settings, seed, _ = review_bundle
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post(url, json={}, headers=await _csrf(client))

    async def revoke():
        async with sessions() as db, db.begin():
            await db.execute(
                update(CourseMembership)
                .where(CourseMembership.principal_id == seed.teacher_id)
                .values(active=False)
            )

    if not during_call:
        await revoke()
    provider = ReviewProvider(callback=revoke if during_call else None)
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == int(during_call)
    async with sessions() as db:
        report = await db.scalar(select(AICodeReview))
        assert report.state == "FAILED"
        assert report.findings == []


@pytest.mark.asyncio
async def test_manual_check_is_bound_to_one_snapshot_and_never_queues_a_replacement(review_bundle):
    app, sessions, settings, seed, _ = review_bundle
    url = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.post(url, json={}, headers=await _csrf(client))
        await _replace_snapshot(sessions, seed)
        assert (await client.get(url)).json()["state"] == "NOT_STARTED"
    provider = ReviewProvider()
    assert await process_next_ai_code_review(sessions, settings, provider)
    assert not await process_next_ai_code_review(sessions, settings, provider)
    assert provider.calls == 0
    async with sessions() as db:
        assert await db.scalar(select(func.count(AICodeReview.id))) == 1
        assert (await db.scalar(select(AICodeReview))).state == "FAILED"
