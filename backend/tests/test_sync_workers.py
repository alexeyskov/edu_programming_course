from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from app.api.attempts import _attempt_read, _checkpoint_state
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    decrypt_moodle_browser_state,
    encrypt_moodle_browser_state,
    encrypt_moodle_credential,
)
from app.integrations.errors import (
    IntegrationAttemptFinalized,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from app.integrations.moodle import CourseDiscovery
from app.integrations.moodle_browser import (
    MoodleBrowserAssignmentReceipt,
    MoodleBrowserAssignmentResult,
    MoodleBrowserClient,
)
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.models.attempts import Attempt, Submission, Workspace, WorkspaceFile
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.enums import AttemptState, AvailabilityTarget, CourseRole, SyncOutboxState
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    MoodleCredential,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import ExternalMapping, LMSSubmissionFingerprint, SyncOutbox
from app.models.review import ReviewDecision
from app.models.tasks import Assessment, AvailabilityRule, TaskBankItem, TaskVersion
from app.services.common import DomainError, canonical_hash, canonical_json, sha256_text
from app.services.course_sync_state import course_sync_stale_before
from app.services.sync import (
    ClaimedOutboxEvent,
    ConnectionTarget,
    _apply_activity_deadlines,
    _apply_memberships,
    _BlockedDelivery,
    _MoodleBrowserAdapter,
    _prepare_checkpoint,
    _prepare_grade,
    _previous_managed_artifact_from_receipts,
    checkpoint_interval_seconds,
    claim_next_outbox_event,
    process_outbox_once,
    recover_attempt_checkpoint,
    run_scheduler_iteration,
    validate_checkpoint_manifest,
)
from app.services.workspace import (
    create_snapshot,
    enqueue_checkpoint,
    ensure_attempt_not_finalized_in_moodle,
    refresh_workspace_hash,
    retry_submission_checkpoint,
)


@pytest.mark.asyncio
async def test_course_sync_removes_legacy_principal_override_rules(db) -> None:
    connection = LMSConnection(
        name="Moodle",
        provider="MOODLE",
        base_url="https://moodle.example.test",
    )
    db.add(connection)
    await db.flush()
    teacher = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="9001",
        display_name="Teacher",
    )
    course = Course(
        connection_id=connection.id,
        external_id="549",
        title="C++",
        external_revision="revision-2",
    )
    db.add_all([teacher, course])
    await db.flush()
    assessment = Assessment(
        course_id=course.id,
        title="Самостоятельная работа №1",
        opens_at=datetime(2026, 9, 1, tzinfo=UTC),
        closes_at=datetime(2026, 9, 10, tzinfo=UTC),
        duration_seconds=1_800,
        attempt_limit=1,
        status="PUBLISHED",
        created_by_id=teacher.id,
    )
    db.add(assessment)
    await db.flush()
    mapping = ExternalMapping(
        connection_id=connection.id,
        local_type="Assessment",
        local_id=assessment.id,
        external_type="mod_quiz",
        external_id="30354",
        metadata_json={
            "managed_by": "MOODLE_ACTIVITY_IMPORT",
            "module": "quiz",
            "cmid": 30354,
            "sync_deadlines": True,
        },
    )
    principal_rule = AvailabilityRule(
        assessment_id=assessment.id,
        target_type=AvailabilityTarget.PRINCIPAL.value,
        target_external_id="104684",
        allowed=True,
        closes_at=datetime(2026, 9, 11, tzinfo=UTC),
        duration_seconds=1_800,
        attempt_limit=1,
        authored_by_id=teacher.id,
    )
    group_rule = AvailabilityRule(
        assessment_id=assessment.id,
        target_type=AvailabilityTarget.GROUP.value,
        target_external_id="group-24",
        allowed=True,
        authored_by_id=teacher.id,
    )
    db.add_all([mapping, principal_rule, group_rule])
    await db.flush()
    override_close = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    activity = {
        "cmid": 30354,
        "module": "quiz",
        "name": "Самостоятельная работа №1",
        "opens_at_epoch": int(datetime(2026, 9, 1, tzinfo=UTC).timestamp()),
        "due_at_epoch": int(datetime(2026, 9, 10, tzinfo=UTC).timestamp()),
        "cutoff_at_epoch": 0,
        "title_confirmed": True,
        "settings_confirmed": True,
        "statement_confirmed": True,
        "schedule_confirmed": True,
        "duration_confirmed": True,
        "grade_confirmed": True,
        "attempt_policy_confirmed": True,
        "import_supported": True,
        "answer_transport": "ESSAY_ONLINE_TEXT",
        "user_overrides_confirmed": True,
        "user_overrides": [
            {
                "override_id": 8123,
                "user_id": "104684",
                "display_name": "Test User",
                "confirmed": True,
                "opens_at_epoch": 0,
                "due_at_epoch": int(override_close.timestamp()),
                "cutoff_at_epoch": 0,
                "opens_at_overridden": False,
                "due_at_overridden": True,
                "cutoff_at_overridden": False,
                "duration_seconds": 2_400,
                "duration_overridden": True,
                "attempt_limit": 2,
                "attempt_limit_overridden": True,
                "attempt_limit_unlimited": False,
            },
            {
                "override_id": 8124,
                "user_id": "104685",
                "display_name": "Unpublished User",
                "confirmed": True,
                "opens_at_epoch": 0,
                "due_at_epoch": int(override_close.timestamp()),
                "cutoff_at_epoch": 0,
                "due_at_overridden": True,
                "attempt_limit": 5,
                "attempt_limit_overridden": True,
            },
        ],
    }

    await _apply_activity_deadlines(db, course, [activity])
    await db.flush()

    assert group_rule.allowed is True
    principal_rules = list(
        (
            await db.scalars(
                select(AvailabilityRule).where(
                    AvailabilityRule.assessment_id == assessment.id,
                    AvailabilityRule.target_type == AvailabilityTarget.PRINCIPAL.value,
                )
            )
        ).all()
    )
    assert principal_rules == []


@pytest.mark.asyncio
async def test_course_sync_removes_legacy_principal_rules_without_override_evidence(db) -> None:
    connection = LMSConnection(
        name="Moodle",
        provider="MOODLE",
        base_url="https://moodle.example.test",
    )
    db.add(connection)
    await db.flush()
    teacher = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="9001",
        display_name="Teacher",
    )
    course = Course(connection_id=connection.id, external_id="549", title="C++")
    db.add_all([teacher, course])
    await db.flush()
    assessment = Assessment(
        course_id=course.id,
        title="Самостоятельная работа №1",
        status="PUBLISHED",
        created_by_id=teacher.id,
    )
    db.add(assessment)
    await db.flush()
    db.add(
        ExternalMapping(
            connection_id=connection.id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type="mod_quiz",
            external_id="30354",
            metadata_json={"managed_by": "MOODLE_ACTIVITY_IMPORT", "module": "quiz"},
        )
    )
    rule = AvailabilityRule(
        assessment_id=assessment.id,
        target_type=AvailabilityTarget.PRINCIPAL.value,
        target_external_id="104684",
        allowed=True,
        closes_at=datetime(2026, 9, 30, tzinfo=UTC),
        duration_seconds=2_400,
        attempt_limit=2,
        authored_by_id=teacher.id,
    )
    db.add(rule)
    await db.flush()

    await _apply_activity_deadlines(
        db,
        course,
        [
            {
                "cmid": 30354,
                "module": "quiz",
                "user_overrides_confirmed": False,
                "user_overrides": [],
            }
        ],
    )
    await db.flush()

    remaining = await db.scalar(select(AvailabilityRule.id).where(AvailabilityRule.id == rule.id))
    assert remaining is None


@pytest.mark.asyncio
async def test_incomplete_roster_merges_known_group_links_without_deleting_old_ones(db) -> None:
    connection = LMSConnection(
        name="Large Moodle",
        provider="MOODLE",
        base_url="https://large-moodle.example.test",
    )
    db.add(connection)
    await db.flush()
    course = Course(
        connection_id=connection.id,
        external_id="549",
        title="C++",
        catalog_enabled=True,
    )
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="student-42",
        display_name="Student",
    )
    db.add_all([course, principal])
    await db.flush()
    membership = CourseMembership(
        course_id=course.id,
        principal_id=principal.id,
        role=CourseRole.STUDENT.value,
    )
    old_group = CourseGroup(course_id=course.id, external_id="old", name="Old group")
    db.add_all([membership, old_group])
    await db.flush()
    db.add(
        CourseMembershipGroup(
            coursemembership_id=membership.id,
            coursegroup_id=old_group.id,
        )
    )
    await db.flush()

    member = {
        "user_id": principal.external_subject,
        "display_name": principal.display_name,
        "role": "STUDENT",
        "groups": [
            {
                "external_id": "new",
                "name": "2.4 подгруппа Коваленко А.С.",
            }
        ],
    }
    await _apply_memberships(db, course, [member], "partial", complete=False)
    linked_ids = set(
        (
            await db.scalars(
                select(CourseMembershipGroup.coursegroup_id).where(
                    CourseMembershipGroup.coursemembership_id == membership.id
                )
            )
        ).all()
    )
    new_group = await db.scalar(
        select(CourseGroup).where(
            CourseGroup.course_id == course.id,
            CourseGroup.external_id == "new",
        )
    )
    assert new_group is not None
    assert linked_ids == {old_group.id, new_group.id}

    await _apply_memberships(db, course, [member], "complete", complete=True)
    linked_ids = set(
        (
            await db.scalars(
                select(CourseMembershipGroup.coursegroup_id).where(
                    CourseMembershipGroup.coursemembership_id == membership.id
                )
            )
        ).all()
    )
    assert linked_ids == {new_group.id}


def test_previous_managed_artifact_receipt_is_fully_scope_bound() -> None:
    digest = "a" * 64
    valid = {
        "status": "DRAFT_SAVED",
        "module": "assign",
        "answer_transport": "ASSIGN_FILE",
        "artifact_sha256": digest,
        "receipt": {
            "course_id": "549",
            "cmid": 777,
            "filename": "main.cpp",
            "sha256": digest,
        },
    }
    assert _previous_managed_artifact_from_receipts(
        [valid],
        module="assign",
        answer_transport="ASSIGN_FILE",
        course_id="549",
        cmid=777,
    ) == ("main.cpp", digest)

    for changed in (
        {**valid, "module": "quiz"},
        {**valid, "answer_transport": "ASSIGN_ONLINE_TEXT"},
        {**valid, "artifact_sha256": "b" * 64},
        {**valid, "receipt": {**valid["receipt"], "course_id": "550"}},
        {**valid, "receipt": {**valid["receipt"], "cmid": 778}},
    ):
        assert _previous_managed_artifact_from_receipts(
            [changed],
            module="assign",
            answer_transport="ASSIGN_FILE",
            course_id="549",
            cmid=777,
        ) == (None, None)


