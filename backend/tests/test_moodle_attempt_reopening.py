from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import select
from starlette.requests import Request

from app.api.attempts import _prepare_moodle_quiz_attempt, create_attempt
from app.core.credential_crypto import BROWSER_STATE_CREDENTIAL_KIND, encrypt_moodle_browser_state
from app.integrations.errors import (
    IntegrationAssessmentUnavailable,
    IntegrationAttemptFinalized,
    IntegrationBusy,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from app.integrations.moodle_browser import MoodleBrowserClient
from app.models.attempts import Attempt
from app.models.courses import Course
from app.models.identity import MoodleCredential
from app.models.integration import SyncOutbox
from app.models.tasks import AvailabilityRule
from app.schemas.attempts import AttemptStartRequest
from app.services.common import DomainError
from app.services.moodle_attempt_reconciliation import reconcile_deleted_quiz_attempts
from app.services.workspace import (
    create_snapshot,
    enqueue_checkpoint,
    replace_file_content,
    start_attempt,
    submit_attempt,
)
from tests.test_attempt_review_services import (
    _multi_quiz_preparation,
    _start_multi_quiz,
    _workspace_and_files,
)
from tests.test_student_work_continuation import auth


@pytest.mark.parametrize(
    "failure", ["terminal", "deleted", "no_more", "busy", "unavailable", "protocol"],
)
async def test_reopen_reconciles_only_proven_terminal_attempt_without_losing_old_code(
    db,
    app_bundle,
    monkeypatch,
    failure,
):
    settings = app_bundle[2]
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    student, _, assessment, old, members = await _start_multi_quiz(db)
    old_id = old.id
    old_ids = [member.attempt_id for member in members]
    workspace, files = await _workspace_and_files(db, old)
    await replace_file_content(
        db,
        attempt_id=old.id,
        principal_id=student.id,
        file_id=files[0].id,
        content="// keep my old answer",
        expected_revision=0,
        client_request_id="old-answer",
    )
    snapshot = await create_snapshot(db, workspace, "PERIODIC")
    checkpoint = await enqueue_checkpoint(db, attempt=old, snapshot=snapshot, reason="PERIODIC")
    checkpoint_id = checkpoint.id
    state = {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "student-session",
                "domain": "lms.services.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    credential = MoodleCredential(
        connection_id=student.connection_id,
        principal_id=student.id,
        kind=BROWSER_STATE_CREDENTIAL_KIND,
        status="ACTIVE",
        encrypted_secret=encrypt_moodle_browser_state(
            state,
            settings,
            connection_id=student.connection_id,
            principal_id=student.id,
        ),
    )
    db.add(credential)
    await db.commit()
    if failure == "deleted":
        assessment.policy = {**assessment.policy, "moodle_metadata_read_only": True}
        db.add(AvailabilityRule(
            assessment_id=assessment.id, target_type="GROUP", target_external_id="reviewers",
            allowed=True, authored_by_id=assessment.created_by_id,
        ))
        await db.flush()
        await submit_attempt(
            db, attempt_id=old.id, principal_id=student.id,
            expected_revision=workspace.current_revision,
        )
        await reconcile_deleted_quiz_attempts(
            db, course=await db.get(Course, assessment.course_id), assessment=assessment,
            cmid=30354, actor_external_subject="teacher",
            known_attempt_ids=["141716"], deleted_attempt_ids=["141716"],
        )
        await db.commit()
    calls = []

    async def prepare(_browser, course_id, cmid, **expected):
        calls.append(expected)
        assert course_id == "549" and cmid == 30354
        if len(calls) == 1 and failure != "deleted":
            assert expected == {"expected_attempt_id": "141716", "expected_question_slot": "1"}
            error = {
                "terminal": IntegrationAttemptFinalized,
                "no_more": IntegrationAttemptFinalized,
                "busy": IntegrationBusy,
                "unavailable": IntegrationUnavailable,
                "protocol": IntegrationProtocolError,
            }[failure]
            raise error("Moodle response")
        assert not expected  # New attempt gets its OWN id and question versions.
        if failure == "no_more":
            raise IntegrationAssessmentUnavailable("No more attempts")
        fresh = _multi_quiz_preparation(remote_attempt_id="141717")
        return SimpleNamespace(
            storage_state=state,
            preparation=SimpleNamespace(
                course_id="549",
                cmid=30354,
                attempt_id="141717",
                question_slot=fresh.question_slot,
                question_text=fresh.question_text,
                answer_transport=fresh.answer_transport,
                available_answer_transports=fresh.available_answer_transports,
                remaining_seconds=1200,
                question_max_mark=3,
                questions=fresh.questions,
            ),
        )

    monkeypatch.setattr(MoodleBrowserClient, "prepare_quiz_essay", prepare)
    if failure == "deleted":
        # Run the complete admission endpoint; mock only the external browser.
        # A service-only start would skip the broken activity-mapping resolver.
        result = await create_attempt(
            assessment.id, AttemptStartRequest(),
            Request({"type": "http", "app": app_bundle[0], "headers": [],
                     "client": ("127.0.0.1", 123)}),
            auth(student, "STUDENT"), db,
        )
        fresh = await db.get(Attempt, result.id)
        assert fresh.id != old_id and fresh.integrity_policy["moodle_attempt_id"] == "141717"
        _, fresh_files = await _workspace_and_files(db, fresh)
        assert fresh_files[0].content == ""
    elif failure == "terminal":
        prepared = await _prepare_moodle_quiz_attempt(
            db,
            settings,
            assessment_id=assessment.id,
            principal_id=student.id,
        )
        fresh = await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
            prepared_moodle_quiz=prepared,
        )
        assert fresh.id != old_id and fresh.integrity_policy["moodle_attempt_id"] == "141717"
        _, fresh_files = await _workspace_and_files(db, fresh)
        assert fresh_files[0].content == ""
        await db.commit()
    else:
        with pytest.raises(DomainError):
            await _prepare_moodle_quiz_attempt(
                db,
                settings,
                assessment_id=assessment.id,
                principal_id=student.id,
            )
    terminal = failure in {"terminal", "no_more", "deleted"}
    for member_id in old_ids:
        member = await db.get(Attempt, member_id, populate_existing=True)
        expected_state = "VOID" if failure == "deleted" else "LOCKED" if terminal else "ACTIVE"
        assert member.state == expected_state
        assert member.integrity_policy["moodle_attempt_id"] == "141716"
    old = await db.get(Attempt, old_id)
    _, retained = await _workspace_and_files(db, old)
    assert retained[0].content == "// keep my old answer"
    event = await db.get(SyncOutbox, checkpoint_id, populate_existing=True)
    assert event.state == ("BLOCKED" if terminal else "PENDING")
    assert event.delivered_at is None
    credential = await db.scalar(select(MoodleCredential).execution_options(populate_existing=True))
    assert credential.lease_owner is None
    assert len(calls) == (2 if failure in {"terminal", "no_more"} else 1)
