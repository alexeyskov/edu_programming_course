from __future__ import annotations

import math
from datetime import datetime, timedelta

from app.core.config import Settings


def course_sync_stale_before(settings: Settings, now: datetime) -> datetime:
    """Return the point before which an abandoned foreground sync is recoverable.

    A foreground course refresh is not represented by an outbox lease while its
    HTTP request is in flight.  Keep its ``SYNCING`` marker alive for at least
    both the durable-worker lease and the browser request timeout (including
    the same unwind margin used by browser credentials).  The scheduler may
    recover a marker only after this bound.
    """

    browser_bound = max(
        30,
        min(
            615,
            math.ceil(float(settings.moodle_browser_http_timeout_seconds)) + 15,
        ),
    )
    stale_seconds = max(int(settings.sync_lease_seconds), browser_bound)
    return now - timedelta(seconds=stale_seconds)


__all__ = ["course_sync_stale_before"]