@pytest.mark.asyncio
async def test_browser_adapter_dispatches_assignment_without_drafts(
    app_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, settings = app_bundle
    state = {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "before",
                "domain": "moodle.example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }
    connection = ConnectionTarget(
        id=uuid.uuid4(),
        base_url="https://moodle.example.test",
        service_token=None,
        mode="PLUGINLESS",
        transport="PLAYWRIGHT",
        browser_state=state,
    )
    captured: dict[str, Any] = {}

    async def sync_assignment(
        _browser: MoodleBrowserClient,
        course_id: str,
        cmid: int,
        artifact,
        **kwargs: Any,
    ) -> MoodleBrowserAssignmentResult:
        captured.update(
            {
                "course_id": course_id,
                "cmid": cmid,
                "filename": artifact.filename,
                **kwargs,
            }
        )
        digest = hashlib.sha256(artifact.content).hexdigest()
        return MoodleBrowserAssignmentResult(
            status="FINALIZED",
            receipt=MoodleBrowserAssignmentReceipt(
                course_id=course_id,
                cmid=cmid,
                filename=artifact.filename,
                sha256=digest,
                size_bytes=len(artifact.content),
                idempotency_key=kwargs["idempotency_key"],
            ),
            storage_state=state,
        )

    monkeypatch.setattr(MoodleBrowserClient, "sync_assignment_submission", sync_assignment)
    content = b"int main() { return 0; }\n"
    digest = hashlib.sha256(content).hexdigest()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as client:
        adapter = _MoodleBrowserAdapter(settings, connection, client)
        result = await adapter.store_checkpoint(
            {
                "module": "assign",
                "course_id": 549,
                "cmid": 23461,
                "answer_transport": "ASSIGN_FILE",
                "artifact_filename": "main.cpp",
                "artifact_bytes": content,
                "artifact_sha256": digest,
                "artifact_size": len(content),
                "finalize": False,
                "requires_submission_statement": True,
                "submission_drafts": False,
                "max_submission_bytes_inherited": True,
                "previous_managed_filename": "main.cpp",
                "previous_managed_sha256": digest,
            },
            "assign:549:23461:42:v2",
        )

    assert captured["answer_transport"] == "ASSIGN_FILE"
    assert captured["submission_drafts"] is False
    assert captured["max_submission_bytes_inherited"] is True
    assert captured["previous_managed_sha256"] == digest
    assert result.value == {
        "status": "FINALIZED",
        "module": "assign",
        "answer_transport": "ASSIGN_FILE",
        "artifact_sha256": digest,
        "receipt": {
            "course_id": "549",
            "cmid": 23461,
            "filename": "main.cpp",
            "sha256": digest,
            "size_bytes": len(content),
            "idempotency_key": "assign:549:23461:42:v2",
        },
    }


async def _grant_teacher(db, principal: ExternalPrincipal) -> None:
    existing = await db.scalar(
        select(TeacherTokenGrant).where(TeacherTokenGrant.principal_id == principal.id)
    )
    if existing is not None:
        return
    token = TeacherAccessToken(
        public_id=principal.id.hex[:16],
        label=f"Test grant: {principal.display_name}",
        secret_hash="$argon2id$test-fixture-not-used-for-login",
        created_by_id=principal.id,
    )
    db.add(token)
    await db.flush()
    db.add(TeacherTokenGrant(token_id=token.id, principal_id=principal.id))
    await db.flush()


async def _seed_attempt(
    session_factory,
    *,
    now: datetime,
    deadline: datetime | None,
    duration_seconds: int | None = 600,
    revision: int = 4,
    auth_mode: str = "BRIDGE",
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as db, db.begin():
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={"auth_mode": auth_mode},
        )
        db.add(connection)
        await db.flush()
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Student",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add_all([principal, course])
        await db.flush()
        assessment = Assessment(
            course_id=course.id,
            title="Exam",
            duration_seconds=duration_seconds,
            created_by_id=principal.id,
            status="PUBLISHED",
        )
        db.add(assessment)
        await db.flush()
        attempt = Attempt(
            assessment_id=assessment.id,
            principal_id=principal.id,
            started_at=now - timedelta(seconds=duration_seconds // 2 if duration_seconds else 60),
            deadline_at=deadline,
            current_revision=revision,
        )
        db.add(attempt)
        await db.flush()
        workspace = Workspace(attempt_id=attempt.id, current_revision=revision)
        db.add(workspace)
        await db.flush()
        content = "int main() { return 0; }\n"
        db.add(
            WorkspaceFile(
                workspace_id=workspace.id,
                path="main.cpp",
                language="CPP",
                content=content,
                content_hash=sha256_text(content),
            )
        )
        await db.flush()
        await refresh_workspace_hash(db, workspace)
        return attempt.id, course.id


async def test_outbox_claim_prioritizes_student_checkpoint_over_course_sync(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        course_event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="course.sync",
            aggregate_type="Course",
            aggregate_id=course.id,
            idempotency_key=f"priority-course:{course.id}",
            payload={"course_id": course.external_id},
            next_attempt_at=now - timedelta(minutes=5),
            created_at=now - timedelta(minutes=5),
        )
        checkpoint_event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"priority-checkpoint:{attempt.id}",
            payload={"reason": "SUBMISSION"},
            next_attempt_at=now,
            created_at=now,
        )
        db.add_all([course_event, checkpoint_event])
        await db.flush()
        checkpoint_event_id = checkpoint_event.id

    claimed = await claim_next_outbox_event(session_factory, settings, now=now)

    assert claimed is not None
    assert claimed.id == checkpoint_event_id
    assert claimed.event_type == "attempt.checkpoint"


async def test_terminal_checkpoint_lane_never_claims_background_or_periodic_work(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        course_event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="course.sync",
            aggregate_type="Course",
            aggregate_id=course.id,
            idempotency_key=f"terminal-lane-course:{course.id}",
            payload={"course_id": course.external_id},
            next_attempt_at=now - timedelta(minutes=5),
        )
        periodic = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"terminal-lane-periodic:{attempt.id}",
            payload={"reason": "PERIODIC"},
            next_attempt_at=now - timedelta(minutes=5),
        )
        terminal = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"terminal-lane-submit:{attempt.id}",
            payload={"reason": "SUBMISSION"},
            next_attempt_at=now,
        )
        db.add_all([course_event, periodic, terminal])
        await db.flush()
        terminal_id = terminal.id

    claimed = await claim_next_outbox_event(
        session_factory,
        settings,
        now=now,
        terminal_checkpoints_only=True,
    )

    assert claimed is not None
    assert claimed.id == terminal_id
    assert claimed.payload["reason"] == "SUBMISSION"


async def test_general_worker_prioritizes_terminal_checkpoint_over_older_periodic_save(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        periodic = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"general-lane-periodic:{attempt.id}",
            payload={"reason": "PERIODIC"},
            next_attempt_at=now - timedelta(minutes=5),
            created_at=now - timedelta(minutes=5),
        )
        terminal = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"general-lane-terminal:{attempt.id}",
            payload={"reason": "SUBMISSION"},
            next_attempt_at=now,
            created_at=now,
        )
        db.add_all([periodic, terminal])
        await db.flush()
        terminal_id = terminal.id

    claimed = await claim_next_outbox_event(session_factory, settings, now=now)

    assert claimed is not None
    assert claimed.id == terminal_id
    assert claimed.payload["reason"] == "SUBMISSION"


async def test_outbox_claim_prioritizes_review_candidate_scan_over_full_history(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        full = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=attempt.assessment_id,
            idempotency_key=f"priority-full-history:{attempt.id}",
            payload={"priority_only": False},
            next_attempt_at=now - timedelta(minutes=10),
            created_at=now - timedelta(minutes=10),
        )
        priority = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=attempt.assessment_id,
            idempotency_key=f"priority-review-candidates:{attempt.id}",
            payload={"priority_only": True},
            next_attempt_at=now,
            created_at=now,
        )
        db.add_all([full, priority])
        await db.flush()
        priority_id = priority.id

    claimed = await claim_next_outbox_event(session_factory, settings, now=now)

    assert claimed is not None
    assert claimed.id == priority_id


async def test_submitted_attempt_status_tracks_terminal_checkpoint_only(app_bundle) -> None:
    _, session_factory, _settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        terminal = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"terminal-status:{attempt.id}",
            payload={"reason": "SUBMISSION"},
            state=SyncOutboxState.PENDING.value,
            next_attempt_at=now,
            created_at=now,
        )
        newer_periodic = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"periodic-status:{attempt.id}",
            payload={"reason": "PERIODIC"},
            state=SyncOutboxState.DELIVERED.value,
            delivered_at=now + timedelta(seconds=1),
            next_attempt_at=now,
            created_at=now + timedelta(seconds=1),
        )
        db.add_all([terminal, newer_periodic])

    async with session_factory() as db:
        _at, status = await _checkpoint_state(db, attempt_id, terminal_only=True)

    assert status == "PENDING"


async def test_unclaimed_terminal_checkpoint_does_not_look_pending_forever(app_bundle) -> None:
    _, session_factory, _settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                attempt_id=attempt.id,
                event_type="attempt.checkpoint",
                aggregate_type="Snapshot",
                aggregate_id=uuid.uuid4(),
                idempotency_key=f"abandoned-terminal-status:{attempt.id}",
                payload={"reason": "SUBMISSION"},
                state=SyncOutboxState.PENDING.value,
                next_attempt_at=now - timedelta(minutes=2),
                created_at=now - timedelta(minutes=2),
            )
        )

    async with session_factory() as db:
        _at, status = await _checkpoint_state(
            db,
            attempt_id,
            terminal_only=True,
            now=now,
        )

    assert status == "ERROR"


async def test_retrying_terminal_checkpoint_remains_pending_while_worker_recovers(
    app_bundle,
) -> None:
    _, session_factory, _settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                attempt_id=attempt.id,
                event_type="attempt.checkpoint",
                aggregate_type="Snapshot",
                aggregate_id=uuid.uuid4(),
                idempotency_key=f"retry-terminal-status:{attempt.id}",
                payload={"reason": "SUBMISSION"},
                state=SyncOutboxState.RETRY.value,
                attempts=1,
                last_error="BROWSER_BUSY: Moodle browser is busy",
                next_attempt_at=now + timedelta(seconds=15),
            )
        )

    async with session_factory() as db:
        _at, status = await _checkpoint_state(db, attempt_id, terminal_only=True, now=now)

    assert status == "PENDING"


async def test_failed_terminal_checkpoint_can_be_requeued_without_new_submission(
    app_bundle,
) -> None:
    _, session_factory, _settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        db.add(
            CourseMembership(
                course_id=course_id,
                principal_id=attempt.principal_id,
                role=CourseRole.STUDENT.value,
            )
        )
        attempt.state = AttemptState.SUBMITTED.value
        attempt.submitted_at = now
        attempt.submission_source = "MANUAL"
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
            submitted_at=now,
        )
        db.add(submission)
        await db.flush()
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="SUBMISSION",
        )
        event.state = SyncOutboxState.FAILED.value
        event.attempts = 8
        event.last_error = "temporary Moodle failure"
        event.locked_at = now
        principal_id = attempt.principal_id
        event_id = event.id
        submission_id = submission.id

    async with session_factory() as db, db.begin():
        returned = await retry_submission_checkpoint(
            db,
            attempt_id=attempt_id,
            principal_id=principal_id,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        submissions = list(
            (await db.scalars(select(Submission).where(Submission.attempt_id == attempt_id))).all()
        )

    assert returned.id == submission_id
    assert len(submissions) == 1
    assert event is not None
    assert event.state == SyncOutboxState.RETRY.value
    assert event.attempts == 0
    assert event.last_error == ""
    assert event.locked_at is None


def _browser_state(marker: str) -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": marker,
                "domain": "moodle.example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


async def _configure_playwright_submission(
    session_factory,
    settings,
    *,
    attempt_id: uuid.UUID,
    course_id: uuid.UUID,
    module: str,
    answer_transport: str,
    activity: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        connection = await db.get(LMSConnection, course.connection_id)
        assert connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        db.add(
            MoodleCredential(
                connection_id=connection.id,
                principal_id=attempt.principal_id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _browser_state("before-finalization-check"),
                    settings,
                    connection_id=connection.id,
                    principal_id=attempt.principal_id,
                ),
                status="ACTIVE",
            )
        )
        metadata: dict[str, Any] = {
            "provider": "MOODLE",
            "module": module,
            "cmid": 777,
            "submission_mode": answer_transport,
            "sync_state": "CURRENT",
        }
        if activity is not None:
            metadata["activity"] = activity
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=attempt.assessment_id,
                external_type=f"mod_{module}",
                external_id="777",
                metadata_json=metadata,
            )
        )
        return connection.id, attempt.principal_id


