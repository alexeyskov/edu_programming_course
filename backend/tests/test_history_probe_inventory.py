from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.db.base import utcnow
from app.integrations.errors import IntegrationProtocolError
from app.models.attempts import Attempt, Submission
from app.models.courses import Course
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment
from app.services.manual_sync import assessment_sync_statuses, queue_manual_assessment_sync
from app.services.moodle_history import enqueue_historical_answer_reads
from tests.test_history_inventory import ref
from tests.test_moodle_history import _seed_history_target


def _probe(ids, *, run="current-run", known_ids=None, payload_patch=None, **changes):
    return SyncOutbox(
        **{
            "connection_id": ids["connection_id"],
            "course_id": ids["course_id"],
            "aggregate_type": "Assessment",
            "aggregate_id": ids["assessment_id"],
            "event_type": "moodle.history.import",
            "idempotency_key": uuid.uuid4().hex,
            "payload": {
                "course_id": "549",
                "actor_external_subject": "42",
                "module": "quiz",
                "cmid": 777,
                "manual_run_id": run,
                "probe_only": True,
                "scan_only": True,
                "priority_only": True,
                "known_attempt_ids": known_ids if known_ids is not None else ["9001"],
                **(payload_patch or {}),
            },
            **changes,
        }
    )


async def _read_inventory(db, ids, *, candidates, run="current-run", module="quiz"):
    return await enqueue_historical_answer_reads(
        db,
        course=await db.get(Course, ids["course_id"]),
        assessment=await db.get(Assessment, ids["assessment_id"]),
        actor_external_subject="42",
        module=module,
        cmid=777,
        candidates=candidates,
        scan_id=uuid.uuid4(),
        manual_run_id=run,
    )


async def test_verified_inventory_eliminates_all_redundant_probe_calls_but_not_answer_reads(
    app_bundle,
):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        for number in range(13):
            db.add(Attempt(
                assessment_id=ids["assessment_id"], principal_id=ids["teacher_id"],
                sequence=number + 1, state="SUBMITTED",
                integrity_policy={"moodle_attempt_id": str(9000 + number)},
            ))
        await db.flush()
        course = await db.get(Course, ids["course_id"])
        await queue_manual_assessment_sync(
            db, course=course, assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42",
        )
        jobs = list((await db.scalars(select(SyncOutbox))).all())
        root = next(job for job in jobs if not job.payload.get("probe_only"))
        probes = [job for job in jobs if job.payload.get("probe_only")]
        assert len(probes) == 3
        root.state = "DELIVERED"
        root.delivered_at = utcnow()
        candidates = [ref(str(9000 + number), str(77 + number)) for number in range(13)]
        candidates.append(ref("9999", "99"))  # A genuinely new answer still needs hydration.
        assert await enqueue_historical_answer_reads(
            db, course=course, assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42", module="quiz", cmid=777,
            candidates=candidates, scan_id=root.id, manual_run_id=root.payload["manual_run_id"],
        ) == 14

        assert all(probe.state == "DELIVERED" for probe in probes)
        assert all(probe.payload["known_attempt_ids"] == [] for probe in probes)
        assert sum(probe.receipt["existence_confirmed_by_inventory"] for probe in probes) == 13
        assert all(probe.receipt["inventory_scan_id"] == str(root.id) for probe in probes)
        assert all(probe.attempts == 0 and probe.last_attempt_at is None for probe in probes)
        assert all(probe.delivered_at is not None for probe in probes)
        details = list((await db.scalars(select(SyncOutbox).where(
            SyncOutbox.state == "PENDING",
        ))).all())
        assert len(details) == 14
        assert all(
            job.payload.get("attempt_refs") and not job.payload.get("probe_only")
            for job in details
        )
        assert (await assessment_sync_statuses(db, course_id=course.id))[0]["status"] == "SYNCING"
        for detail in details:
            detail.state = "DELIVERED"
            detail.delivered_at = utcnow()
        await db.flush()
        assert (await assessment_sync_statuses(db, course_id=course.id))[0]["status"] == "COMPLETED"
        assert list((await db.scalars(select(Attempt.state))).all()) == ["SUBMITTED"] * 13
        assert await db.scalar(select(Submission.id)) is None


