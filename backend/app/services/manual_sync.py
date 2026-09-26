"""Explicit, course-scoped Moodle synchronization commands.

The course row is the short-lived admission lock, not the browser session. It
serializes requests from different teachers without holding a database lock
during any Moodle I/O. Durable outbox rows own the operation after commit.
"""
from __future__ import annotations

import uuid
from datetime import UTC
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.courses import Course
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment
from app.services.common import DomainError
from app.services.moodle_history import enqueue_historical_submission_imports
from app.services.moodle_history_diagnostics import history_import_diagnostic
from app.services.moodle_history_warnings import add_history_warning_details

ACTIVE_STATES = ("PENDING", "PROCESSING", "RETRY")


async def lock_course_sync_admission(db: AsyncSession, course: Course) -> None:
    # A no-op UPDATE gives PostgreSQL a row lock and also works in SQLite tests.
    # Do not change updated_at: it describes real metadata synchronization.
    await db.execute(
        update(Course)
        .where(Course.id == course.id)
        .values(updated_at=Course.updated_at)
        .execution_options(synchronize_session=False)
    )
    await db.refresh(course)


async def require_idle_course_for_import(db: AsyncSession, *, course: Course) -> None:
    """Re-enabling a catalog entry must not bypass manual admission either."""
    await lock_course_sync_admission(db, course)
    active_type = await db.scalar(select(SyncOutbox.event_type).where(
        SyncOutbox.course_id == course.id,
        SyncOutbox.event_type.in_(("course.sync", "moodle.history.import")),
        SyncOutbox.state.in_(ACTIVE_STATES),
    ).limit(1))
    if active_type is not None:
        raise DomainError(
            409,
            "COURSE_SYNC_IN_PROGRESS" if active_type == "course.sync"
            else "ASSESSMENT_SYNC_IN_PROGRESS",
            "Дождитесь завершения уже запущенной синхронизации этого курса.",
        )


async def queue_manual_course_sync(
    db: AsyncSession, *, course: Course, actor_external_subject: str
) -> None:
    await lock_course_sync_admission(db, course)
    active = await db.scalar(
        select(SyncOutbox.id).where(
            SyncOutbox.course_id == course.id,
            SyncOutbox.event_type == "course.sync",
            SyncOutbox.state.in_(ACTIVE_STATES),
        ).limit(1)
    )
    if active is not None:
        course.sync_status = "SYNCING"
        return
    task_active = await db.scalar(
        select(SyncOutbox.id).where(
            SyncOutbox.course_id == course.id,
            SyncOutbox.event_type == "moodle.history.import",
            SyncOutbox.state.in_(ACTIVE_STATES),
        ).limit(1)
    )
    if task_active is not None:
        raise DomainError(
            409, "ASSESSMENT_SYNC_IN_PROGRESS",
            "Дождитесь завершения синхронизации работ этого курса.",
        )
    course.sync_status = "SYNCING"
    course.sync_error_code = ""
    course.sync_error_message = ""
    course.sync_error_at = None
    course.sync_error_retryable = False
    course.updated_at = utcnow()
    run_id = uuid.uuid4().hex
    db.add(SyncOutbox(
        connection_id=course.connection_id,
        course_id=course.id,
        event_type="course.sync",
        aggregate_type="Course",
        aggregate_id=course.id,
        idempotency_key=f"course-sync-manual:{course.id.hex}:{run_id}",
        payload={
            "course_id": course.external_id,
            "actor_external_subject": actor_external_subject,
            "manual_run_id": run_id,
        },
    ))
    await db.flush()