async def _seed_playwright_course_sync(
    session_factory,
    settings,
    *,
    missing_teacher_first: bool = False,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, str]:
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        teacher = await db.get(ExternalPrincipal, attempt.principal_id)
        connection = await db.get(LMSConnection, course.connection_id)
        assert teacher is not None and connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        if missing_teacher_first:
            missing_teacher = ExternalPrincipal(
                id=uuid.UUID(int=1),
                connection_id=connection.id,
                external_subject="7",
                display_name="Teacher without browser session",
            )
            db.add(missing_teacher)
            await db.flush()
            db.add(
                CourseMembership(
                    course_id=course.id,
                    principal_id=missing_teacher.id,
                    role=CourseRole.TEACHER.value,
                )
            )
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=teacher.id,
                role=CourseRole.TEACHER.value,
            )
        )
        await _grant_teacher(db, teacher)
        credential = MoodleCredential(
            connection_id=connection.id,
            principal_id=teacher.id,
            kind=BROWSER_STATE_CREDENTIAL_KIND,
            encrypted_secret=encrypt_moodle_browser_state(
                _browser_state("before-course-sync"),
                settings,
                connection_id=connection.id,
                principal_id=teacher.id,
            ),
            status="ACTIVE",
        )
        event = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="course.sync",
            aggregate_type="Course",
            aggregate_id=course.id,
            idempotency_key=f"course-browser-test:{course.id}",
            payload={"course_id": course.external_id},
        )
        db.add_all([credential, event])
        await db.flush()
        return event.id, credential.id, connection.id, teacher.external_subject


def _course_discovery_response(actor_external_subject: str) -> dict[str, Any]:
    return {
        "discovery": {
            "external_id": "549",
            "actor_role": "TEACHER",
            "preview": {
                "external_id": "549",
                "title": "C++ synchronized",
                "short_name": "CPP",
                "external_revision": "a" * 64,
                "membership_revision": "b" * 64,
                "starts_at_epoch": 0,
                "ends_at_epoch": 0,
                "sections": [],
                "groups": [],
                "membership_snapshot": {
                    "complete": True,
                    "members": [
                        {
                            "user_id": actor_external_subject,
                            "display_name": "Teacher",
                            "role": "TEACHER",
                            "groups": [],
                        }
                    ],
                },
            },
            "capabilities": {"roster": True},
        },
        "storage_state": _browser_state("after-course-sync"),
    }


def test_checkpoint_manifest_validates_envelope_and_each_file() -> None:
    content = "int main() {}\n"
    manifest = [
        {
            "id": str(uuid.uuid4()),
            "path": "main.cpp",
            "language": "CPP",
            "content": content,
            "content_hash": sha256_text(content),
        }
    ]
    manifest_json = canonical_json(manifest)
    digest = hashlib.sha256(manifest_json.encode()).hexdigest()

    assert validate_checkpoint_manifest(manifest_json, digest, max_bytes=4096) == manifest

    tampered = [dict(manifest[0], content="changed")]
    tampered_json = canonical_json(tampered)
    with pytest.raises(IntegrationProtocolError, match="content hash"):
        validate_checkpoint_manifest(
            tampered_json,
            hashlib.sha256(tampered_json.encode()).hexdigest(),
            max_bytes=4096,
        )
    with pytest.raises(IntegrationProtocolError, match="size limit"):
        validate_checkpoint_manifest(manifest_json, digest, max_bytes=10)


def test_checkpoint_cadence_switches_for_last_fifth() -> None:
    started = datetime(2026, 8, 24, 10, tzinfo=UTC)
    attempt = Attempt(
        assessment_id=uuid.uuid4(),
        principal_id=uuid.uuid4(),
        started_at=started,
    )
    assessment = Assessment(
        course_id=uuid.uuid4(),
        created_by_id=uuid.uuid4(),
        title="Control",
        duration_seconds=1000,
    )

    assert (
        checkpoint_interval_seconds(attempt, assessment, now=started + timedelta(seconds=799))
        == 100
    )
    assert (
        checkpoint_interval_seconds(attempt, assessment, now=started + timedelta(seconds=800)) == 50
    )


async def test_scheduler_enqueues_periodic_heartbeat_without_workspace_change(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        duration_seconds=600,
    )

    first = await run_scheduler_iteration(
        session_factory,
        settings,
        now=now,
        include_course_sync=False,
    )
    duplicate = await run_scheduler_iteration(
        session_factory,
        settings,
        now=now,
        include_course_sync=False,
    )
    heartbeat = await run_scheduler_iteration(
        session_factory,
        settings,
        now=now + timedelta(seconds=61),
        include_course_sync=False,
    )

    async with session_factory() as db:
        events = list(
            (
                await db.scalars(
                    select(SyncOutbox)
                    .where(
                        SyncOutbox.attempt_id == attempt_id,
                        SyncOutbox.event_type == "attempt.checkpoint",
                    )
                    .order_by(SyncOutbox.created_at, SyncOutbox.id)
                )
            ).all()
        )

    assert first.checkpoints_enqueued == 1
    assert duplicate.checkpoints_enqueued == 0
    assert heartbeat.checkpoints_enqueued == 1
    assert len(events) == 2
    assert len({event.aggregate_id for event in events}) == 1
    assert len({event.idempotency_key for event in events}) == 2
    assert all(event.payload["reason"] == "PERIODIC" for event in events)
    assert len({event.payload["heartbeat_generation"] for event in events}) == 2


async def test_moodle_finalization_locks_attempt_and_blocks_every_pending_checkpoint(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    await _configure_playwright_submission(
        session_factory,
        settings,
        attempt_id=attempt_id,
        course_id=course_id,
        module="quiz",
        answer_transport="ESSAY_ATTACHMENT",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        current = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
            heartbeat_generation=200,
        )
        queued = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
            heartbeat_generation=100,
        )
        current.created_at = now
        queued.created_at = now + timedelta(seconds=1)

    class FinalizedBridge:
        async def store_checkpoint(self, *_args) -> dict[str, Any]:
            raise IntegrationAttemptFinalized("Moodle attempt is already finalized")

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: FinalizedBridge(),
            client=client,
            now=now + timedelta(seconds=2),
        )

    async with session_factory() as db:
        attempt = await db.get(Attempt, attempt_id)
        assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        events = list(
            (
                await db.scalars(
                    select(SyncOutbox).where(
                        SyncOutbox.attempt_id == attempt_id,
                        SyncOutbox.event_type == "attempt.checkpoint",
                    )
                )
            ).all()
        )
        fingerprints = list(
            (
                await db.scalars(
                    select(LMSSubmissionFingerprint).where(
                        LMSSubmissionFingerprint.attempt_id == attempt_id
                    )
                )
            ).all()
        )
        submissions = list(
            (await db.scalars(select(Submission).where(Submission.attempt_id == attempt_id))).all()
        )
        assert attempt is not None and assessment is not None and workspace is not None
        response = await _attempt_read(db, attempt, assessment, workspace, settings)

    assert attempt.state == AttemptState.LOCKED.value
    assert attempt.submission_source == "MOODLE_FINALIZED"
    assert attempt.submitted_at is not None
    assert response.closure_reason == "LMS_ATTEMPT_FINALIZED"
    assert {event.state for event in events} == {SyncOutboxState.BLOCKED.value}
    assert all(event.last_error.startswith("MOODLE_ATTEMPT_FINALIZED:") for event in events)
    assert fingerprints == []
    assert submissions == []
    with pytest.raises(DomainError) as error:
        ensure_attempt_not_finalized_in_moodle(attempt)
    assert error.value.status_code == 423
    assert error.value.code == "LMS_ATTEMPT_FINALIZED"


