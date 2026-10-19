from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.models.integration import AuditEntry
from app.services.storage_cleanup import cleanup_storage, get_storage_status
from app.services.sync import SchedulerResult, run_scheduler_iteration

LOGGER = logging.getLogger("eduprog.scheduler")
SCHEDULER_ADVISORY_LOCK_ID = 1_129_270_868  # signed int derived from ASCII "CONT"


async def run_storage_maintenance_once(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    """Own one independent transaction; never mutate data below the quota."""
    async with session_factory() as db, db.begin():
        status = await get_storage_status(db, settings)
        if status.used_bytes <= status.storage_limit_gb * 1024 ** 3:
            return
        result = await cleanup_storage(
            db,
            settings,
            category="quota",
            eligible_before=status.eligible_before,
            automatic=True,
        )
        if result.removed_records:
            db.add(
                AuditEntry(
                    actor_id=None,
                    action="storage.cleaned_up",
                    object_type="SystemStorage",
                    metadata_json={"automatic": True, **result.model_dump(mode="json")},
                )
            )
        if result.over_limit:
            # The retention policy must not sacrifice protected work just to
            # meet a quota. Log counts only, never transcripts, source or SQL.
            LOGGER.warning(
                "storage quota remains exceeded: used_bytes=%s limit_bytes=%s removed_records=%s",
                result.used_bytes_after,
                status.storage_limit_gb * 1024 ** 3,
                result.removed_records,
            )


async def run_storage_maintenance(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run sequentially at its own cadence, independently of student deadlines."""
    interval = float(settings.storage_cleanup_interval_seconds)
    while True:
        try:
            await run_storage_maintenance_once(session_factory, settings)
        except Exception as exc:
            # Exception text may contain query parameters or student content.
            # Cancellation deliberately escapes and rolls back the transaction.
            LOGGER.warning("storage maintenance failed: error_type=%s", type(exc).__name__)
        await sleep(interval)


async def acquire_scheduler_lock(connection: AsyncConnection) -> bool:
    """Acquire one session-level PostgreSQL lock; SQLite test/dev has a single local owner."""

    if connection.dialect.name != "postgresql":
        return True
    acquired = await connection.scalar(
        text("SELECT pg_try_advisory_lock(:lock_id)"),
        {"lock_id": SCHEDULER_ADVISORY_LOCK_ID},
    )
    await connection.commit()
    return bool(acquired)


async def release_scheduler_lock(connection: AsyncConnection) -> None:
    if connection.dialect.name != "postgresql":
        return
    await connection.execute(
        text("SELECT pg_advisory_unlock(:lock_id)"),
        {"lock_id": SCHEDULER_ADVISORY_LOCK_ID},
    )
    await connection.commit()


async def run_scheduler(
    engine: AsyncEngine,
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    once: bool = False,
    stop_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SchedulerResult | None:
    poll_seconds = max(1.0, float(getattr(settings, "scheduler_poll_seconds", 5)))
    async with engine.connect() as lock_connection:
        if not await acquire_scheduler_lock(lock_connection):
            LOGGER.info("another scheduler owns the PostgreSQL advisory lock")
            return None
        storage_task: asyncio.Task[None] | None = None
        try:
            if not once and (stop_event is None or not stop_event.is_set()):
                storage_task = asyncio.create_task(
                    run_storage_maintenance(session_factory, settings),
                    name="storage-maintenance",
                )
            last_result: SchedulerResult | None = None
            while stop_event is None or not stop_event.is_set():
                try:
                    last_result = await run_scheduler_iteration(session_factory, settings)
                except Exception:
                    LOGGER.exception("scheduler iteration failed")
                if once:
                    return last_result
                await sleep(poll_seconds)
            return last_result
        finally:
            if storage_task is not None:
                storage_task.cancel()
                await asyncio.gather(storage_task, return_exceptions=True)
            await release_scheduler_lock(lock_connection)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await run_scheduler(engine, session_factory, settings)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
