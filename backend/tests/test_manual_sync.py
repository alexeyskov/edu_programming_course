from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import event, func, select

from app.api.courses import (
    confirm_course_import,
    course_assessment_sync_status,
    sync_assessment,
    sync_course,
)
from app.api.system import retry_sync_outbox
from app.auth.context import AuthContext
from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.session import create_engine, create_session_factory
from app.models.attempts import Attempt
from app.models.courses import Course, CourseImportJob, CourseMembership
from app.models.identity import ExternalPrincipal, TeacherAccessToken, TeacherTokenGrant
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment
from app.schemas.courses import CourseSyncRequest
from app.schemas.system import SyncOutboxRetryRequest
from app.services.common import DomainError
from app.services.manual_sync import (
    assessment_sync_statuses,
    queue_manual_assessment_sync,
    queue_manual_course_sync,
)
from app.services.moodle_history import enqueue_historical_answer_reads
from tests.test_moodle_browser_courses import _discovery
from tests.test_moodle_history import _seed_history_target


def ref():
    return {
        "attempt_id": "9001", "user_id": "77", "display_name": "Student",
        "state": "SUBMITTED", "submitted_at_epoch": 0, "grade": None, "grade_max": None,
    }


def auth(principal_id):
    return AuthContext(principal_id, "Teacher", uuid.uuid4(), "test", ("TEACHER",), ())


async def test_manual_course_request_is_durable_and_never_contacts_moodle(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)

    async def forbidden(*_args, **_kwargs):
        pytest.fail("Manual course POST must not perform a browser request")

    monkeypatch.setattr("app.api.courses._discover", forbidden)
    async with sessions() as db:
        response = await sync_course(
            ids["course_id"], CourseSyncRequest(), auth(ids["teacher_id"]), db,
        )
        assert response["sync_status"] == "SYNCING"
        response = await sync_course(
            ids["course_id"], CourseSyncRequest(), auth(ids["teacher_id"]), db,
        )
        assert response["sync_status"] == "SYNCING"
        rows = list((await db.scalars(select(SyncOutbox))).all())
        assert len(rows) == 1 and rows[0].event_type == "course.sync"
        assert rows[0].state == "PENDING"
        assert rows[0].payload["actor_external_subject"] == "42"