async def test_successful_nonfinal_assignment_that_moodle_finalizes_is_attested_then_locked(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    await _configure_playwright_submission(
        session_factory,
        settings,
        attempt_id=attempt_id,
        course_id=course_id,
        module="assign",
        answer_transport="ASSIGN_FILE",
        activity={
            "max_submission_files": 1,
            "file_types_confirmed": True,
            "accepted_file_types": ".c,.cpp,.zip",
            "submission_drafts": False,
        },
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
            heartbeat_generation=300,
        )
        event_id = event.id

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        content = base64.b64decode(body["artifact"]["content_base64"], validate=True)
        return httpx.Response(
            200,
            json={
                "status": "FINALIZED",
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "filename": body["artifact"]["filename"],
                    "sha256": body["artifact"]["sha256"],
                    "size_bytes": len(content),
                    "idempotency_key": body["idempotency_key"],
                },
                "storage_state": _browser_state("after-final-save"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        assert await process_outbox_once(session_factory, settings, client=client)

    async with session_factory() as db:
        attempt = await db.get(Attempt, attempt_id)
        event = await db.get(SyncOutbox, event_id)
        fingerprint = await db.scalar(
            select(LMSSubmissionFingerprint).where(LMSSubmissionFingerprint.outbox_id == event_id)
        )

    assert attempt is not None and attempt.state == AttemptState.LOCKED.value
    assert attempt.submission_source == "MOODLE_FINALIZED"
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert event.receipt["status"] == "FINALIZED"
    assert fingerprint is not None


async def test_next_quiz_checkpoint_is_bound_to_the_previously_attested_moodle_attempt(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    await _configure_playwright_submission(
        session_factory,
        settings,
        attempt_id=attempt_id,
        course_id=course_id,
        module="quiz",
        answer_transport="ESSAY_ATTACHMENT",
    )

    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
            heartbeat_generation=400,
        )

    requests: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        content = base64.b64decode(body["artifact"]["content_base64"], validate=True)
        return httpx.Response(
            200,
            json={
                "status": "DRAFT_SAVED",
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "attempt_id": "123",
                    "question_slot": "7",
                    "filename": body["artifact"]["filename"],
                    "sha256": body["artifact"]["sha256"],
                    "size_bytes": len(content),
                    "idempotency_key": body["idempotency_key"],
                },
                "storage_state": _browser_state(f"after-quiz-{len(requests)}"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        assert await process_outbox_once(session_factory, settings, client=client)
        async with session_factory() as db, db.begin():
            attempt = await db.get(Attempt, attempt_id)
            workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
            assert attempt is not None and workspace is not None
            snapshot = await create_snapshot(db, workspace, "PERIODIC")
            await enqueue_checkpoint(
                db,
                attempt=attempt,
                snapshot=snapshot,
                reason="PERIODIC",
                heartbeat_generation=500,
            )
        assert await process_outbox_once(session_factory, settings, client=client)

    assert "expected_attempt_id" not in requests[0]
    assert "expected_question_slot" not in requests[0]
    assert requests[1]["expected_attempt_id"] == "123"
    assert requests[1]["expected_question_slot"] == "7"


async def test_deadline_always_submits_last_server_revision(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now - timedelta(seconds=1),
        revision=7,
    )

    result = await run_scheduler_iteration(
        session_factory,
        settings,
        now=now,
        include_course_sync=False,
    )

    async with session_factory() as db:
        attempt = await db.get(Attempt, attempt_id)
        submission = await db.scalar(select(Submission).where(Submission.attempt_id == attempt_id))
        event = await db.scalar(
            select(SyncOutbox).where(
                SyncOutbox.attempt_id == attempt_id,
                SyncOutbox.event_type == "attempt.checkpoint",
            )
        )
    assert result.attempts_submitted == 1
    assert attempt is not None and attempt.state == AttemptState.AUTO_SUBMITTED.value
    assert submission is not None and submission.source == "DEADLINE"
    assert event is not None and event.payload["workspace_revision"] == 7
    assert event.payload["reason"] == "DEADLINE"


async def test_final_minute_checkpoint_is_terminal_even_without_new_revision(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(seconds=30),
        revision=2,
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
        )
        event.created_at = now

    result = await run_scheduler_iteration(
        session_factory,
        settings,
        now=now,
        include_course_sync=False,
    )

    async with session_factory() as db:
        events = list(
            (await db.scalars(select(SyncOutbox).where(SyncOutbox.attempt_id == attempt_id))).all()
        )
    assert result.checkpoints_enqueued == 1
    assert {event.payload["reason"] for event in events} == {"PERIODIC", "FINAL_MINUTE"}
    assert len({event.aggregate_id for event in events}) == 1


async def test_checkpoint_delivery_observes_committed_claim(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
        )
        event_id = event.id

    class FakeBridge:
        async def store_checkpoint(
            self, payload: dict[str, Any], idempotency_key: str
        ) -> dict[str, Any]:
            async with session_factory() as db:
                visible = await db.get(SyncOutbox, event_id)
                assert visible is not None
                assert visible.state == SyncOutboxState.PROCESSING.value
                assert visible.attempts == 1
            assert payload["attempt_ref"] == str(attempt_id)
            assert idempotency_key
            return {"status": "DELIVERED", "receipt": {"checkpoint": "ok"}}

    def bridge_factory(_settings, _connection, _client):
        return FakeBridge()

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=bridge_factory,
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert event.locked_at is None


async def test_pluginless_checkpoint_is_recorded_local_only_without_external_delivery(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
        )
        event_id = event.id

    assert not await process_outbox_once(session_factory, settings)
    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert event.receipt == {
        "status": "LOCAL_ONLY",
        "external_sync": False,
        "reason": "PLUGINLESS_MOODLE",
    }


@pytest.mark.parametrize("second_transport", ["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"])
async def test_checkpoint_delivery_rejects_duplicate_or_conflicting_current_mappings(
    app_bundle,
    second_transport: str,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and course is not None and workspace is not None
        connection = await db.get(LMSConnection, course.connection_id)
        assert connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        for external_id, transport in (("777", "ESSAY_ATTACHMENT"), ("778", second_transport)):
            db.add(
                ExternalMapping(
                    connection_id=connection.id,
                    local_type="Assessment",
                    local_id=attempt.assessment_id,
                    external_type="mod_quiz",
                    external_id=external_id,
                    metadata_json={
                        "module": "quiz",
                        "cmid": int(external_id),
                        "submission_mode": transport,
                        "sync_state": "CURRENT",
                    },
                )
            )
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="SUBMISSION",
        )
        await db.flush()
        claim = ClaimedOutboxEvent(
            id=event.id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            connection_id=event.connection_id,
            course_id=event.course_id,
            attempt_id=event.attempt_id,
            idempotency_key=event.idempotency_key,
            payload=dict(event.payload),
            attempts=1,
            locked_at=now,
        )
        target = ConnectionTarget(
            id=connection.id,
            base_url=connection.base_url,
            service_token=None,
            mode="PLUGINLESS",
            transport="PLAYWRIGHT",
        )

        with pytest.raises(_BlockedDelivery) as error:
            await _prepare_checkpoint(db, settings, claim, target)
        assert error.value.code == "MOODLE_SUBMISSION_MAPPING_REQUIRED"


@pytest.mark.parametrize(
    "answer_transport",
    ["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"],
)
async def test_first_deferred_quiz_checkpoint_uses_pinned_runtime_binding(
    app_bundle,
    answer_transport: str,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and course is not None and workspace is not None
        connection = await db.get(LMSConnection, course.connection_id)
        assert connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        attempt.integrity_policy = {
            "moodle_course_id": "549",
            "moodle_cmid": 777,
            "moodle_attempt_id": "141716",
            "moodle_question_slot": "1",
            "moodle_answer_transport": answer_transport,
            "moodle_runtime_prepared": True,
        }
        source_confirmation = {
            "title": True,
            "settings": True,
            "statement": False,
            "schedule": True,
            "duration": True,
            "grade": True,
            "attempt_policy": True,
            "statement_deferred": True,
        }
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=attempt.assessment_id,
                external_type="mod_quiz",
                external_id="777",
                metadata_json={
                    "module": "quiz",
                    "cmid": 777,
                    "submission_mode": "REQUIRES_CONFIGURATION",
                    "sync_state": "ANSWER_TRANSPORT_UNSUPPORTED",
                    "moodle_source_confirmation": source_confirmation,
                    "activity": {
                        "module": "quiz",
                        "cmid": 777,
                        "question_count": 1,
                        "essay_question_count": 0,
                        "random_question_count": 1,
                        "random_essay_confirmed": True,
                        "statement_deferred": True,
                        "import_supported": True,
                        "statement_confirmed": False,
                    },
                },
            )
        )
        db.add(
            MoodleCredential(
                connection_id=connection.id,
                principal_id=attempt.principal_id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _browser_state("runtime-binding"),
                    settings,
                    connection_id=connection.id,
                    principal_id=attempt.principal_id,
                ),
                status="ACTIVE",
            )
        )
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
        )
        await db.flush()
        claim = ClaimedOutboxEvent(
            id=event.id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            connection_id=event.connection_id,
            course_id=event.course_id,
            attempt_id=event.attempt_id,
            idempotency_key=event.idempotency_key,
            payload=dict(event.payload),
            attempts=1,
            locked_at=now,
        )
        target = ConnectionTarget(
            id=connection.id,
            base_url=connection.base_url,
            service_token=None,
            mode="PLUGINLESS",
            transport="PLAYWRIGHT",
        )

        delivery = await _prepare_checkpoint(db, settings, claim, target)

    assert delivery.payload["answer_transport"] == answer_transport
    assert delivery.payload["expected_attempt_id"] == "141716"
    assert delivery.payload["expected_question_slot"] == "1"


@pytest.mark.parametrize(
    ("reason", "finalize", "status"),
    [("PERIODIC", False, "DRAFT_SAVED"), ("SUBMISSION", True, "FINALIZED")],
)
@pytest.mark.parametrize(
    "answer_transport",
    ["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"],
)
async def test_playwright_checkpoint_syncs_full_code_to_quiz_essay_and_refreshes_session(
    app_bundle,
    reason: str,
    finalize: bool,
    status: str,
    answer_transport: str,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    settings.moodle_browser_request_body_max_bytes = 6 * 1024 * 1024
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )

    def browser_state(marker: str) -> dict[str, Any]:
        return {
            "cookies": [
                {
                    "name": "MoodleSession",
                    "value": marker,
                    "domain": "moodle.example.test",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }

    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and course is not None and workspace is not None
        connection = await db.get(LMSConnection, course.connection_id)
        assert connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        db.add(
            MoodleCredential(
                connection_id=connection.id,
                principal_id=attempt.principal_id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    browser_state("before"),
                    settings,
                    connection_id=connection.id,
                    principal_id=attempt.principal_id,
                ),
                status="ACTIVE",
            )
        )
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=attempt.assessment_id,
                external_type="mod_quiz",
                external_id="777",
                metadata_json={
                    "provider": "MOODLE",
                    "module": "quiz",
                    "cmid": 777,
                    "submission_mode": answer_transport,
                    "sync_state": "CURRENT",
                },
            )
        )
        if answer_transport == "ESSAY_ONLINE_TEXT":
            # Companion input remains part of the IDE snapshot but must not be
            # serialized into Moodle's text response.
            db.add(
                WorkspaceFile(
                    workspace_id=workspace.id,
                    path="input.txt",
                    language="TEXT",
                    content="42\n",
                    content_hash=sha256_text("42\n"),
                )
            )
            await db.flush()
            await refresh_workspace_hash(db, workspace)
        snapshot = await create_snapshot(db, workspace, reason)
        event = await enqueue_checkpoint(db, attempt=attempt, snapshot=snapshot, reason=reason)
        event_id = event.id
        credential_principal_id = attempt.principal_id
        connection_id = connection.id

    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        content = base64.b64decode(body["artifact"]["content_base64"], validate=True)
        assert content == b"int main() { return 0; }\n"
        assert body["artifact"]["filename"] == "main.cpp"
        assert body["answer_transport"] == answer_transport
        assert hashlib.sha256(content).hexdigest() == body["artifact"]["sha256"]
        assert body["finalize"] is finalize
        return httpx.Response(
            200,
            json={
                "status": status,
                "receipt": {
                    "course_id": "549",
                    "cmid": 777,
                    "attempt_id": "123",
                    "question_slot": "1",
                    "filename": "main.cpp",
                    "sha256": body["artifact"]["sha256"],
                    "size_bytes": len(content),
                    "idempotency_key": body["idempotency_key"],
                },
                "storage_state": browser_state("after"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        assert await process_outbox_once(session_factory, settings, client=client)

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        fingerprint = await db.scalar(
            select(LMSSubmissionFingerprint).where(LMSSubmissionFingerprint.outbox_id == event_id)
        )
        credential = await db.scalar(
            select(MoodleCredential).where(
                MoodleCredential.connection_id == connection_id,
                MoodleCredential.principal_id == credential_principal_id,
                MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
            )
        )
    assert captured["course_id"] == "549"
    assert captured["cmid"] == 777
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert event.receipt["status"] == status
    assert fingerprint is not None
    assert fingerprint.submission_id is None
    assert fingerprint.checkpoint_reason == reason
    assert fingerprint.terminal is (reason == "SUBMISSION")
    assert (
        fingerprint.artifact_md5
        == hashlib.md5(b"int main() { return 0; }\n", usedforsecurity=False).hexdigest()
    )
    assert fingerprint.artifact_sha256 == hashlib.sha256(b"int main() { return 0; }\n").hexdigest()
    assert credential is not None and credential.revision == 2
    assert credential.lease_owner is None and credential.lease_expires_at is None
    refreshed = decrypt_moodle_browser_state(
        credential.encrypted_secret,
        settings,
        connection_id=connection_id,
        principal_id=credential_principal_id,
    )
    assert refreshed["cookies"][0]["value"] == "after"


async def test_playwright_checkpoint_expires_rejected_browser_session(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    state = {"cookies": [], "origins": []}
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and course is not None and workspace is not None
        connection = await db.get(LMSConnection, course.connection_id)
        assert connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        credential = MoodleCredential(
            connection_id=connection.id,
            principal_id=attempt.principal_id,
            kind=BROWSER_STATE_CREDENTIAL_KIND,
            encrypted_secret=encrypt_moodle_browser_state(
                state,
                settings,
                connection_id=connection.id,
                principal_id=attempt.principal_id,
            ),
            status="ACTIVE",
        )
        mapping = ExternalMapping(
            connection_id=connection.id,
            local_type="Assessment",
            local_id=attempt.assessment_id,
            external_type="mod_quiz",
            external_id="777",
            metadata_json={
                "module": "quiz",
                "cmid": 777,
                "submission_mode": "ESSAY_ATTACHMENT",
                "sync_state": "CURRENT",
            },
        )
        db.add_all([credential, mapping])
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="SUBMISSION",
        )
        await db.flush()
        credential_id = credential.id
        event_id = event.id

    class ExpiredBrowser:
        async def store_checkpoint(self, *_args, **_kwargs) -> dict[str, Any]:
            raise MoodleAuthenticationError("Moodle browser session expired")

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: ExpiredBrowser(),
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        credential = await db.get(MoodleCredential, credential_id)
    assert event is not None and event.state == SyncOutboxState.FAILED.value
    assert event.last_error.startswith("MOODLE_AUTHENTICATION_FAILED:")
    assert credential is not None and credential.status == "EXPIRED"
    assert credential.expires_at is not None
    assert credential.lease_owner is None and credential.lease_expires_at is None


async def test_playwright_old_draft_is_superseded_by_terminal_checkpoint(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and course is not None and workspace is not None
        connection = await db.get(LMSConnection, course.connection_id)
        assert connection is not None
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        draft = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
        )
        terminal = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="SUBMISSION",
        )
        draft.created_at = now
        terminal.created_at = now + timedelta(seconds=1)
        # Keep the terminal row present for supersession evidence while making
        # only the older draft due in this focused preparation test. Queue
        # ordering itself is covered by the terminal-priority tests above.
        terminal.next_attempt_at = now + timedelta(minutes=1)
        draft_id = draft.id
        terminal_id = terminal.id

    def bridge_factory(*_args):
        raise AssertionError("superseded draft must not contact Moodle")

    assert await process_outbox_once(
        session_factory,
        settings,
        bridge_factory=bridge_factory,
    )

    async with session_factory() as db:
        draft = await db.get(SyncOutbox, draft_id)
        terminal = await db.get(SyncOutbox, terminal_id)
    assert draft is not None and draft.state == SyncOutboxState.BLOCKED.value
    assert draft.last_error.startswith("SUPERSEDED:")
    assert terminal is not None and terminal.state == SyncOutboxState.PENDING.value


async def test_superseded_review_event_is_blocked_without_external_call(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        course = await db.get(Course, course_id)
        assert attempt is not None and workspace is not None and course is not None
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
        )
        db.add(submission)
        await db.flush()
        older = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=attempt.principal_id,
            revision=1,
            grade=Decimal("7"),
            status="SUPERSEDED",
        )
        newer = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=attempt.principal_id,
            revision=2,
            grade=Decimal("8"),
        )
        db.add_all([older, newer])
        await db.flush()
        event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=older.id,
            idempotency_key=f"review-test:{older.id}",
            payload={},
        )
        db.add(event)
        await db.flush()
        event_id = event.id

    def bridge_factory(*_args):
        raise AssertionError("superseded grade must not contact Moodle")

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=bridge_factory,
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.BLOCKED.value
    assert event.last_error.startswith("SUPERSEDED:")


