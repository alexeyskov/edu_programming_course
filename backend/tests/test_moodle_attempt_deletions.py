from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.integrations.errors import IntegrationProtocolError
from app.models.attempts import Attempt, Snapshot, Submission, WorkspaceFile
from app.models.courses import Course
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ReviewDecision
from app.models.tasks import Assessment
from app.services.common import DomainError
from app.services.moodle_attempt_reconciliation import (
    known_quiz_attempt_probe_batch,
    reconcile_deleted_quiz_attempts,
)
from app.services.moodle_attempt_selection import (
    is_latest_completed_moodle_attempt,
    latest_reviewable_moodle_submission_ids,
)
from app.services.moodle_history import (
    enqueue_historical_submission_imports,
    materialize_historical_submissions,
)
from app.services.moodle_quiz_runtime import resolve_moodle_quiz_context
from app.services.policy import visible_submission_ids_for_review
from app.services.review import claim_submission, finalize_review
from app.services.sync import (
    ClaimedOutboxEvent,
    _BrowserDeliveryResult,
    _owned_claim,
    process_outbox_once,
)
from app.services.workspace import start_attempt
from tests.test_attempt_review_services import _multi_quiz_preparation, _start_multi_quiz
from tests.test_moodle_history import _browser_state, _multi_essay_item, _seed_history_target


def _item(remote_id: str):
    return {
        **_multi_essay_item(revision=f"revision-{remote_id}"),
        "attempt_id": remote_id,
        "external_id": f"quiz:777:{remote_id}",
    }


async def _rows(db):
    return list(
        (
            await db.execute(
                select(Submission, Attempt, Assessment)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
            )
        ).all()
    )


async def test_confirmed_deletion_retires_all_siblings_fences_exports_preserves_sources(app_bundle):
    _, factory, _ = app_bundle
    ids = await _seed_history_target(factory)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        detail = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=assessment.id,
            idempotency_key="deleted-pinned-detail",
            state="PROCESSING",
            locked_at=utcnow(),
            payload={"module": "quiz", "cmid": 777, "detail_key": "124"},
        )
        db.add(detail)
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("123"), _item("124")],
        )
        before = await _rows(db)
        deleted_rows = [
            row for row in before if row[0].external_receipt["moodle_parent_attempt_id"] == "124"
        ]
        kept_rows = [row for row in before if row not in deleted_rows]
        native = Attempt(
            assessment_id=assessment.id,
            principal_id=deleted_rows[0][1].principal_id,
            assigned_task_version_id=deleted_rows[0][1].assigned_task_version_id,
            sequence=20,
            state="ACTIVE",
            integrity_policy={"moodle_attempt_id": "124", "moodle_cmid": 777},
        )
        db.add(native)
        await db.flush()
        pending = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=native.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key="deleted-native-checkpoint",
            payload={},
            state="PROCESSING",
            attempts=1,
            last_attempt_at=utcnow(),
            locked_at=utcnow(),
        )
        delivered = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=native.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key="delivered-native-checkpoint",
            payload={},
            state="DELIVERED",
            delivered_at=utcnow(),
        )
        decision = await db.scalar(
            select(ReviewDecision).where(
                ReviewDecision.submission_id == deleted_rows[0][0].id,
            )
        )
        decision.lms_export_state = "PENDING"
        grade = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=decision.id,
            idempotency_key="deleted-grade",
            payload={},
            state="PENDING",
        )
        db.add_all([pending, delivered, grade])
        await db.flush()
        old_claim = ClaimedOutboxEvent(
            id=pending.id,
            event_type=pending.event_type,
            aggregate_type=pending.aggregate_type,
            aggregate_id=pending.aggregate_id,
            connection_id=pending.connection_id,
            course_id=pending.course_id,
            attempt_id=pending.attempt_id,
            idempotency_key=pending.idempotency_key,
            payload=pending.payload,
            attempts=pending.attempts,
            locked_at=pending.last_attempt_at,
        )
        files_before = [
            (file.id, file.content) for file in (await db.scalars(select(WorkspaceFile))).all()
        ]
        snapshots_before = [
            (snapshot.id, snapshot.files) for snapshot in (await db.scalars(select(Snapshot))).all()
        ]

        assert (
            await reconcile_deleted_quiz_attempts(
                db,
                course=course,
                assessment=assessment,
                cmid=777,
                actor_external_subject="42",
                known_attempt_ids=["123", "124"],
                deleted_attempt_ids=["124"],
            )
            == 3
        )
        assert native.state == "VOID" and native.submission_source == "MOODLE_DELETED"
        await db.refresh(detail)
        assert detail.state == "DELIVERED" and detail.locked_at is None
        assert detail.receipt["status"] == "REMOTE_DELETED" and not detail.last_error
        assert pending.state == grade.state == "BLOCKED"
        assert pending.locked_at is None and "MOODLE_ATTEMPT_DELETED" in pending.last_error
        assert await _owned_claim(db, old_claim) is None
        assert delivered.state == "DELIVERED" and delivered.delivered_at is not None
        assert decision.lms_export_state == "BLOCKED"
        assert all(attempt.state == "VOID" for _, attempt, _ in deleted_rows)
        assert all(attempt.state == "SUBMITTED" for _, attempt, _ in kept_rows)
        assert await latest_reviewable_moodle_submission_ids(db, await _rows(db)) == {
            submission.id for submission, _, _ in kept_rows
        }
        assert not await is_latest_completed_moodle_attempt(
            db,
            submission=deleted_rows[0][0],
            attempt=deleted_rows[0][1],
            assessment=deleted_rows[0][2],
        )
        visible = await visible_submission_ids_for_review(
            db,
            principal_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        assert visible == {submission.id for submission, _, _ in kept_rows}
        assert [
            (file.id, file.content) for file in (await db.scalars(select(WorkspaceFile))).all()
        ] == files_before
        assert [
            (snapshot.id, snapshot.files) for snapshot in (await db.scalars(select(Snapshot))).all()
        ] == snapshots_before

        # A delayed old report cannot revive the deleted latest marker or code.
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("124")],
        )
        assert await latest_reviewable_moodle_submission_ids(db, await _rows(db)) == visible
        # A genuinely new Moodle attempt for the same student remains valid.
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("125")],
        )
        selected = await latest_reviewable_moodle_submission_ids(db, await _rows(db))
        assert len(selected) == 2
        assert all(
            submission.external_receipt["moodle_parent_attempt_id"] == "125"
            for submission, _, _ in await _rows(db)
            if submission.id in selected
        )