async def test_latest_only_report_preserves_unseen_old_attempts_for_exact_deletion_checks(
    app_bundle,
):
    _, sessions, _ = app_bundle
    ids = await _seed_history_target(sessions)
    async with sessions() as db, db.begin():
        probe = _probe(ids, known_ids=["8001", "9001", "9002"])
        db.add(probe)
        await db.flush()
        assert await _read_inventory(db, ids, candidates=[ref("9001"), ref("9999", "78")]) == 2
        assert probe.state == "PENDING"
        assert probe.payload["known_attempt_ids"] == ["8001", "9002"]
        assert probe.receipt == {} and probe.delivered_at is None
        # Re-reading a page neither duplicates answer jobs nor removes any
        # other old attempt based on absence from the latest-only report.
        assert await _read_inventory(db, ids, candidates=[ref("9001"), ref("9999", "78")]) == 0
        assert probe.payload["known_attempt_ids"] == ["8001", "9002"]


async def test_probe_reduction_is_scoped_to_idle_same_actor_course_activity_and_run(app_bundle):
    _, sessions, _ = app_bundle
    ids = await _seed_history_target(sessions)
    async with sessions() as db, db.begin():
        other_course = Course(
            connection_id=ids["connection_id"], external_id="550", title="Other course",
        )
        other_assessment = Assessment(
            course_id=ids["course_id"], title="Other quiz", created_by_id=ids["teacher_id"],
        )
        db.add_all([other_course, other_assessment])
        await db.flush()
        current = _probe(ids)
        retried = _probe(ids, state="RETRY", last_error="Previous temporary failure")
        protected = [
            _probe(ids, payload_patch={"actor_external_subject": "43"}),
            _probe(ids, run="another-run"),
            _probe(ids, course_id=other_course.id),
            _probe(ids, aggregate_id=other_assessment.id),
            _probe(ids, payload_patch={"course_id": "550"}),
            _probe(ids, payload_patch={"cmid": 778}),
            _probe(ids, payload_patch={"module": "assign"}),
            _probe(ids, payload_patch={"probe_only": False}),
            _probe(ids, aggregate_type="Other"),
            _probe(ids, event_type="course.sync"),
            _probe(
                ids, state="PROCESSING", attempts=1, locked_at=utcnow(), last_attempt_at=utcnow(),
            ),
            _probe(ids, state="DELIVERED"),
            _probe(ids, state="BLOCKED"),
        ]
        db.add_all([current, retried, *protected])
        await db.flush()
        previous = [
            (job.state, dict(job.payload), job.locked_at, job.last_attempt_at)
            for job in protected
        ]
        assert await _read_inventory(db, ids, candidates=[ref()]) == 1
        assert current.state == retried.state == "DELIVERED"
        assert retried.last_error == ""
        for job, before in zip(protected, previous, strict=True):
            assert (job.state, job.payload, job.locked_at, job.last_attempt_at) == before


@pytest.mark.parametrize("candidates,run", [([], "current-run"), ([ref()], None)])
async def test_empty_or_legacy_inventory_cannot_finish_an_unverified_manual_probe(
    app_bundle, candidates, run,
):
    _, sessions, _ = app_bundle
    ids = await _seed_history_target(sessions)
    async with sessions() as db, db.begin():
        probe = _probe(ids)
        db.add(probe)
        await db.flush()
        await _read_inventory(db, ids, candidates=candidates, run=run)
        assert probe.state == "PENDING" and probe.payload["known_attempt_ids"] == ["9001"]


async def test_invalid_inventory_does_not_change_probe_jobs(app_bundle):
    _, sessions, _ = app_bundle
    ids = await _seed_history_target(sessions)
    async with sessions() as db, db.begin():
        probe = _probe(ids)
        db.add(probe)
        await db.flush()
        with pytest.raises(IntegrationProtocolError):
            await _read_inventory(db, ids, candidates=[ref(), ref("untrusted-id")])
        assert probe.state == "PENDING" and probe.payload["known_attempt_ids"] == ["9001"]


async def test_inventory_reduction_rolls_back_with_its_answer_jobs(app_bundle):
    _, sessions, _ = app_bundle
    ids = await _seed_history_target(sessions)
    async with sessions() as db, db.begin():
        probe = _probe(ids)
        db.add(probe)
        await db.flush()
        probe_id = probe.id
    async with sessions() as db:
        await _read_inventory(db, ids, candidates=[ref()])
        assert (await db.get(SyncOutbox, probe_id)).state == "DELIVERED"
        await db.rollback()
    async with sessions() as db:
        probe = await db.get(SyncOutbox, probe_id)
        assert probe.state == "PENDING" and probe.payload["known_attempt_ids"] == ["9001"]
        assert len(list((await db.scalars(select(SyncOutbox))).all())) == 1