@pytest.mark.parametrize("auth_mode", ["BRIDGE", "PLUGINLESS"])
async def test_latest_grade_uses_explicit_mod_assign_mapping_and_numeric_user(
    app_bundle,
    auth_mode: str,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode=auth_mode,
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        course = await db.get(Course, course_id)
        assert attempt is not None and workspace is not None and course is not None
        reviewer_id = attempt.principal_id
        if auth_mode == "PLUGINLESS":
            token = "reviewer-mobile-token-1234567890"
            db.add(
                MoodleCredential(
                    connection_id=course.connection_id,
                    principal_id=reviewer_id,
                    kind="MOBILE_TOKEN",
                    encrypted_secret=encrypt_moodle_credential(
                        token,
                        settings,
                        connection_id=course.connection_id,
                        principal_id=reviewer_id,
                    ),
                    status="ACTIVE",
                )
            )
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
        )
        db.add(submission)
        await db.flush()
        decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=reviewer_id,
            revision=1,
            grade=Decimal("8.50"),
            comment="Good",
        )
        mapping = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=attempt.assessment_id,
            external_type="mod_assign",
            external_id="777",
            metadata_json={
                "module": "assign",
                "cmid": 777,
                "sync_state": "CURRENT",
                "activity": {
                    "cmid": 777,
                    "instance_id": 91,
                    "module": "assign",
                    "grade_max": 10.0,
                },
            },
        )
        db.add_all([decision, mapping])
        await db.flush()
        event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
            idempotency_key=f"review-test:{decision.id}",
            payload={},
        )
        db.add(event)
        await db.flush()
        event_id = event.id
        decision_id = decision.id
        submission_id = submission.id

    class FakeBridge:
        async def push_grade(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
            expected = {
                "courseid": 549,
                "cmid": 777,
                "userid": 42,
                "grade": "8.50",
                "comment": "Good\n\nStudent",
            }
            if auth_mode == "PLUGINLESS":
                expected["assignmentid"] = 91
            assert payload == expected
            return {"status": "DELIVERED", "receipt": {"key": idempotency_key}}

    def factory(_settings, target, _client):
        assert target.mode == auth_mode
        if auth_mode == "PLUGINLESS":
            assert target.service_token == "reviewer-mobile-token-1234567890"
            assert target.principal_id == reviewer_id
        return FakeBridge()

    async with httpx.AsyncClient() as client:
        await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=factory,
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        decision = await db.get(ReviewDecision, decision_id)
        submission = await db.get(Submission, submission_id)
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert decision is not None and decision.lms_export_state == "DELIVERED"
    assert submission is not None and submission.lms_export_state == "DELIVERED"


@pytest.mark.parametrize(
    ("mapped_attempt_id", "grade_exported"),
    [("user-42-attempt-2", True), ("user-42-attempt-1", False)],
)
async def test_playwright_historical_assignment_grade_targets_exact_reopened_attempt(
    app_bundle,
    mapped_attempt_id: str,
    grade_exported: bool,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        course = await db.get(Course, course_id)
        assert attempt is not None and workspace is not None and course is not None
        assessment = await db.get(Assessment, attempt.assessment_id)
        reviewer = await db.get(ExternalPrincipal, attempt.principal_id)
        connection = await db.get(LMSConnection, course.connection_id)
        assert assessment is not None and reviewer is not None and connection is not None
        attempt.state = AttemptState.SUBMITTED.value
        attempt.submitted_at = now
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        db.add(
            MoodleCredential(
                connection_id=connection.id,
                principal_id=reviewer.id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _browser_state("before-assignment-grade"),
                    settings,
                    connection_id=connection.id,
                    principal_id=reviewer.id,
                ),
                status="ACTIVE",
            )
        )
        snapshot = await create_snapshot(db, workspace, "LMS_IMPORT")
        external_id = "assign:777:user-42-attempt-2"
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MOODLE_IMPORT",
            submitted_at=now,
            external_receipt={
                "source": "MOODLE_HISTORY",
                "external_id": external_id,
                "lms_module": "assign",
                "lms_cmid": "777",
                "moodle_parent_attempt_id": "user-42-attempt-2",
            },
        )
        db.add(submission)
        await db.flush()
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Submission",
                local_id=submission.id,
                external_type="moodle_historical_submission",
                external_id=external_id,
                metadata_json={
                    "course_id": "549",
                    "assessment_id": str(assessment.id),
                    "module": "assign",
                    "cmid": 777,
                    "moodle_parent_attempt_id": mapped_attempt_id,
                },
            )
        )
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type="mod_assign",
                external_id="777",
                metadata_json={
                    "module": "assign",
                    "cmid": 777,
                    "sync_state": "CURRENT",
                    "activity": {
                        "module": "assign",
                        "cmid": 777,
                        "instance_id": 91,
                        "grade_max": 10.0,
                    },
                },
            )
        )
        decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=reviewer.id,
            revision=1,
            grade=Decimal("8.00"),
            comment="Проверено",
        )
        db.add(decision)
        await db.flush()
        event = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
            idempotency_key=f"assign-history-grade:{decision.id}",
            payload={},
        )
        db.add(event)
        await db.flush()
        event_id = event.id

    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": "DELIVERED",
                "receipt": {"module": "assign", "attempt_number": 2},
                "storage_state": _browser_state("after-assignment-grade"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        assert await process_outbox_once(session_factory, settings, client=client)

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None
    if grade_exported:
        assert captured["payload"] == {
            "module": "assign",
            "course_id": "549",
            "cmid": 777,
            "user_id": "42",
            "grade": 8.0,
            "comment": "Проверено\n\nStudent",
            "attempt_number": 2,
        }
        assert event.state == SyncOutboxState.DELIVERED.value
    else:
        assert captured == {}
        assert event.state == SyncOutboxState.BLOCKED.value
        assert event.last_error.startswith("MAPPING_NOT_CONFIRMED:")


@pytest.mark.parametrize(
    ("quiz_grading_method", "method_confirmed", "grade_confirmed", "expected_error"),
    [
        ("LAST", True, True, None),
        ("HIGHEST", True, True, None),
        ("AVERAGE", True, True, None),
        ("FIRST", True, True, None),
        ("UNKNOWN", True, True, "MOODLE_QUIZ_GRADING_METHOD_UNCONFIRMED"),
        ("HIGHEST", False, True, "MOODLE_QUIZ_GRADING_METHOD_UNCONFIRMED"),
        ("LAST", True, False, "QUIZ_GRADE_SCALE_UNCONFIRMED"),
    ],
)
async def test_playwright_exports_historical_quiz_essay_grade_to_exact_slot(
    app_bundle,
    quiz_grading_method: str,
    method_confirmed: bool,
    grade_confirmed: bool,
    expected_error: str | None,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        course = await db.get(Course, course_id)
        assert attempt is not None and workspace is not None and course is not None
        reviewer = await db.get(ExternalPrincipal, attempt.principal_id)
        connection = await db.get(LMSConnection, course.connection_id)
        assert reviewer is not None and connection is not None
        assessment = await db.get(Assessment, attempt.assessment_id)
        assert assessment is not None
        assessment.max_score = Decimal("3.00")
        parent_assessment = Assessment(
            course_id=course.id,
            title="Самостоятельная работа №1",
            max_score=Decimal("3.00"),
            created_by_id=reviewer.id,
            status="PUBLISHED",
        )
        db.add(parent_assessment)
        await db.flush()
        assessment.policy = {
            "moodle_quiz_question_split": True,
            "moodle_parent_assessment_id": str(parent_assessment.id),
        }
        # A principal created by an older connector could retain both Moodle's
        # compact avatar initials and given-name-first ordering until relogin.
        reviewer.display_name = "АК Алексей Коваленко"
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        db.add(
            MoodleCredential(
                connection_id=connection.id,
                principal_id=reviewer.id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _browser_state("before-grade"),
                    settings,
                    connection_id=connection.id,
                    principal_id=reviewer.id,
                ),
                status="ACTIVE",
            )
        )
        snapshot = await create_snapshot(db, workspace, "LMS_IMPORT")
        external_id = "quiz:777:134403:essay:response-1"
        provenance = {
            "source": "MOODLE_HISTORY",
            "external_id": external_id,
            "moodle_parent_attempt_id": "134403",
            "moodle_response_id": "1",
            "moodle_response_position": 1,
        }
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MOODLE_IMPORT",
            external_receipt=provenance,
        )
        db.add(submission)
        await db.flush()
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Submission",
                local_id=submission.id,
                external_type="moodle_historical_submission",
                external_id=external_id,
                metadata_json={
                    "course_id": "549",
                    "assessment_id": str(attempt.assessment_id),
                    "module": "quiz",
                    "cmid": 777,
                    "moodle_parent_attempt_id": "134403",
                    "moodle_response_id": "1",
                    "moodle_response_position": 1,
                },
            )
        )
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=parent_assessment.id,
                external_type="mod_quiz",
                external_id="777",
                metadata_json={
                    "module": "quiz",
                    "cmid": 777,
                    "activity": {
                        "module": "quiz",
                        "cmid": 777,
                        "grade_confirmed": grade_confirmed,
                        "grade_max": 3.0,
                        "quiz_grading_method": quiz_grading_method,
                        "quiz_grading_method_confirmed": method_confirmed,
                    },
                },
            )
        )
        decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=reviewer.id,
            revision=1,
            grade=Decimal("1.20"),
            comment="Хорошо\n\nКоваленко А.\nКоваленко А.",
        )
        db.add(decision)
        await db.flush()
        event = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
            idempotency_key=f"quiz-grade:{decision.id}",
            payload={},
        )
        db.add(event)
        await db.flush()
        event_id = event.id

    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.update(body)
        return httpx.Response(
            200,
            json={
                "status": "DELIVERED",
                "receipt": {
                    "module": "quiz",
                    "attempt_id": "134403",
                    "question_slot": 1,
                },
                "storage_state": _browser_state("after-grade"),
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        assert await process_outbox_once(session_factory, settings, client=client)

    if expected_error is None:
        assert captured["payload"] == {
            "module": "quiz",
            "course_id": "549",
            "cmid": 777,
            "user_id": "42",
            "grade": 1.2,
            "grade_scale_max": 3.0,
            "quiz_overall_grade_max": 3.0,
            "comment": "Хорошо\n\nКоваленко А.",
            "attempt_id": "134403",
            "question_slot": 1,
        }
    else:
        assert captured == {}
    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None
    if expected_error is None:
        assert event.state == SyncOutboxState.DELIVERED.value
    else:
        assert event.state == SyncOutboxState.BLOCKED.value
        assert event.last_error.startswith(f"{expected_error}:")


async def _seed_app_quiz_grade_delivery(
    session_factory,
    settings,
    *,
    policy_cmid: int = 777,
    mapping_cmid: int = 777,
    remote_attempt_id: str = "141720",
    quiz_grading_method: str = "LAST",
    quiz_grading_method_confirmed: bool = True,
) -> tuple[ClaimedOutboxEvent, ConnectionTarget, uuid.UUID, uuid.UUID, uuid.UUID]:
    """Create a locally authored Quiz submission with an exact terminal attestation."""

    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
        auth_mode="PLUGINLESS",
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        course = await db.get(Course, course_id)
        assert attempt is not None and workspace is not None and course is not None
        assessment = await db.get(Assessment, attempt.assessment_id)
        reviewer = await db.get(ExternalPrincipal, attempt.principal_id)
        connection = await db.get(LMSConnection, course.connection_id)
        assert assessment is not None and reviewer is not None and connection is not None
        assessment.max_score = Decimal("3.00")
        attempt.state = AttemptState.SUBMITTED.value
        attempt.submitted_at = now
        attempt.integrity_policy = {
            "moodle_course_id": "549",
            "moodle_cmid": policy_cmid,
            "moodle_attempt_id": remote_attempt_id,
            "moodle_question_slot": "1",
            "moodle_answer_transport": "ESSAY_ATTACHMENT",
            "moodle_runtime_prepared": True,
        }
        connection.config = {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
        }
        db.add(
            MoodleCredential(
                connection_id=connection.id,
                principal_id=reviewer.id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _browser_state("before-app-quiz-grade"),
                    settings,
                    connection_id=connection.id,
                    principal_id=reviewer.id,
                ),
                status="ACTIVE",
            )
        )
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type="mod_quiz",
                external_id=str(mapping_cmid),
                metadata_json={
                    "module": "quiz",
                    "cmid": mapping_cmid,
                    "activity": {
                        "module": "quiz",
                        "cmid": mapping_cmid,
                        "grade_confirmed": True,
                        "grade_max": 3.0,
                        "quiz_grading_method": quiz_grading_method,
                        "quiz_grading_method_confirmed": quiz_grading_method_confirmed,
                    },
                },
            )
        )
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
            submitted_at=now,
        )
        db.add(submission)
        await db.flush()
        checkpoint = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Attempt",
            aggregate_id=attempt.id,
            idempotency_key=f"app-quiz-terminal:{attempt.id}",
            state=SyncOutboxState.DELIVERED.value,
            delivered_at=now,
        )
        db.add(checkpoint)
        await db.flush()
        artifact = b"int main() { return 0; }\n"
        db.add(
            LMSSubmissionFingerprint(
                outbox_id=checkpoint.id,
                connection_id=connection.id,
                course_id=course.id,
                assessment_id=assessment.id,
                principal_id=attempt.principal_id,
                attempt_id=attempt.id,
                submission_id=submission.id,
                snapshot_id=snapshot.id,
                module="quiz",
                external_activity_id=str(mapping_cmid),
                external_attempt_id=remote_attempt_id,
                external_question_slot="1",
                answer_transport="ESSAY_ATTACHMENT",
                artifact_filename="main.cpp",
                artifact_size=len(artifact),
                artifact_md5=hashlib.md5(artifact, usedforsecurity=False).hexdigest(),
                artifact_sha256=hashlib.sha256(artifact).hexdigest(),
                comparison_md5=hashlib.md5(artifact, usedforsecurity=False).hexdigest(),
                comparison_sha256=hashlib.sha256(artifact).hexdigest(),
                canonicalization="RAW_BYTES_V1",
                checkpoint_reason="SUBMISSION",
                terminal=True,
                delivered_at=now,
            )
        )
        decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=reviewer.id,
            revision=1,
            grade=Decimal("1.20"),
            comment="Latest app attempt",
        )
        db.add(decision)
        await db.flush()
        grade_event = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
            idempotency_key=f"app-quiz-grade:{decision.id}",
            payload={},
        )
        db.add(grade_event)
        await db.flush()
        claim = ClaimedOutboxEvent(
            id=grade_event.id,
            event_type=grade_event.event_type,
            aggregate_type=grade_event.aggregate_type,
            aggregate_id=grade_event.aggregate_id,
            connection_id=grade_event.connection_id,
            course_id=grade_event.course_id,
            attempt_id=grade_event.attempt_id,
            idempotency_key=grade_event.idempotency_key,
            payload=dict(grade_event.payload or {}),
            attempts=1,
            locked_at=now,
        )
        target = ConnectionTarget(
            id=connection.id,
            base_url=connection.base_url,
            service_token=None,
            mode="PLUGINLESS",
            transport="PLAYWRIGHT",
        )
        return claim, target, attempt.id, submission.id, assessment.id