async def assessment_sync_statuses(
    db: AsyncSession, *, course_id: uuid.UUID, assessment_id: uuid.UUID | None = None,
    principal_id: uuid.UUID | None = None, allow_system_settings_read: bool = False,
) -> list[dict[str, Any]]:
    assessments_query = select(Assessment.id, Assessment.title).where(
        Assessment.course_id == course_id
    )
    if assessment_id is not None:
        assessments_query = assessments_query.where(Assessment.id == assessment_id)
    assessments = list((await db.execute(assessments_query)).all())
    assessment_ids = [row.id for row in assessments]
    assessment_titles = {row.id: row.title for row in assessments}
    if not assessment_ids:
        return []
    # Only root inventories select the latest run. An older, slow detail must
    # never replace the status of a newer manual click.
    ranked = select(
        SyncOutbox.id,
        func.row_number().over(
            partition_by=SyncOutbox.aggregate_id,
            order_by=(SyncOutbox.created_at.desc(), SyncOutbox.id.desc()),
        ).label("rank"),
    ).where(
        SyncOutbox.course_id == course_id,
        SyncOutbox.aggregate_id.in_(assessment_ids),
        SyncOutbox.event_type == "moodle.history.import",
        SyncOutbox.payload["scan_only"].as_boolean().is_(True),
        SyncOutbox.payload["cursor"].as_string() == "0:0",
        or_(
            SyncOutbox.payload["probe_only"].as_boolean().is_(False),
            SyncOutbox.payload["probe_only"].as_boolean().is_(None),
        ),
        SyncOutbox.payload["manual_run_id"].as_string().is_not(None),
    ).subquery()
    roots = list((await db.scalars(
        select(SyncOutbox).join(ranked, ranked.c.id == SyncOutbox.id).where(ranked.c.rank == 1)
    )).all())
    roots_by_assessment = {row.aggregate_id: row for row in roots}
    run_ids = [row.payload["manual_run_id"] for row in roots]
    rows = list((await db.scalars(select(SyncOutbox).where(
        SyncOutbox.course_id == course_id,
        SyncOutbox.event_type == "moodle.history.import",
        SyncOutbox.payload["manual_run_id"].as_string().in_(run_ids),
    ))).all()) if run_ids else []
    runs: dict[str, list[SyncOutbox]] = {}
    for row in rows:
        runs.setdefault(row.payload["manual_run_id"], []).append(row)
    result = []
    for current_id in assessment_ids:
        root = roots_by_assessment.get(current_id)
        current = runs.get(root.payload["manual_run_id"], []) if root is not None else []
        failed = [row for row in current if row.state in {"FAILED", "BLOCKED"}]
        warnings = [row for row in current if (row.receipt or {}).get("warning_count")]
        state = (
            "SYNCING" if any(row.state in ACTIVE_STATES for row in current)
            else "FAILED" if failed
            else "PARTIAL" if warnings
            else "COMPLETED" if current
            else "IDLE"
        )
        error_code, last_error = (
            history_import_diagnostic(failed or warnings)
            if failed or warnings else (None, None)
        )
        result.append({
            "assessment_id": current_id,
            "assessment_title": assessment_titles[current_id],
            "status": state,
            "last_error": last_error,
            "error_code": error_code,
            "updated_at": max((
                row.updated_at.replace(tzinfo=UTC)
                if row.updated_at.tzinfo is None else row.updated_at
                for row in current
            ), default=None),
        })
    if principal_id is not None:
        await add_history_warning_details(
            db, course_id=course_id, principal_id=principal_id, statuses=result, rows=rows,
            allow_system_settings_read=allow_system_settings_read,
        )
    return result


async def queue_manual_assessment_sync(
    db: AsyncSession, *, course: Course, assessment: Assessment,
    actor_external_subject: str,
) -> dict[str, Any]:
    await lock_course_sync_admission(db, course)
    course_active = await db.scalar(select(SyncOutbox.id).where(
        SyncOutbox.course_id == course.id,
        SyncOutbox.event_type == "course.sync",
        SyncOutbox.state.in_(ACTIVE_STATES),
    ).limit(1))
    if course_active is not None:
        raise DomainError(
            409, "COURSE_SYNC_IN_PROGRESS",
            "Дождитесь завершения синхронизации этого курса.",
        )
    statuses = await assessment_sync_statuses(
        db, course_id=course.id, assessment_id=assessment.id,
    )
    if statuses and statuses[0]["status"] == "SYNCING":
        return statuses[0]
    queued = await enqueue_historical_submission_imports(
        db, course=course, actor_external_subject=actor_external_subject,
        assessment_id=assessment.id, manual_run_id=uuid.uuid4().hex,
    )
    if not queued:
        raise DomainError(
            409, "ASSESSMENT_SYNC_UNAVAILABLE",
            "Синхронизация работы недоступна. Проверьте вход в Moodle и обновите курс.",
        )
    return (await assessment_sync_statuses(
        db, course_id=course.id, assessment_id=assessment.id,
    ))[0]
