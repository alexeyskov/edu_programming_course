from __future__ import annotations

import io
import json
import uuid
from dataclasses import replace
from datetime import timedelta

import py7zr
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.courses import course_assessment_sync_status, dismiss_assessment_sync_warnings
from app.api.reviews import get_submission
from app.auth.context import require_auth
from app.db.base import utcnow
from app.models.attempts import Submission
from app.models.courses import Course, CourseMembership, CourseMembershipGroup
from app.models.identity import ExternalPrincipal, TeacherAccessToken, TeacherTokenGrant
from app.models.integration import HistoryWarningDismissal, SyncOutbox
from app.models.tasks import Assessment
from app.schemas.courses import AssessmentSyncRead, HistoryWarningsDismissRequest
from app.services.manual_sync import queue_manual_assessment_sync
from app.services.moodle_history import (
    enqueue_historical_answer_reads,
    materialize_historical_submissions,
)
from app.services.moodle_history_warnings import _moodle_url
from tests.test_history_inventory import ref, seed_roster
from tests.test_manual_sync import auth
from tests.test_moodle_history import _encoded_artifact, _multi_essay_item, _seed_history_target


async def seed_warning(sessions, settings, *, imported=True):
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        await seed_roster(db, ids)
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        root = await db.scalar(select(SyncOutbox))
        root.state = "DELIVERED"
        await enqueue_historical_answer_reads(
            db, course=course, assessment=assessment, actor_external_subject="42",
            module="quiz", cmid=777, candidates=[ref()], scan_id=root.id,
            manual_run_id=root.payload["manual_run_id"],
        )
        detail = await db.scalar(select(SyncOutbox).where(SyncOutbox.id != root.id))
        detail.state = "DELIVERED" if imported else "FAILED"
        detail.receipt = {"warning_count": 1, "warning_codes": ["ARCHIVE_SOURCE_OMITTED"]}
        if not imported:
            detail.last_error = "TIMEOUT: private Moodle body and session cookie"
        else:
            archive = io.BytesIO()
            with py7zr.SevenZipFile(archive, "w") as zipped:
                zipped.writestr(b"Microsoft Visual Studio Solution File", "solution.sln")
            item = {
                **_multi_essay_item(), "user_id": "77", "display_name": "Иван Иванов",
                "attempt_id": "9001", "external_id": "quiz:777:9001",
            }
            item["responses"][1]["artifacts"] = [
                _encoded_artifact("answer.7z", archive.getvalue()),
            ]
            await materialize_historical_submissions(
                db, course=course, assessment=assessment, actor_external_subject="42", items=[item],
            )
        ids.update(root_id=root.id, detail_id=detail.id)
    return ids


async def status_for(db, ids, context=None):
    result = await course_assessment_sync_status(
        ids["course_id"], context or auth(ids["teacher_id"]), db, include_warnings=True,
    )
    return next(item for item in result if item["assessment_id"] == ids["assessment_id"])


async def dismiss(db, ids, warnings, context=None):
    return await dismiss_assessment_sync_warnings(
        ids["assessment_id"],
        HistoryWarningsDismissRequest(warning_ids=[item["id"] for item in warnings]),
        context or auth(ids["teacher_id"]), db,
    )