@pytest.mark.parametrize("quiz_grading_method", ["LAST", "HIGHEST", "AVERAGE", "FIRST"])
async def test_app_authored_latest_quiz_attempt_exports_grade_before_history_import(
    app_bundle,
    quiz_grading_method: str,
) -> None:
    _, session_factory, settings = app_bundle
    claim, target, _attempt_id, submission_id, _assessment_id = await _seed_app_quiz_grade_delivery(
        session_factory, settings, quiz_grading_method=quiz_grading_method,
    )

    async with session_factory() as db:
        delivery = await _prepare_grade(db, settings, claim, target)

    assert delivery.submission_id == submission_id
    assert delivery.payload == {
        "module": "quiz",
        "courseid": 549,
        "cmid": 777,
        "userid": 42,
        "attempt_id": 141720,
        "question_slot": 1,
        "grade": "1.20",
        "grade_scale_max": "3.00",
        "quiz_overall_grade_max": "3.0",
        "comment": "Latest app attempt\n\nStudent",
    }


@pytest.mark.parametrize(
    ("method", "confirmed"), [("UNKNOWN", True), ("HIGHEST", False)],
)
async def test_app_authored_quiz_grade_requires_confirmed_aggregation_method(
    app_bundle, method: str, confirmed: bool,
) -> None:
    _, session_factory, settings = app_bundle
    claim, target, *_ = await _seed_app_quiz_grade_delivery(
        session_factory, settings,
        quiz_grading_method=method, quiz_grading_method_confirmed=confirmed,
    )
    async with session_factory() as db:
        with pytest.raises(_BlockedDelivery) as blocked:
            await _prepare_grade(db, settings, claim, target)
    assert blocked.value.code == "MOODLE_QUIZ_GRADING_METHOD_UNCONFIRMED"


@pytest.mark.parametrize("quiz_grading_method", ["LAST", "HIGHEST", "AVERAGE", "FIRST"])
async def test_app_authored_older_quiz_attempt_grade_is_rejected(
    app_bundle,
    quiz_grading_method: str,
) -> None:
    _, session_factory, settings = app_bundle
    (
        claim,
        target,
        older_attempt_id,
        _submission_id,
        assessment_id,
    ) = await _seed_app_quiz_grade_delivery(
        session_factory,
        settings,
        remote_attempt_id="141719",
        quiz_grading_method=quiz_grading_method,
    )
    async with session_factory() as db, db.begin():
        older = await db.get(Attempt, older_attempt_id)
        assert older is not None
        newer = Attempt(
            assessment_id=assessment_id,
            principal_id=older.principal_id,
            sequence=2,
            state=AttemptState.SUBMITTED.value,
            started_at=older.started_at + timedelta(minutes=1),
            submitted_at=(older.submitted_at or older.started_at) + timedelta(minutes=1),
            integrity_policy={
                **dict(older.integrity_policy or {}),
                "moodle_attempt_id": "141720",
            },
        )
        db.add(newer)
        await db.flush()
        newer_workspace = Workspace(attempt_id=newer.id, current_revision=0)
        db.add(newer_workspace)
        await db.flush()
        newer_snapshot = await create_snapshot(db, newer_workspace, "SUBMISSION")
        db.add(
            Submission(
                attempt_id=newer.id,
                snapshot_id=newer_snapshot.id,
                revision=1,
                source="MANUAL",
                submitted_at=newer.submitted_at,
            )
        )

    async with session_factory() as db:
        with pytest.raises(_BlockedDelivery) as blocked:
            await _prepare_grade(db, settings, claim, target)
    assert blocked.value.code == "SUPERSEDED"


async def test_app_authored_quiz_binding_mapping_mismatch_is_rejected(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    (
        claim,
        target,
        _attempt_id,
        _submission_id,
        _assessment_id,
    ) = await _seed_app_quiz_grade_delivery(
        session_factory,
        settings,
        policy_cmid=778,
        mapping_cmid=777,
    )

    async with session_factory() as db:
        with pytest.raises(_BlockedDelivery) as blocked:
            await _prepare_grade(db, settings, claim, target)
    assert blocked.value.code == "MOODLE_ATTEMPT_BINDING_CONFLICT"


async def test_app_authored_quiz_grade_requires_exact_terminal_delivery(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    claim, target, attempt_id, _submission_id, _assessment_id = await _seed_app_quiz_grade_delivery(
        session_factory, settings
    )
    async with session_factory() as db, db.begin():
        fingerprint = await db.scalar(
            select(LMSSubmissionFingerprint).where(
                LMSSubmissionFingerprint.attempt_id == attempt_id,
                LMSSubmissionFingerprint.terminal.is_(True),
            )
        )
        assert fingerprint is not None
        await db.delete(fingerprint)

    async with session_factory() as db:
        with pytest.raises(_BlockedDelivery) as blocked:
            await _prepare_grade(db, settings, claim, target)
    assert blocked.value.code == "MOODLE_QUIZ_TERMINAL_DELIVERY_REQUIRED"


async def test_grade_delivery_completion_cannot_acknowledge_a_newer_pending_review(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        course = await db.get(Course, course_id)
        assert attempt is not None and workspace is not None and course is not None
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
        )
        db.add(submission)
        await db.flush()
        older = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=attempt.principal_id,
            revision=1,
            grade=Decimal("7"),
        )
        mapping = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=attempt.assessment_id,
            external_type="mod_assign",
            external_id="777",
            metadata_json={
                "module": "assign",
                "cmid": 777,
                "sync_state": "CURRENT",
                "activity": {"cmid": 777, "module": "assign", "grade_max": 10.0},
            },
        )
        db.add_all([older, mapping])
        await db.flush()
        event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=older.id,
            idempotency_key=f"review-race:{older.id}",
        )
        db.add(event)
        await db.flush()
        event_id = event.id
        older_id = older.id
        submission_id = submission.id

    class RacingBridge:
        async def push_grade(
            self, _payload: dict[str, Any], _idempotency_key: str
        ) -> dict[str, Any]:
            async with session_factory() as db, db.begin():
                old_row = await db.get(ReviewDecision, older_id)
                submission_row = await db.get(Submission, submission_id)
                assert old_row is not None and submission_row is not None
                assert submission_row.lms_export_state == "PROCESSING"
                old_row.status = "SUPERSEDED"
                old_row.lms_export_state = "SUPERSEDED"
                db.add(
                    ReviewDecision(
                        submission_id=submission_id,
                        reviewer_id=old_row.reviewer_id,
                        revision=2,
                        grade=Decimal("9"),
                    )
                )
                submission_row.lms_export_state = "PENDING"
            return {"status": "DELIVERED", "receipt": {"race": "simulated"}}

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: RacingBridge(),
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        old_decision = await db.get(ReviewDecision, older_id)
        submission = await db.get(Submission, submission_id)
    assert event is not None and event.state == SyncOutboxState.BLOCKED.value
    assert event.last_error.startswith("SUPERSEDED:")
    assert old_decision is not None and old_decision.lms_export_state == "SUPERSEDED"
    assert submission is not None and submission.lms_export_state == "PENDING"


