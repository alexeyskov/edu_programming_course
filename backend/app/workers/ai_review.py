"""Independent, bounded background processing for advisory code reviews."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.ai import process_next_ai_code_review
from app.core.config import Settings

LOGGER = logging.getLogger("eduprog.ai_review")


async def run_ai_review_worker(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    once: bool = False,
    stop_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> int:
    """Process one durable job at a time, outside the critical Moodle delivery lane.

    The processor claims jobs atomically and checks live AI policy before any
    model call. Cancellation leaves the durable lease for stale-job recovery.
    """
    processed_count = 0
    while stop_event is None or not stop_event.is_set():
        try:
            processed = await process_next_ai_code_review(session_factory, settings)
        except Exception:
            # Recover from temporary database failures; provider failures are
            # normally persisted on the individual report by the processor.
            LOGGER.exception("AI review worker iteration failed")
            processed = False
        if processed:
            processed_count += 1
        if once:
            return processed_count
        if not processed and (stop_event is None or not stop_event.is_set()):
            await sleep(1.0)
    return processed_count