async def test_details_identify_student_and_only_the_incomplete_quiz_question(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    async with sessions() as db:
        status = await status_for(db, ids)
        assert status["status"] == "PARTIAL"
        assert not status["warnings_dismissed"]
        assert len(status["warnings"]) == 1
        warning = status["warnings"][0]
        assert warning["student_name"] == "Иван Иванов"
        assert warning["attempt_id"] == "9001"
        assert warning["response_label"] == "Задание 2"
        assert "нет поддерживаемых исходников" in warning["message"]
        assert warning["moodle_url"] == "https://moodle.example.test/mod/quiz/review.php?attempt=9001&cmid=777"
        submission = await db.get(Submission, warning["submission_id"])
        assert submission.external_receipt["moodle_response_id"] == "12"
        assert submission.external_receipt["source_complete"] is False
        assert await db.scalar(select(func.count(Submission.id))) == 2
        # The actual HTTP schema retains details, but never arbitrary receipts.
        public = AssessmentSyncRead.model_validate(status).model_dump_json()
        assert "Иван Иванов" in public and "solution.sln" not in public


async def test_review_warning_is_scoped_to_the_exact_submission_and_clears_on_recovery(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    async with sessions() as db:
        warning = (await status_for(db, ids))["warnings"][0]
        broken = await db.get(Submission, warning["submission_id"])
        reviewed = await get_submission(broken.id, auth(ids["teacher_id"]), db)
        assert reviewed.source_warnings
        assert "нет поддерживаемых исходников" in reviewed.source_warnings[0].message
        assert reviewed.source_warnings[0].moodle_url == warning["moodle_url"]
        assert "solution.sln" not in reviewed.model_dump_json()
        other = await db.scalar(select(Submission).where(Submission.id != broken.id))
        assert not (await get_submission(other.id, auth(ids["teacher_id"]), db)).source_warnings
        # Warning comes from the answer, not the latest job or its acknowledgement.
        await dismiss(db, ids, [warning])
        assert (await get_submission(broken.id, auth(ids["teacher_id"]), db)).source_warnings
        broken.external_receipt = {
            **broken.external_receipt, "source_complete": True, "source_refresh_omissions": [],
        }
        await db.flush()
        assert not (await get_submission(broken.id, auth(ids["teacher_id"]), db)).source_warnings


async def test_failed_detail_has_name_and_moodle_link_before_any_submission_exists(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings, imported=False)
    async with sessions() as db:
        status = await status_for(db, ids)
        assert status["status"] == "FAILED"
        warning = status["warnings"][0]
        assert warning["student_name"] == "Student 77"
        assert warning["submission_id"] is None
        assert "attempt=9001" in warning["moodle_url"]
        assert warning["code"] == "TIMEOUT"
        assert "private" not in json.dumps(status, default=str)


async def test_acknowledgement_survives_reload_and_same_problem_in_new_manual_run(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    async with sessions() as db:
        before = await status_for(db, ids)
        warnings = before["warnings"]
        expected = {"dismissed_warning_ids": [warnings[0]["id"]]}
        assert await dismiss(db, ids, warnings) == expected
        assert await dismiss(db, ids, warnings) == expected  # two tabs / retry
    async with sessions() as db, db.begin():
        after = await status_for(db, ids)
        assert after["status"] == "PARTIAL"  # hiding is NOT a successful sync
        assert after["warnings"] == [] and after["warnings_dismissed"]
        assert after["updated_at"] == before["updated_at"]
        assert await db.scalar(select(func.count(HistoryWarningDismissal.id))) == 1
        root = await db.get(SyncOutbox, ids["root_id"])
        detail = await db.get(SyncOutbox, ids["detail_id"])
        new_run = uuid.uuid4().hex
        for old in (root, detail):
            db.add(SyncOutbox(
                connection_id=old.connection_id, course_id=old.course_id,
                aggregate_id=old.aggregate_id, aggregate_type=old.aggregate_type,
                event_type=old.event_type, state=old.state, receipt=old.receipt,
                idempotency_key=uuid.uuid4().hex,
                payload={**old.payload, "manual_run_id": new_run},
                created_at=utcnow() + timedelta(seconds=1),
            ))
        await db.flush()
        again = await status_for(db, ids)
        assert again["warnings_dismissed"] and not again["warnings"]


async def test_new_problem_arriving_during_dismiss_is_not_acknowledged(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings, imported=False)
    async with sessions() as db:
        old = (await status_for(db, ids))["warnings"]
        detail = await db.get(SyncOutbox, ids["detail_id"])
        db.add(SyncOutbox(
            connection_id=detail.connection_id, course_id=detail.course_id,
            aggregate_id=detail.aggregate_id, aggregate_type=detail.aggregate_type,
            event_type=detail.event_type, state="FAILED", last_error="TIMEOUT: new failure",
            idempotency_key=uuid.uuid4().hex,
            payload={**detail.payload, "attempt_refs": [ref("9002", "78")]},
        ))
        await db.flush()
        await dismiss(db, ids, old)
        status = await status_for(db, ids)
        assert not status["warnings_dismissed"]
        assert len(status["warnings"]) == 1
        assert status["warnings"][0]["student_name"] == "Student 78"
        assert status["warnings"][0]["id"] != old[0]["id"]


async def test_changed_answer_reappears_but_changed_grade_revision_does_not(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    async with sessions() as db:
        old = (await status_for(db, ids))["warnings"]
        await dismiss(db, ids, old)
        submission = await db.get(Submission, old[0]["submission_id"])
        submission.external_receipt = {
            **submission.external_receipt, "external_revision": "new-grade",
        }
        await db.flush()
        assert (await status_for(db, ids))["warnings_dismissed"]
        submission.external_receipt = {
            **submission.external_receipt,
            "lms_response_observations": [{"kind": "FILE", "sha256": "b" * 64}],
        }
        await db.flush()
        changed = (await status_for(db, ids))["warnings"]
        assert len(changed) == 1 and changed[0]["id"] != old[0]["id"]


async def test_warning_names_and_links_obey_group_scope_even_if_payload_knows_name(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings, imported=False)
    async with sessions() as db:
        detail = await db.get(SyncOutbox, ids["detail_id"])
        detail.payload = {**detail.payload, "attempt_refs": [ref("9009", "79")]}
        await db.flush()
        normal = await status_for(db, ids)
        assert normal["warnings"] == []
        assert not normal["warnings_dismissed"]
        assert "Student 79" not in json.dumps(normal, default=str)
        system_auth = replace(auth(ids["teacher_id"]), capabilities=("SYSTEM_SETTINGS",))
        system = await status_for(db, ids, system_auth)
        system_warning = system["warnings"][0]
        assert system_warning["student_name"] == "Student 79"
        assert "attempt=9009" in system_warning["moodle_url"]
        # An ordinary teacher cannot acknowledge a detail they cannot read.
        assert await dismiss(db, ids, [system_warning]) == {"dismissed_warning_ids": []}


async def test_mixed_warning_batch_shows_only_own_students_but_admin_sees_everyone(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings, imported=False)
    async with sessions() as db:
        detail = await db.get(SyncOutbox, ids["detail_id"])
        detail.payload = {**detail.payload, "attempt_refs": [ref(), ref("9009", "79")]}
        await db.flush()
        normal = await status_for(db, ids)
        assert [item["student_name"] for item in normal["warnings"]] == ["Student 77"]
        system = await status_for(
            db, ids, replace(auth(ids["teacher_id"]), capabilities=("SYSTEM_SETTINGS",)),
        )
        assert {item["student_name"] for item in system["warnings"]} == {"Student 77", "Student 79"}


async def test_dismissal_is_personal_and_does_not_change_other_teachers_notifications(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    async with sessions() as db, db.begin():
        second = ExternalPrincipal(
            connection_id=ids["connection_id"], external_subject="43", display_name="Other teacher",
        )
        db.add(second)
        await db.flush()
        token = TeacherAccessToken(
            public_id=uuid.uuid4().hex[:16], label="Other", secret_hash="$argon2id$test",
            created_by_id=second.id,
        )
        membership = CourseMembership(
            course_id=ids["course_id"], principal_id=second.id, role="TEACHER",
        )
        db.add_all([token, membership])
        await db.flush()
        first_membership = await db.scalar(select(CourseMembership).where(
            CourseMembership.principal_id == ids["teacher_id"],
        ))
        group_id = await db.scalar(select(CourseMembershipGroup.coursegroup_id).where(
            CourseMembershipGroup.coursemembership_id == first_membership.id,
        ))
        db.add_all([
            TeacherTokenGrant(token_id=token.id, principal_id=second.id),
            CourseMembershipGroup(coursemembership_id=membership.id, coursegroup_id=group_id),
        ])
        second_id = second.id
    async with sessions() as db:
        warnings = (await status_for(db, ids))["warnings"]
        await dismiss(db, ids, warnings)
        second_status = await status_for(db, ids, auth(second_id))
        assert second_status["warnings"] == warnings
        assert not second_status["warnings_dismissed"]


async def test_student_cannot_read_or_dismiss_teacher_warnings(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    async with sessions() as db:
        warnings = (await status_for(db, ids))["warnings"]
        student = await db.scalar(select(ExternalPrincipal).where(
            ExternalPrincipal.external_subject == "77",
        ))
        for operation in (lambda: status_for(db, ids, auth(student.id)),
                          lambda: dismiss(db, ids, warnings, auth(student.id))):
            with pytest.raises(HTTPException) as error:
                await operation()
            assert error.value.status_code == 403


async def test_inventory_problem_without_known_students_is_honest_and_dismissible(app_bundle):
    _, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings, imported=False)
    async with sessions() as db:
        detail = await db.get(SyncOutbox, ids["detail_id"])
        detail.state, detail.receipt = "DELIVERED", {}
        root = await db.get(SyncOutbox, ids["root_id"])
        root.state, root.last_error = "FAILED", "INVALID_RESPONSE: QUIZ_TABLE_NOT_FOUND: secret"
        await db.flush()
        warning = (await status_for(db, ids))["warnings"][0]
        assert warning["code"] == "SUBMISSIONS_TABLE_NOT_FOUND"
        assert warning["student_name"] is warning["moodle_url"] is None
        await dismiss(db, ids, [warning])
        assert (await status_for(db, ids))["warnings_dismissed"]


def test_moodle_warning_links_are_constructed_not_taken_from_untrusted_urls():
    assert _moodle_url("https://moodle.test/sub/?token=secret", "assign", "1", "", "2") == (
        "https://moodle.test/sub/mod/assign/view.php?id=1&action=grader&userid=2"
    )
    assert _moodle_url("javascript:alert(1)", "quiz", "1", "2", "3") is None
    assert _moodle_url("https://user:secret@moodle.test", "quiz", "1", "2", "3") is None
    assert _moodle_url("https://moodle.test", "quiz", "1", "2&token=secret", "3") is None


async def test_warning_http_roundtrip_requires_csrf_and_validates_the_requested_ids(app_bundle):
    app, sessions, settings = app_bundle
    ids = await seed_warning(sessions, settings)
    app.dependency_overrides[require_auth] = lambda: auth(ids["teacher_id"])
    url = f"/api/v1/courses/{ids['course_id']}/assessment-sync-status?include_warnings=true"
    mutation = f"/api/v1/assessments/{ids['assessment_id']}/sync-warnings/dismiss"
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            response = await client.get(url)
            assert response.status_code == 200, response.text
            status = next(item for item in response.json()
                          if item["assessment_id"] == str(ids["assessment_id"]))
            warning_ids = [item["id"] for item in status["warnings"]]
            assert warning_ids
            denied = await client.post(mutation, json={"warning_ids": warning_ids})
            assert denied.status_code == 403
            csrf = await client.get("/api/v1/auth/csrf")
            headers = {"X-CSRFToken": csrf.json()["csrf_token"]}
            for invalid in ([], ["not-a-warning"], ["a" * 64] * 1001):
                response = await client.post(
                    mutation, headers=headers, json={"warning_ids": invalid},
                )
                assert response.status_code == 422
            response = await client.post(
                mutation, headers=headers, json={"warning_ids": warning_ids},
            )
            assert response.status_code == 200 and response.json() == {
                "dismissed_warning_ids": warning_ids,
            }
            # A new HTTP read gets the durable acknowledgement, without local storage.
            status = next(item for item in (await client.get(url)).json()
                          if item["assessment_id"] == str(ids["assessment_id"]))
            assert status["warnings_dismissed"] and status["warnings"] == []
            assert status["status"] == "PARTIAL"
    finally:
        app.dependency_overrides.pop(require_auth, None)