async def test_retryable_delivery_uses_bounded_backoff(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="PERIODIC",
        )
        event_id = event.id

    class FailingBridge:
        async def store_checkpoint(self, *_args) -> dict[str, Any]:
            raise IntegrationUnavailable("Moodle unavailable")

    process_now = now + timedelta(seconds=1)
    async with httpx.AsyncClient() as client:
        await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: FailingBridge(),
            client=client,
            now=process_now,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.RETRY.value
    assert event.attempts == 1
    assert event.next_attempt_at == process_now.replace(tzinfo=None) + timedelta(
        seconds=settings.sync_retry_base_seconds
    )


async def test_retryable_terminal_delivery_uses_short_student_facing_backoff(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "SUBMISSION")
        event = await enqueue_checkpoint(
            db,
            attempt=attempt,
            snapshot=snapshot,
            reason="SUBMISSION",
        )
        event_id = event.id

    class FailingBridge:
        async def store_checkpoint(self, *_args) -> dict[str, Any]:
            raise IntegrationUnavailable("Moodle unavailable")

    process_now = now + timedelta(seconds=1)
    async with httpx.AsyncClient() as client:
        await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: FailingBridge(),
            client=client,
            now=process_now,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.RETRY.value
    assert event.attempts == 1
    assert event.next_attempt_at == process_now.replace(tzinfo=None) + timedelta(seconds=5)


async def test_recovery_rejects_checkpoint_for_another_attempt(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    content = "int main() {}\n"
    manifest = [
        {
            "id": str(uuid.uuid4()),
            "path": "main.cpp",
            "content": content,
            "content_hash": sha256_text(content),
        }
    ]
    manifest_json = canonical_json(manifest)

    class FakeBridge:
        async def get_latest_checkpoint(self, **_kwargs) -> dict[str, Any]:
            return {
                "courseid": "549",
                "userid": "42",
                "attemptref": str(uuid.uuid4()),
                "snapshotref": str(uuid.uuid4()),
                "snapshotsha256": hashlib.sha256(manifest_json.encode()).hexdigest(),
                "manifestjson": manifest_json,
            }

    async with httpx.AsyncClient() as client:
        with pytest.raises(IntegrationProtocolError, match="attempt ownership"):
            await recover_attempt_checkpoint(
                session_factory,
                settings,
                attempt_id=attempt_id,
                bridge_factory=lambda *_args: FakeBridge(),
                client=client,
            )


async def test_course_sync_scheduler_does_not_duplicate_open_event(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )

    first = await run_scheduler_iteration(session_factory, settings, now=now)
    second = await run_scheduler_iteration(session_factory, settings, now=now)

    async with session_factory() as db:
        count = await db.scalar(
            select(func.count(SyncOutbox.id)).where(SyncOutbox.event_type == "course.sync")
        )
    assert first.courses_enqueued == 1
    assert second.courses_enqueued == 0
    assert count == 1


async def test_course_sync_scheduler_preserves_fresh_foreground_marker_and_recovers_stale_one(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC).replace(microsecond=0)
    _, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        course = await db.get(Course, course_id)
        assert course is not None
        course.sync_status = "SYNCING"
        course.updated_at = now

    fresh = await run_scheduler_iteration(session_factory, settings, now=now)
    async with session_factory() as db:
        course = await db.get(Course, course_id)
        event_count = await db.scalar(
            select(func.count(SyncOutbox.id)).where(
                SyncOutbox.course_id == course_id,
                SyncOutbox.event_type == "course.sync",
            )
        )
    assert fresh.courses_enqueued == 0
    assert course is not None and course.sync_status == "SYNCING"
    assert event_count == 0

    async with session_factory() as db, db.begin():
        course = await db.get(Course, course_id)
        assert course is not None
        course.updated_at = course_sync_stale_before(settings, now) - timedelta(seconds=1)
        bucket = int(now.timestamp()) // settings.sync_course_interval_seconds
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                event_type="course.sync",
                aggregate_type="Course",
                aggregate_id=course.id,
                idempotency_key=f"course-sync:{course.id}:{bucket}",
                payload={"course_id": course.external_id},
                state=SyncOutboxState.DELIVERED.value,
                delivered_at=now,
            )
        )

    recovered = await run_scheduler_iteration(session_factory, settings, now=now)
    async with session_factory() as db:
        course = await db.get(Course, course_id)
        event_count = await db.scalar(
            select(func.count(SyncOutbox.id)).where(
                SyncOutbox.course_id == course_id,
                SyncOutbox.event_type == "course.sync",
            )
        )
    assert recovered.courses_enqueued == 1
    assert course is not None and course.sync_status == "PENDING"
    assert event_count == 2


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("event_chain_head", "f" * 64),
        ("epoch", 2),
        ("workspace_revision", 999),
    ],
)
async def test_checkpoint_delivery_blocks_tampered_external_anchors(
    app_bundle,
    field: str,
    tampered: str | int,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        event = await enqueue_checkpoint(db, attempt=attempt, snapshot=snapshot, reason="PERIODIC")
        event.payload = {**event.payload, field: tampered}
        event_id = event.id

    def bridge_factory(*_args):
        raise AssertionError("tampered checkpoint must not contact Moodle")

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=bridge_factory,
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.BLOCKED.value
    assert event.last_error.startswith("CHECKPOINT_CONTEXT_MISMATCH:")


async def _local_checkpoint(session_factory, attempt_id: uuid.UUID) -> dict[str, Any]:
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt_id))
        assert attempt is not None and workspace is not None
        snapshot = await create_snapshot(db, workspace, "PERIODIC")
        manifest_json = canonical_json(snapshot.files)
        return {
            "courseid": "549",
            "userid": "42",
            "attemptref": str(attempt_id),
            "snapshotref": str(snapshot.id),
            "snapshotsha256": snapshot.manifest_hash,
            "eventchainhead": snapshot.event_chain_head,
            "epoch": attempt.epoch,
            "workspacerevision": snapshot.revision,
            "reason": snapshot.reason,
            "manifestjson": manifest_json,
        }


async def test_recovery_returns_verified_external_anchors(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=datetime.now(UTC),
        deadline=datetime.now(UTC) + timedelta(minutes=10),
    )
    checkpoint = await _local_checkpoint(session_factory, attempt_id)

    class FakeBridge:
        async def get_latest_checkpoint(self, **_kwargs) -> dict[str, Any]:
            return checkpoint

    async with httpx.AsyncClient() as client:
        recovered = await recover_attempt_checkpoint(
            session_factory,
            settings,
            attempt_id=attempt_id,
            bridge_factory=lambda *_args: FakeBridge(),
            client=client,
        )

    assert recovered is not None
    assert recovered.snapshot_ref == checkpoint["snapshotref"]
    assert recovered.event_chain_head == checkpoint["eventchainhead"]
    assert recovered.epoch == checkpoint["epoch"]
    assert recovered.workspace_revision == checkpoint["workspacerevision"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("eventchainhead", "a" * 64, "event chain head does not match"),
        ("epoch", 2, "attempt epoch does not match"),
        ("workspacerevision", 999, "workspace revision does not match"),
    ],
)
async def test_recovery_rejects_tampered_external_anchors(
    app_bundle,
    field: str,
    value: str | int,
    message: str,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC)
    attempt_id, _ = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(minutes=10),
    )
    checkpoint = await _local_checkpoint(session_factory, attempt_id)
    checkpoint = {**checkpoint, field: value}

    class FakeBridge:
        async def get_latest_checkpoint(self, **_kwargs) -> dict[str, Any]:
            return checkpoint

    async with httpx.AsyncClient() as client:
        with pytest.raises(IntegrationProtocolError, match=message):
            await recover_attempt_checkpoint(
                session_factory,
                settings,
                attempt_id=attempt_id,
                bridge_factory=lambda *_args: FakeBridge(),
                client=client,
            )


async def _seed_task_mirror_event(session_factory) -> tuple[uuid.UUID, uuid.UUID, dict[str, Any]]:
    async with session_factory() as db, db.begin():
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={"auth_mode": "BRIDGE"},
        )
        db.add(connection)
        await db.flush()
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="7",
            display_name="Teacher",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add_all([principal, course])
        await db.flush()
        item = TaskBankItem(
            course_id=course.id,
            scope="COURSE",
            slug="sum-two",
            category="Basics",
            tags=["io"],
            created_by_id=principal.id,
        )
        db.add(item)
        await db.flush()
        semantic = {
            "title": "A + B",
            "statement": "Read two integers and print their sum.",
            "language": "CPP",
            "language_standard": "C++20",
            "multi_file": False,
            "starter_files": [{"path": "main.cpp", "content": "int main() {}\n"}],
            "build_profile": "cpp-gcc-c++20-single",
            "public_examples": [{"stdin": "1 2\n", "stdout": "3\n"}],
            "hidden_test_manifest": {"count": 4},
            "max_score": "10.00",
            "difficulty": "easy",
            "ai_policy": {"student": "explain_only"},
        }
        version = TaskVersion(
            item_id=item.id,
            number=1,
            content_hash=canonical_hash(semantic),
            status="PUBLISHED",
            authored_by_id=principal.id,
            **{key: value for key, value in semantic.items() if key != "max_score"},
            max_score=Decimal(semantic["max_score"]),
        )
        db.add(version)
        await db.flush()
        definition = {
            "schema_version": 1,
            "task_ref": str(item.id),
            "slug": item.slug,
            "category": item.category,
            "tags": item.tags,
            "version": version.number,
            "content_hash": version.content_hash,
            **semantic,
        }
        definition_json = canonical_json(definition)
        payload = {
            "course_id": course.external_id,
            "task_ref": str(item.id),
            "version": version.number,
            "content_hash": version.content_hash,
            "definition_sha256": sha256_text(definition_json),
            "definition_json": definition_json,
            "status": version.status,
        }
        event = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="task.version",
            aggregate_type="TaskVersion",
            aggregate_id=version.id,
            idempotency_key=f"task-test:{version.id}",
            payload=payload,
        )
        db.add(event)
        await db.flush()
        return event.id, version.id, payload


async def test_task_version_delivery_rebuilds_payload_and_records_mapping(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    event_id, version_id, expected_payload = await _seed_task_mirror_event(session_factory)

    class FakeBridge:
        async def upsert_task_definition(
            self,
            payload: dict[str, Any],
            idempotency_key: str,
        ) -> dict[str, Any]:
            assert payload == expected_payload
            assert idempotency_key.startswith("task-test:")
            return {
                "status": "DELIVERED",
                "receipt": {
                    "status": "MIRRORED",
                    "mirrorid": 321,
                    "timecreated": 1,
                    "timemodified": 1,
                },
            }

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: FakeBridge(),
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.local_type == "TaskVersion",
                ExternalMapping.local_id == version_id,
            )
        )
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert mapping is not None
    assert mapping.external_type == "local_programming_bridge_task"
    assert mapping.external_id == "321"
    assert mapping.external_revision == expected_payload["definition_sha256"]
    assert mapping.metadata_json["receipt"]["receipt"]["status"] == "MIRRORED"