async def test_empty_complete_or_partial_history_never_infers_deletion(app_bundle):
    _, factory, settings = app_bundle
    ids = await _seed_history_target(factory, settings=settings)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("123")],
        )
        await enqueue_historical_submission_imports(db, course=course, actor_external_subject="42")

    class EmptyBridge:
        async def discover_historical_submissions(self, payload):
            assert payload["known_attempt_ids"] == ["123"]
            return _BrowserDeliveryResult(
                value={
                    "items": [],
                    "complete": True,
                    "next_cursor": None,
                    "warnings": [],
                },
                storage_state=_browser_state("after"),
            )

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            factory, settings, client=client, bridge_factory=lambda *_: EmptyBridge()
        )
    async with factory() as db:
        assert all(attempt.state == "SUBMITTED" for _, attempt, _ in await _rows(db))


async def test_history_worker_applies_only_confirmed_exact_deletion(app_bundle):
    _, factory, settings = app_bundle
    ids = await _seed_history_target(factory, settings=settings)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("123"), _item("124")],
        )
        await enqueue_historical_submission_imports(db, course=course, actor_external_subject="42")

    class DeletedBridge:
        async def discover_historical_submissions(self, payload):
            assert payload["known_attempt_ids"] == ["123", "124"]
            return _BrowserDeliveryResult(
                value={
                    "items": [_item("124")],
                    "complete": False,
                    "next_cursor": "0:1",
                    "warnings": [],
                    "deleted_attempt_ids": ["123"],
                },
                storage_state=_browser_state("after"),
            )

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            factory, settings, client=client, bridge_factory=lambda *_: DeletedBridge()
        )
    async with factory() as db:
        rows = await _rows(db)
        assert len(rows) == 4
        for submission, attempt, _ in rows:
            assert attempt.state == (
                "VOID"
                if submission.external_receipt["moodle_parent_attempt_id"] == "123"
                else "SUBMITTED"
            )
        delivered = await db.scalar(select(SyncOutbox).where(SyncOutbox.state == "DELIVERED"))
        assert delivered.receipt["retired_attempts"] == 2
        assert delivered.receipt["warning_count"] == 0


async def test_probe_round_robin_visits_all_known_attempts_without_loading_code(app_bundle):
    _, factory, _ = app_bundle
    ids = await _seed_history_target(factory)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item(str(value)) for value in range(120, 132)],
        )
        mapping = await db.scalar(
            select(ExternalMapping).where(ExternalMapping.external_type == "mod_quiz")
        )
        batches = []
        for _ in range(3):
            batches.append(
                await known_quiz_attempt_probe_batch(
                    db,
                    course=course,
                    assessment=assessment,
                    mapping=mapping,
                    cmid=777,
                )
            )
        assert all(len(batch) == 5 for batch in batches)
        assert set().union(*map(set, batches)) == {str(value) for value in range(120, 132)}


async def test_probe_cursor_commits_between_actual_worker_requests(app_bundle):
    _, factory, settings = app_bundle
    ids = await _seed_history_target(factory, settings=settings)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item(str(value)) for value in range(120, 131)],
        )
        await enqueue_historical_submission_imports(db, course=course, actor_external_subject="42")
    probes = []

    class PagedBridge:
        async def discover_historical_submissions(self, payload):
            probes.append(payload["known_attempt_ids"])
            return _BrowserDeliveryResult(
                value={
                    "items": [],
                    "complete": len(probes) == 2,
                    "next_cursor": "0:5" if len(probes) == 1 else None,
                    "warnings": [],
                },
                storage_state=_browser_state("after"),
            )

    async with httpx.AsyncClient() as client:
        for _ in range(2):
            assert await process_outbox_once(
                factory, settings, client=client, bridge_factory=lambda *_: PagedBridge()
            )
    assert probes == [
        [str(value) for value in range(120, 125)],
        [str(value) for value in range(125, 130)],
    ]


