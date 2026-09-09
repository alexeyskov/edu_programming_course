from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.session import create_engine, create_session_factory
from app.services.sync import BridgeFactory, ClientFactory, process_outbox_once

LOGGER = logging.getLogger("eduprog.sync")


async def run_sync_worker(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    bridge_factory: BridgeFactory | None = None,
    once: bool = False,
    stop_event: asyncio.Event | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    client: httpx.AsyncClient | None = None,
    client_factory: ClientFactory | None = None,
    terminal_checkpoints_only: bool = False,
    concurrency: int | None = None,
) -> int:
    """Drain due events through bounded lanes backed by atomic row leases.

    Course discovery can legitimately spend several minutes reading Moodle.  A
    single sequential loop would keep student checkpoints queued for that whole
    interval, so production uses two lanes by default.  ``once`` deliberately
    remains single-lane for deterministic maintenance commands and tests.
    """

    poll_seconds = max(0.1, float(getattr(settings, "sync_poll_seconds", 1)))
    lane_count = (
        1
        if once
        else max(
            1,
            int(
                concurrency
                if concurrency is not None
                else getattr(settings, "sync_worker_concurrency", 2)
            ),
        )
    )
    owns_client = client is None
    active_client = client or (
        client_factory()
        if client_factory
        else httpx.AsyncClient(follow_redirects=False, trust_env=False)
    )

    async def run_lane() -> int:
        delivered_or_transitioned = 0
        while stop_event is None or not stop_event.is_set():
            try:
                processed = await process_outbox_once(
                    session_factory,
                    settings,
                    bridge_factory=bridge_factory,
                    client=active_client,
                    terminal_checkpoints_only=terminal_checkpoints_only,
                )
            except Exception:
                # A row-level error is normally persisted by the service; this guards DB outages.
                LOGGER.exception("sync worker iteration failed")
                processed = False
            if processed:
                delivered_or_transitioned += 1
            if once:
                return delivered_or_transitioned
            if not processed:
                await sleep(poll_seconds)
        return delivered_or_transitioned

    try:
        if lane_count == 1:
            return await run_lane()
        return sum(await asyncio.gather(*(run_lane() for _ in range(lane_count))))
    finally:
        if owns_client:
            await active_client.aclose()


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        await run_sync_worker(session_factory, settings)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