async def test_task_version_delivery_blocks_tampered_outbox_payload(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    event_id, _, _ = await _seed_task_mirror_event(session_factory)
    async with session_factory() as db, db.begin():
        event = await db.get(SyncOutbox, event_id)
        assert event is not None
        event.payload = {**event.payload, "definition_sha256": "0" * 64}

    def bridge_factory(*_args):
        raise AssertionError("tampered task payload must not contact Moodle")

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=bridge_factory,
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
    assert event is not None and event.state == SyncOutboxState.BLOCKED.value
    assert event.last_error.startswith("TASK_VERSION_CONTEXT_MISMATCH:")


async def test_playwright_course_sync_skips_teacher_without_browser_session(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, credential_id, _, actor_external_subject = await _seed_playwright_course_sync(
        session_factory,
        settings,
        missing_teacher_first=True,
    )
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json=_course_discovery_response(actor_external_subject),
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        assert await process_outbox_once(session_factory, settings, client=client)

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        credential = await db.get(MoodleCredential, credential_id)
    assert captured["actor_external_subject"] == actor_external_subject
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert credential is not None and credential.revision == 2
    assert credential.lease_owner is None and credential.lease_expires_at is None


async def test_busy_browser_credential_retries_course_sync_without_visible_failure(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, credential_id, _, _ = await _seed_playwright_course_sync(
        session_factory,
        settings,
    )
    async with session_factory() as db, db.begin():
        credential = await db.get(MoodleCredential, credential_id)
        assert credential is not None
        credential.lease_owner = "history-import-worker"
        credential.lease_expires_at = datetime.now(UTC) + timedelta(minutes=2)

    assert await process_outbox_once(session_factory, settings)

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        assert event is not None and event.course_id is not None
        course = await db.get(Course, event.course_id)
    assert event.state == SyncOutboxState.RETRY.value
    assert event.attempts == 0
    assert event.last_error.startswith("BROWSER_BUSY:")
    assert timedelta(seconds=5) <= event.next_attempt_at - event.last_attempt_at < timedelta(
        seconds=6
    )
    assert course is not None and course.sync_status == "SYNCING"
    assert course.sync_error_code == ""
    assert course.sync_error_message == ""
    assert course.sync_error_at is None
    assert course.sync_error_retryable is False


async def test_playwright_lease_is_released_when_http_client_creation_fails(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, credential_id, _, _ = await _seed_playwright_course_sync(
        session_factory,
        settings,
    )

    def failing_client_factory() -> httpx.AsyncClient:
        raise RuntimeError("client construction failed")

    assert await process_outbox_once(
        session_factory,
        settings,
        client_factory=failing_client_factory,
    )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        credential = await db.get(MoodleCredential, credential_id)
    assert event is not None and event.state == SyncOutboxState.RETRY.value
    assert credential is not None
    assert credential.lease_owner is None and credential.lease_expires_at is None


async def test_playwright_client_close_failure_does_not_lose_delivery_or_lease(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, credential_id, _, actor_external_subject = await _seed_playwright_course_sync(
        session_factory, settings
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_course_discovery_response(actor_external_subject),
        )

    class CloseFailingClient(httpx.AsyncClient):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("client close failed")

    def client_factory() -> httpx.AsyncClient:
        return CloseFailingClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    assert await process_outbox_once(
        session_factory,
        settings,
        client_factory=client_factory,
    )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        credential = await db.get(MoodleCredential, credential_id)
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert credential is not None and credential.revision == 2
    assert credential.lease_owner is None and credential.lease_expires_at is None


async def test_playwright_cancellation_releases_browser_lease(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    _, credential_id, _, _ = await _seed_playwright_course_sync(
        session_factory,
        settings,
    )
    request_started = asyncio.Event()
    never_responds = asyncio.Event()

    async def handler(_: httpx.Request) -> httpx.Response:
        request_started.set()
        await never_responds.wait()
        raise AssertionError("unreachable")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=False
    ) as client:
        worker = asyncio.create_task(process_outbox_once(session_factory, settings, client=client))
        await asyncio.wait_for(request_started.wait(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker

    async with session_factory() as db:
        credential = await db.get(MoodleCredential, credential_id)
    assert credential is not None
    assert credential.lease_owner is None and credential.lease_expires_at is None


async def test_worker_course_discovery_projects_activities_and_updates_mapped_deadlines(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    now = datetime.now(UTC).replace(microsecond=0)
    attempt_id, course_id = await _seed_attempt(
        session_factory,
        now=now,
        deadline=now + timedelta(hours=4),
    )
    opens_at = now + timedelta(minutes=5)
    due_at = now + timedelta(hours=3)
    cutoff_at = now + timedelta(hours=2)
    old_missing_close = now + timedelta(days=2)
    async with session_factory() as db, db.begin():
        attempt = await db.get(Attempt, attempt_id)
        course = await db.get(Course, course_id)
        assert attempt is not None and course is not None
        principal = await db.get(ExternalPrincipal, attempt.principal_id)
        assert principal is not None
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role="TEACHER",
            )
        )
        await _grant_teacher(db, principal)
        attempt.expected_end_at = now + timedelta(hours=5)
        mapped = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=attempt.assessment_id,
            external_type="mod_assign",
            external_id="777",
            metadata_json={"module": "assign", "cmid": 777, "sync_deadlines": True},
        )
        missing_assessment = Assessment(
            course_id=course.id,
            title="Missing assignment",
            closes_at=old_missing_close,
            created_by_id=principal.id,
            status="PUBLISHED",
        )
        db.add(missing_assessment)
        await db.flush()
        missing = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=missing_assessment.id,
            external_type="mod_assign",
            external_id="888",
            metadata_json={
                "module": "assign",
                "cmid": 888,
                "sync_deadlines": True,
                "activity": {"cmid": 888, "name": "Last known"},
            },
        )
        quiz_assessment = Assessment(
            course_id=course.id,
            title="Quiz essay",
            max_score=Decimal("10.00"),
            created_by_id=principal.id,
            status="PUBLISHED",
        )
        db.add(quiz_assessment)
        await db.flush()
        quiz_mapping = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=quiz_assessment.id,
            external_type="mod_quiz",
            external_id="779",
            metadata_json={
                "module": "quiz",
                "cmid": 779,
                "submission_mode": "ESSAY_ATTACHMENT",
                "sync_deadlines": True,
            },
        )
        event = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="course.sync",
            aggregate_type="Course",
            aggregate_id=course.id,
            idempotency_key=f"course-test:{course.id}",
            payload={"course_id": course.external_id},
        )
        db.add_all([mapped, missing, quiz_mapping, event])
        await db.flush()
        event_id = event.id
        assessment_id = attempt.assessment_id
        missing_assessment_id = missing_assessment.id
        quiz_assessment_id = quiz_assessment.id

    discovery = CourseDiscovery(
        external_id="549",
        preview={
            "external_id": "549",
            "title": "C++ synchronized",
            "short_name": "CPP",
            "external_revision": "course-revision-2",
            "membership_revision": "membership-revision-2",
            "starts_at_epoch": 0,
            "ends_at_epoch": 0,
            "sections": [
                {
                    "external_id": "10",
                    "title": "Exam",
                    "position": 1,
                    "visible": True,
                    "activities": [
                        {
                            "cmid": 777,
                            "instance_id": 70,
                            "module": "assign",
                            "name": "Programming exam",
                            "description": "Solve the assignment.",
                            "visible": True,
                            "uservisible": True,
                            "url": "https://moodle.example.test/mod/assign/view.php?id=777",
                            "opens_at": int(opens_at.timestamp()),
                            "due_at": int(due_at.timestamp()),
                            "cutoff_at": int(cutoff_at.timestamp()),
                            "grade_max": 10.0,
                            "attempt_limit": 1,
                            "title_confirmed": True,
                            "settings_confirmed": True,
                            "statement_confirmed": True,
                            "schedule_confirmed": True,
                            "duration_confirmed": True,
                            "grade_confirmed": True,
                            "attempt_policy_confirmed": True,
                        },
                        {
                            "cmid": 779,
                            "instance_id": 71,
                            "module": "quiz",
                            "name": "Independent work (Essay)",
                            "description": "Solve the Essay.",
                            "visible": True,
                            "uservisible": True,
                            "url": "https://moodle.example.test/mod/quiz/view.php?id=779",
                            "opens_at": int(opens_at.timestamp()),
                            "due_at": int(due_at.timestamp()),
                            "cutoff_at": 0,
                            "grade_max": 100.0,
                            "attempt_limit": 1,
                            "quiz_grading_method": "LAST",
                            "quiz_grading_method_confirmed": True,
                            "question_count": 1,
                            "essay_question_count": 1,
                            "import_supported": True,
                            "answer_transport": "ESSAY_ATTACHMENT",
                            "title_confirmed": True,
                            "settings_confirmed": True,
                            "statement_confirmed": True,
                            "schedule_confirmed": True,
                            "duration_confirmed": True,
                            "grade_confirmed": True,
                            "attempt_policy_confirmed": True,
                        },
                    ],
                }
            ],
            "membership_snapshot": {
                "members": [
                    {
                        "user_id": "42",
                        "display_name": "Teacher",
                        "role": "TEACHER",
                        "suspended": False,
                        "groups": [],
                    }
                ]
            },
        },
        capabilities={"roster": True},
    )

    class FakeBridge:
        async def discover_course(self, external_id: str, actor: str) -> CourseDiscovery:
            assert external_id == "549" and actor == "42"
            return discovery

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: FakeBridge(),
            client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        course = await db.get(Course, course_id)
        assessment = await db.get(Assessment, assessment_id)
        missing_assessment = await db.get(Assessment, missing_assessment_id)
        quiz_assessment = await db.get(Assessment, quiz_assessment_id)
        mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.local_id.in_(
                            [assessment_id, missing_assessment_id, quiz_assessment_id]
                        )
                    )
                )
            ).all()
        )
        attempt = await db.get(Attempt, attempt_id)
    assert event is not None and event.state == SyncOutboxState.DELIVERED.value
    assert course is not None and len(course.policies["lms_activities"]) == 2
    assert course.policies["lms_activity_revision"] == "course-revision-2"
    assert assessment is not None and assessment.opens_at.replace(tzinfo=UTC) == opens_at
    assert assessment.closes_at.replace(tzinfo=UTC) == cutoff_at
    assert attempt is not None and attempt.deadline_at.replace(tzinfo=UTC) == cutoff_at
    by_local = {mapping.local_id: mapping for mapping in mappings}
    assert by_local[assessment_id].metadata_json["sync_state"] == "ANSWER_TRANSPORT_UNSUPPORTED"
    assert by_local[assessment_id].metadata_json["submission_mode"] == "REQUIRES_CONFIGURATION"
    assert missing_assessment is not None
    assert missing_assessment.closes_at.replace(tzinfo=UTC) == old_missing_close
    assert by_local[missing_assessment_id].metadata_json["sync_state"] == "MISSING_IN_MOODLE"
    assert by_local[missing_assessment_id].metadata_json["activity"]["name"] == "Last known"
    assert quiz_assessment is not None
    assert quiz_assessment.opens_at.replace(tzinfo=UTC) == opens_at
    assert quiz_assessment.closes_at.replace(tzinfo=UTC) == due_at
    assert by_local[quiz_assessment_id].metadata_json["sync_state"] == "CURRENT"
    assert by_local[quiz_assessment_id].metadata_json["submission_mode"] == "ESSAY_ATTACHMENT"