async def test_deletion_scope_and_newer_marker_are_preserved(app_bundle):
    _, factory, _ = app_bundle
    ids = await _seed_history_target(factory)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("123"), _item("124")],
        )
        existing = (await _rows(db))[0][1]
        other = Assessment(
            course_id=course.id,
            title="Other quiz",
            created_by_id=ids["teacher_id"],
        )
        db.add(other)
        await db.flush()
        unrelated = Attempt(
            assessment_id=other.id,
            principal_id=existing.principal_id,
            assigned_task_version_id=existing.assigned_task_version_id,
            state="ACTIVE",
            integrity_policy={"moodle_attempt_id": "123", "moodle_cmid": 778},
        )
        db.add(unrelated)
        await db.flush()
        await reconcile_deleted_quiz_attempts(
            db,
            course=course,
            assessment=assessment,
            cmid=777,
            actor_external_subject="42",
            known_attempt_ids=["123"],
            deleted_attempt_ids=["123"],
        )
        assert unrelated.state == "ACTIVE"
        rows = await _rows(db)
        selected = await latest_reviewable_moodle_submission_ids(db, rows)
        assert len(selected) == 2
        assert all(
            submission.external_receipt["moodle_parent_attempt_id"] == "124"
            for submission, _, _ in rows
            if submission.id in selected
        )
        marker = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == "moodle_attempt_observation",
            )
        )
        assert marker.metadata_json["remote_attempt_id"] == "124"
        assert not marker.metadata_json.get("deleted_in_moodle")


@pytest.mark.parametrize("action", ["claim", "finalize"])
async def test_review_refreshes_cached_attempt_after_remote_deletion(app_bundle, action):
    _, factory, _ = app_bundle
    ids = await _seed_history_target(factory)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_item("123")],
        )
    async with factory() as old_request:
        submission, cached_attempt, _ = (await _rows(old_request))[0]
        await old_request.commit()
        assert cached_attempt.state == "SUBMITTED"
        async with factory() as other, other.begin():
            course = await other.get(Course, ids["course_id"])
            assessment = await other.get(Assessment, ids["assessment_id"])
            await reconcile_deleted_quiz_attempts(
                other,
                course=course,
                assessment=assessment,
                cmid=777,
                actor_external_subject="42",
                known_attempt_ids=["123"],
                deleted_attempt_ids=["123"],
            )
        assert cached_attempt.state == "SUBMITTED"  # Stale identity-map entry.
        with pytest.raises(DomainError) as failure:
            if action == "claim":
                await claim_submission(
                    old_request,
                    submission_id=submission.id,
                    teacher_id=ids["teacher_id"],
                    allow_system_settings_read=True,
                )
            else:
                await finalize_review(
                    old_request,
                    submission_id=submission.id,
                    teacher_id=ids["teacher_id"],
                    grade=1,
                    comment="must not export",
                    allow_system_settings_read=True,
                )
        assert failure.value.status_code in {403, 404}
        assert cached_attempt.state == "VOID"


async def test_deleted_native_quiz_does_not_block_genuinely_new_attempt(db):
    student, _, assessment, old, questions = await _start_multi_quiz(db)
    course = await db.get(Course, assessment.course_id)
    await reconcile_deleted_quiz_attempts(
        db,
        course=course,
        assessment=assessment,
        cmid=30354,
        actor_external_subject="42",
        known_attempt_ids=["141716"],
        deleted_attempt_ids=["141716"],
    )
    assert old.state == "VOID"
    for question in questions:
        assert (await db.get(Attempt, question.attempt_id)).state == "VOID"
    # Admission resolves the activity before preparing the next Moodle attempt.
    # Calling start_attempt alone bypasses this check and misses marker collisions.
    context = await resolve_moodle_quiz_context(db, assessment.id)
    assert context is not None and context.cmid == 30354
    fresh = await start_attempt(
        db,
        assessment_id=assessment.id,
        principal_id=student.id,
        prepared_moodle_quiz=_multi_quiz_preparation(remote_attempt_id="141717"),
    )
    assert fresh.id != old.id and fresh.state == "ACTIVE"
    assert fresh.integrity_policy["moodle_attempt_id"] == "141717"


@pytest.mark.parametrize("evidence", [["999"], ["123", "999"], [123], "123", None])
async def test_unrequested_or_malformed_deletion_evidence_is_rejected(app_bundle, evidence):
    _, factory, _ = app_bundle
    ids = await _seed_history_target(factory)
    async with factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        with pytest.raises(IntegrationProtocolError):
            await reconcile_deleted_quiz_attempts(
                db,
                course=course,
                assessment=assessment,
                cmid=777,
                actor_external_subject="42",
                known_attempt_ids=["123"],
                deleted_attempt_ids=evidence,
            )
