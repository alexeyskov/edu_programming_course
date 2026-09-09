from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.services.sync import SchedulerResult, run_scheduler_iteration

LOGGER = logging.getLogger("eduprog.scheduler")
SCHEDULER_ADVISORY_LOCK_ID = 1_129_270_868  # signed int derived from ASCII "CONT"


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
        try:
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
