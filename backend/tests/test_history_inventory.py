from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.api.reviews import list_submissions
from app.auth.context import AuthContext
from app.core.credential_crypto import decrypt_moodle_browser_state
from app.integrations.errors import IntegrationProtocolError, IntegrationUnavailable
from app.models.attempts import Submission
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.identity import ExternalPrincipal, MoodleCredential
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment
from app.services.manual_sync import assessment_sync_statuses, queue_manual_assessment_sync
from app.services.moodle_history import (
    enqueue_historical_answer_reads,
    enqueue_historical_submission_imports,
)
from app.services.review import claim_submission
from app.services.sync import (
    _BrowserDeliveryResult,
    claim_next_outbox_event,
    process_outbox_once,
    run_scheduler_iteration,
)
from tests.test_moodle_history import _browser_state, _finished_item, _seed_history_target


def ref(attempt="9001", user="77"):
    return {
        "attempt_id": attempt,
        "user_id": user,
        "display_name": f"Student {user}",
        "state": "SUBMITTED",
        "submitted_at_epoch": 0,
        "grade": None,
        "grade_max": None,
    }


async def test_general_lane_leaves_history_to_reserved_lanes_and_answers_precede_probes(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        # Enqueue the probe first: ordinary FIFO/priority_only ordering used
        # to put every old-attempt probe ahead of reviewable student answers.
        for kind, payload in (
            ("probe", {"scan_only": True, "probe_only": True, "priority_only": True}),
            ("answer", {"attempt_refs": [ref()], "priority_only": False}),
            ("inventory", {"scan_only": True, "priority_only": True}),
        ):
            db.add(SyncOutbox(
                connection_id=ids["connection_id"], course_id=ids["course_id"],
                aggregate_id=ids["assessment_id"], aggregate_type="Assessment",
                event_type="moodle.history.import", idempotency_key=kind, payload=payload,
            ))
    assert await claim_next_outbox_event(sessions, settings, exclude_history_imports=True) is None
    for expected in ("inventory", "answer", "probe"):
        claim = await claim_next_outbox_event(sessions, settings, history_imports_only=True)
        assert claim is not None and claim.idempotency_key == expected


async def seed_roster(db, ids):
    group = CourseGroup(course_id=ids["course_id"], external_id="group1", name="Teaching group")
    db.add(group)
    await db.flush()
    teacher = await db.scalar(select(CourseMembership).where(
        CourseMembership.principal_id == ids["teacher_id"],
    ))
    db.add(CourseMembershipGroup(coursemembership_id=teacher.id, coursegroup_id=group.id))
    for subject in ("77", "78", "79"):
        student = ExternalPrincipal(
            connection_id=ids["connection_id"], external_subject=subject,
            display_name=f"Student {subject}",
        )
        db.add(student)
        await db.flush()
        membership = CourseMembership(
            course_id=ids["course_id"], principal_id=student.id, role="STUDENT",
        )
        db.add(membership)
        await db.flush()
        if subject != "79":  # This student is outside the teacher's group.
            db.add(CourseMembershipGroup(
                coursemembership_id=membership.id, coursegroup_id=group.id,
            ))


async def test_teacher_can_review_committed_answer_while_siblings_are_pending_or_failed(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    candidates = [ref(str(9001 + i), str(77 + i)) for i in range(3)]
    async with sessions() as db, db.begin():
        await seed_roster(db, ids)
        await queue_manual_assessment_sync(
            db, course=await db.get(Course, ids["course_id"]),
            assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42",
        )

    class Connector:
        async def discover_historical_submissions(self, payload):
            if payload.get("scan_only"):
                value = {"items": [], "scan_only": True, "candidates": candidates}
            else:
                candidate = payload["attempt_refs"][0]
                if candidate["user_id"] == "78":
                    raise IntegrationProtocolError("One student's answer is unreadable")
                item = {**_finished_item(), **candidate,
                        "external_id": f"quiz:777:{candidate['attempt_id']}"}
                item["responses"][0]["grade"] = None
                item["responses"][0]["comment"] = ""
                value = {"items": [item]}
            return _BrowserDeliveryResult(
                value={**value, "complete": True, "next_cursor": None, "warnings": []},
                storage_state=_browser_state("after"),
            )

    async def process():
        assert await process_outbox_once(
            sessions, settings, bridge_factory=lambda *_: Connector(), history_imports_only=True,
        )

    await process()  # inventory
    await process()  # first student's answer is committed
    teacher_auth = AuthContext(ids["teacher_id"], "Teacher", uuid.uuid4(), "test", ("TEACHER",), ())
    async with sessions() as db, db.begin():
        status = (await assessment_sync_statuses(db, course_id=ids["course_id"]))[0]
        assert status["status"] == "SYNCING"
        queue = await list_submissions("all", teacher_auth, db)
        assert len(queue) == 1 and queue[0].can_review
        claim = await claim_submission(db, submission_id=queue[0].id, teacher_id=ids["teacher_id"])
        assert claim.owner_id == ids["teacher_id"]
    await process()  # second student's detail fails
    await process()  # an out-of-group answer is imported but remains private
    async with sessions() as db:
        assert await db.scalar(select(func.count(Submission.id))) == 2
        statuses = await assessment_sync_statuses(db, course_id=ids["course_id"])
        assert statuses[0]["status"] == "FAILED"
        queue = await list_submissions("all", teacher_auth, db)
        assert len(queue) == 1 and queue[0].can_review


async def test_inventory_pins_details_and_failure_does_not_block_other_students(
    app_bundle,
):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    candidates = [ref(str(9000 + i), str(70 + i)) for i in range(1, 4)]
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        await enqueue_historical_submission_imports(db, course=course, actor_external_subject="42")

    class Connector:
        async def discover_historical_submissions(self, payload):
            if payload.get("scan_only"):
                return _BrowserDeliveryResult(
                    value={
                        "items": [],
                        "candidates": candidates,
                        "scan_only": True,
                        "complete": True,
                        "next_cursor": None,
                        "warnings": [],
                    },
                    storage_state=_browser_state("after"),
                )
            assert len(payload["attempt_refs"]) == 1 and not payload.get("known_attempt_ids")
            candidate = payload["attempt_refs"][0]
            if candidate["attempt_id"] == "9001":
                raise IntegrationUnavailable("One Moodle file timed out")
            item = {
                **_finished_item(),
                **candidate,
                "external_id": f"quiz:777:{candidate['attempt_id']}",
            }
            return _BrowserDeliveryResult(
                value={"items": [item], "complete": True, "next_cursor": None, "warnings": []},
                storage_state=_browser_state("after"),
            )

    def factory(*_):
        return Connector()
    assert await process_outbox_once(
        sessions, settings, bridge_factory=factory, history_imports_only=True
    )
    async with sessions() as db:
        assert (
            await db.scalar(select(func.count(Submission.id))) == 0
        )  # No fabricated empty answers.
        rows = (await db.scalars(select(SyncOutbox))).all()
        assert len(rows) == 4
        assert next(row for row in rows if row.state == "DELIVERED").receipt["answers_queued"] == 3
        assert all(row.payload.get("attempt_refs") for row in rows if row.state == "PENDING")
    for _ in range(3):
        assert await process_outbox_once(
            sessions, settings, bridge_factory=factory, history_imports_only=True
        )
    async with sessions() as db, db.begin():
        assert await db.scalar(select(func.count(Submission.id))) == 2
        rows = (await db.scalars(select(SyncOutbox))).all()
        assert len([row for row in rows if row.state == "RETRY"]) == 1
        assert len([row for row in rows if row.state == "DELIVERED"]) == 3
        # Repeated inventory neither duplicates active answers nor redownloads
        # unchanged, just-delivered code. No extra exhaustive pass is queued.
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert (
            await enqueue_historical_answer_reads(
                db,
                course=course,
                assessment=assessment,
                actor_external_subject="42",
                module="quiz",
                cmid=777,
                candidates=candidates,
                scan_id=rows[0].id,
            )
            == 0
        )


async def test_inventory_runs_while_teacher_credential_is_owned_by_course_sync(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        await enqueue_historical_submission_imports(db, course=course, actor_external_subject="42")
        credential = await db.scalar(select(MoodleCredential))
        credential.lease_owner = "course-worker"
        credential.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        revision = credential.revision

    class Connector:
        async def discover_historical_submissions(self, payload):
            return _BrowserDeliveryResult(
                value={
                    "scan_only": True,
                    "candidates": [],
                    "items": [],
                    "complete": True,
                    "next_cursor": None,
                },
                storage_state=_browser_state("late-reader"),
            )

    def factory(_settings, target, _client):
        assert target.browser_read_only and target.browser_lease_owner is None
        return Connector()

    assert await process_outbox_once(
        sessions, settings, bridge_factory=factory, history_imports_only=True
    )
    async with sessions() as db:
        credential = await db.scalar(select(MoodleCredential))
        assert credential.lease_owner == "course-worker" and credential.revision == revision
        assert decrypt_moodle_browser_state(
            credential.encrypted_secret,
            settings,
            connection_id=credential.connection_id,
            principal_id=credential.principal_id,
        ) == _browser_state("before")
        assert (await db.scalar(select(SyncOutbox))).state == "DELIVERED"


async def test_scheduler_never_enqueues_history_even_with_an_active_teacher_session(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        course.sync_status = "CURRENT"
    for delay in (0, 180, 86400):
        await run_scheduler_iteration(
            sessions, settings, now=datetime.now(UTC) + timedelta(seconds=delay)
        )
    async with sessions() as db:
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0