async def test_assessment_sync_and_status_are_shared_between_teachers(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        second = ExternalPrincipal(
            connection_id=ids["connection_id"], external_subject="43", display_name="Other teacher",
        )
        db.add(second)
        await db.flush()
        token = TeacherAccessToken(
            public_id=second.id.hex[:16], label="Other teacher", secret_hash="$argon2id$test",
            created_by_id=second.id,
        )
        db.add(token)
        await db.flush()
        db.add_all([
            CourseMembership(course_id=ids["course_id"], principal_id=second.id, role="TEACHER"),
            TeacherTokenGrant(token_id=token.id, principal_id=second.id),
        ])
        second_id = second.id
    async with sessions() as db:
        first = await sync_assessment(
            ids["assessment_id"], CourseSyncRequest(), auth(ids["teacher_id"]), db,
        )
        # The second teacher needs no separate credential for the active job:
        # this is the same course operation, not a second actor-owned run.
        second_result = await sync_assessment(
            ids["assessment_id"], CourseSyncRequest(), auth(second_id), db,
        )
        assert first == second_result and first["status"] == "SYNCING"
        statuses = await course_assessment_sync_status(ids["course_id"], auth(second_id), db)
        assert statuses == [first]
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 1


async def test_course_and_task_exclude_each_other_but_not_other_courses(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await queue_manual_course_sync(db, course=course, actor_external_subject="42")
        with pytest.raises(DomainError) as busy:
            await queue_manual_assessment_sync(
                db, course=course, assessment=assessment, actor_external_subject="42",
            )
        assert busy.value.code == "COURSE_SYNC_IN_PROGRESS"
        job = await db.scalar(select(SyncOutbox))
        job.state = "DELIVERED"
        await db.flush()
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        with pytest.raises(DomainError) as busy:
            await queue_manual_course_sync(db, course=course, actor_external_subject="42")
        assert busy.value.code == "ASSESSMENT_SYNC_IN_PROGRESS"
        other = Course(
            connection_id=course.connection_id, external_id="other", title="Other course",
            catalog_enabled=True,
        )
        db.add(other)
        await db.flush()
        await queue_manual_course_sync(db, course=other, actor_external_subject="42")
        assert other.sync_status == "SYNCING"


async def test_manual_run_waits_for_all_details_then_allows_full_refresh(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        root = await db.scalar(select(SyncOutbox))
        run_id = root.payload["manual_run_id"]
        root.state = "DELIVERED"
        root.delivered_at = utcnow()
        assert await enqueue_historical_answer_reads(
            db, course=course, assessment=assessment, actor_external_subject="42", module="quiz",
            cmid=777, candidates=[ref()], scan_id=root.id, manual_run_id=run_id,
        ) == 1
        state = (await assessment_sync_statuses(db, course_id=course.id))[0]
        assert state["status"] == "SYNCING"
        # A second click must not start an inventory while a detail is pending.
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 2
        detail = await db.scalar(select(SyncOutbox).where(SyncOutbox.id != root.id))
        detail.state = "DELIVERED"
        detail.delivered_at = utcnow()
        await db.flush()
        assert (await assessment_sync_statuses(db, course_id=course.id))[0]["status"] == "COMPLETED"
        # Explicit second synchronization bypasses the former 15 minute cache.
        root.created_at = utcnow() - timedelta(minutes=1)
        second = await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        assert second["status"] == "SYNCING"
        new_root = await db.scalar(select(SyncOutbox).where(SyncOutbox.state == "PENDING"))
        assert new_root.payload["manual_run_id"] != run_id
        assert await enqueue_historical_answer_reads(
            db, course=course, assessment=assessment, actor_external_subject="42", module="quiz",
            cmid=777, candidates=[ref()], scan_id=new_root.id,
            manual_run_id=new_root.payload["manual_run_id"],
        ) == 1


async def test_status_does_not_hide_failed_detail_behind_successful_inventory(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        idle = await assessment_sync_statuses(db, course_id=course.id)
        assert idle[0]["status"] == "IDLE"
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        root = await db.scalar(select(SyncOutbox))
        root.state = "DELIVERED"
        await enqueue_historical_answer_reads(
            db, course=course, assessment=assessment, actor_external_subject="42", module="quiz",
            cmid=777, candidates=[ref()], scan_id=root.id,
            manual_run_id=root.payload["manual_run_id"],
        )
        detail = await db.scalar(select(SyncOutbox).where(SyncOutbox.id != root.id))
        detail.state = "FAILED"
        detail.last_error = "private connector diagnostic"
        await db.flush()
        state = (await assessment_sync_statuses(db, course_id=course.id))[0]
        assert state["status"] == "FAILED" and state["last_error"]
        assert "private" not in state["last_error"]


@pytest.mark.parametrize("error_code", [
    "TIMEOUT", "INVALID_RESPONSE", "UNEXPECTED_VALUEERROR", "private-secret",
])
async def test_failed_import_status_exposes_only_safe_failure_category(app_bundle, error_code):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        await queue_manual_assessment_sync(
            db, course=course, assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42",
        )
        root = await db.scalar(select(SyncOutbox))
        root.state = "FAILED"
        root.last_error = f"{error_code}: private diagnostic with session=value"
        await db.flush()
        status = (await assessment_sync_statuses(db, course_id=course.id))[0]
        assert status["status"] == "FAILED"
        assert status["error_code"] == (
            "HISTORY_IMPORT_FAILED" if error_code == "private-secret" else error_code
        )
        assert "private" not in status["last_error"] and "session=" not in status["last_error"]


@pytest.mark.parametrize("warning_code", [None, "ARTIFACT_OMITTED", "DELETION_CHECK_INCOMPLETE"])
async def test_delivered_with_warnings_is_partial_not_a_failed_import(app_bundle, warning_code):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        root = await db.scalar(select(SyncOutbox))
        root.state = "DELIVERED"
        root.receipt = {"warning_count": 2, "unchanged": 2}
        if warning_code:
            root.receipt["warning_codes"] = [warning_code]
        await db.flush()
        status = (await assessment_sync_statuses(db, course_id=course.id))[0]
        assert status["status"] == "PARTIAL"
        assert status["error_code"] == (warning_code or "HISTORY_IMPORT_WARNING")
        assert status["last_error"]
        # Warnings must not lock admission to the next explicit refresh.
        root.created_at = utcnow() - timedelta(minutes=1)
        assert (await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        ))["status"] == "SYNCING"


async def test_student_cannot_start_or_read_manual_sync(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        member = await db.scalar(select(CourseMembership))
        member.role = "STUDENT"
    async with sessions() as db:
        for call in (
            lambda: sync_course(ids["course_id"], CourseSyncRequest(), auth(ids["teacher_id"]), db),
            lambda: sync_assessment(
                ids["assessment_id"], CourseSyncRequest(), auth(ids["teacher_id"]), db,
            ),
            lambda: course_assessment_sync_status(ids["course_id"], auth(ids["teacher_id"]), db),
        ):
            with pytest.raises(HTTPException) as forbidden:
                await call()
            assert forbidden.value.status_code == 403


async def test_manual_sync_queues_all_known_attempt_deletion_probes(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        for number in range(13):
            db.add(Attempt(
                assessment_id=ids["assessment_id"], principal_id=ids["teacher_id"],
                sequence=number + 1,
                state="SUBMITTED", integrity_policy={"moodle_attempt_id": str(9000 + number)},
            ))
        await db.flush()
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        await queue_manual_assessment_sync(
            db, course=course, assessment=assessment, actor_external_subject="42",
        )
        jobs = list((await db.scalars(select(SyncOutbox))).all())
        assert len(jobs) == 4
        probes = [job for job in jobs if job.payload.get("probe_only")]
        assert sorted(len(job.payload["known_attempt_ids"]) for job in probes) == [3, 5, 5]
        assert {value for job in probes for value in job.payload["known_attempt_ids"]} == {
            str(9000 + number) for number in range(13)
        }
        assert len({job.payload["manual_run_id"] for job in jobs}) == 1
        root = next(job for job in jobs if not job.payload.get("probe_only"))
        root.state = "DELIVERED"
        await db.flush()
        assert (await assessment_sync_statuses(db, course_id=course.id))[0]["status"] == "SYNCING"


@pytest.mark.parametrize("command", ["course", "task", "mixed"])
async def test_concurrent_manual_clicks_share_database_admission_lock(tmp_path, command):
    settings = Settings(
        debug=True, secret_key="test-secret-key-with-more-than-thirty-two-characters",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'manual-sync.sqlite'}",
    )
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        ids = await _seed_history_target(sessions, settings=settings)

        async def click(index):
            async with sessions() as db, db.begin():
                course = await db.get(Course, ids["course_id"])
                assessment = await db.get(Assessment, ids["assessment_id"])
                try:
                    if command == "course" or (command == "mixed" and index % 2 == 0):
                        await queue_manual_course_sync(
                            db, course=course, actor_external_subject="42",
                        )
                    else:
                        await queue_manual_assessment_sync(
                            db, course=course, assessment=assessment, actor_external_subject="42",
                        )
                except DomainError as exc:
                    assert command == "mixed"
                    assert exc.code in {"COURSE_SYNC_IN_PROGRESS", "ASSESSMENT_SYNC_IN_PROGRESS"}

        await asyncio.gather(*(click(index) for index in range(20)))
        async with sessions() as db:
            assert await db.scalar(select(func.count(SyncOutbox.id))) == 1
    finally:
        await engine.dispose()


@pytest.mark.parametrize("event_type", ["course.sync", "moodle.history.import"])
@pytest.mark.parametrize("state", ["FAILED", "BLOCKED", "PENDING", "RETRY"])
async def test_generic_retry_cannot_bypass_manual_sync_admission(app_bundle, event_type, state):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        row = SyncOutbox(
            connection_id=ids["connection_id"], course_id=ids["course_id"],
            event_type=event_type,
            aggregate_type="Course" if event_type == "course.sync" else "Assessment",
            aggregate_id=ids["course_id"] if event_type == "course.sync" else ids["assessment_id"],
            state=state, idempotency_key="old-run", attempts=8,
            payload={"actor_external_subject": "42", "manual_run_id": "old-run"},
        )
        db.add(row)
        await db.flush()
        row_id = row.id
    async with sessions() as db:
        with pytest.raises(HTTPException) as blocked:
            await retry_sync_outbox(
                row_id, SyncOutboxRetryRequest(reason="Retry stale run"),
                SimpleNamespace(state=SimpleNamespace(request_id="test")),
                auth(ids["teacher_id"]), db,
            )
        assert blocked.value.status_code == 409
        assert blocked.value.detail["code"] == "MANUAL_SYNC_REQUIRED"
        row = await db.get(SyncOutbox, row_id)
        assert row.state == state and row.attempts == 8


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("event_type", ["course.sync", "moodle.history.import"])
async def test_readding_course_cannot_apply_stale_preview_during_sync(
    app_bundle, monkeypatch, enabled, event_type,
):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        course.catalog_enabled = enabled
        course.title = "Current title"
        course.sync_status = "SYNCING" if event_type == "course.sync" else "CURRENT"
        original_status = course.sync_status
        discovery = _discovery(course.external_id, revision=1)
        job = CourseImportJob(
            connection_id=course.connection_id, requested_by_id=ids["teacher_id"],
            locator_hash="a" * 64, external_course_id=course.external_id,
            preview=discovery.preview, capability_report=discovery.capabilities,
        )
        db.add_all([job, SyncOutbox(
            connection_id=course.connection_id, course_id=course.id,
            aggregate_type="Course" if event_type == "course.sync" else "Assessment",
            aggregate_id=course.id if event_type == "course.sync" else ids["assessment_id"],
            event_type=event_type, idempotency_key="active-course-operation",
            payload={"actor_external_subject": "42"},
        )])
        await db.flush()
        job_id = job.id

    async def unexpected_projection(*_args, **_kwargs):
        pytest.fail("An import preview must not overwrite an active course/task sync")

    monkeypatch.setattr("app.api.courses._project_course", unexpected_projection)
    context = AuthContext(
        ids["teacher_id"], "Teacher", uuid.uuid4(), "test", ("TEACHER",), ("SYSTEM_SETTINGS",),
    )
    async with sessions() as db:
        request = SimpleNamespace(state=SimpleNamespace(request_id="test"))
        if enabled:
            result = await confirm_course_import(job_id, context, request, db)
            assert result.state.value == "CONFIRMED"
            assert result.confirmed_course == ids["course_id"]
        else:
            with pytest.raises(HTTPException) as busy:
                await confirm_course_import(job_id, context, request, db)
            assert busy.value.status_code == 409
            assert busy.value.detail["code"] == (
                "COURSE_SYNC_IN_PROGRESS" if event_type == "course.sync"
                else "ASSESSMENT_SYNC_IN_PROGRESS"
            )
    async with sessions() as db:
        course = await db.get(Course, ids["course_id"])
        assert course.title == "Current title" and course.sync_status == original_status
        assert course.catalog_enabled is enabled
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 1


async def test_disabled_idle_course_can_be_reenabled_by_explicit_import(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        course.catalog_enabled = False
        discovery = _discovery(course.external_id, revision=2)
        job = CourseImportJob(
            connection_id=course.connection_id, requested_by_id=ids["teacher_id"],
            locator_hash="a" * 64, external_course_id=course.external_id,
            preview=discovery.preview, capability_report=discovery.capabilities,
        )
        db.add(job)
        await db.flush()
        job_id = job.id
    context = AuthContext(
        ids["teacher_id"], "Teacher", uuid.uuid4(), "test", ("TEACHER",), ("SYSTEM_SETTINGS",),
    )
    async with sessions() as db:
        await confirm_course_import(
            job_id, context, SimpleNamespace(state=SimpleNamespace(request_id="test")), db,
        )
        course = await db.get(Course, ids["course_id"])
        assert course.catalog_enabled and course.title == "C++ course revision 2"
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 0


async def test_new_course_confirm_locks_connection_before_duplicate_lookup(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    job_ids = []
    async with sessions() as db, db.begin():
        for revision in (1, 2):
            discovery = _discovery("550", revision=revision)
            job = CourseImportJob(
                connection_id=ids["connection_id"], requested_by_id=ids["teacher_id"],
                locator_hash="b" * 64, external_course_id="550", preview=discovery.preview,
                capability_report=discovery.capabilities,
            )
            db.add(job)
            await db.flush()
            job_ids.append(job.id)
    context = AuthContext(
        ids["teacher_id"], "Teacher", uuid.uuid4(), "test", ("TEACHER",), ("SYSTEM_SETTINGS",),
    )
    lock_order = []

    def capture_locks(_conn, _cursor, _statement, _params, execution_context, _many):
        statement = getattr(execution_context.compiled, "statement", None)
        if getattr(statement, "_for_update_arg", None) is None:
            return
        names = [table.name for table in statement.get_final_froms()]
        lock_order.extend(name for name in names if name in {"core_course", "core_lmsconnection"})

    async with sessions() as db:
        engine = db.bind.sync_engine
        event.listen(engine, "before_cursor_execute", capture_locks)
        try:
            first = await confirm_course_import(
                job_ids[0], context, SimpleNamespace(state=SimpleNamespace(request_id="test")), db,
            )
            # This structural assertion also guards PostgreSQL's nonexistent
            # course-row race; SQLite itself cannot model FOR UPDATE blocking.
            assert lock_order[:2] == ["core_lmsconnection", "core_course"]
            second = await confirm_course_import(
                job_ids[1], context, SimpleNamespace(state=SimpleNamespace(request_id="test")), db,
            )
            assert first.confirmed_course == second.confirmed_course
            course = await db.get(Course, first.confirmed_course)
            assert course.title == "C++ course revision 1"
        finally:
            event.remove(engine, "before_cursor_execute", capture_locks)
