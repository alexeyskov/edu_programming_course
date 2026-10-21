"""Bounded, user-scoped Moodle session renewal while the application is open."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.credential_crypto import BROWSER_STATE_CREDENTIAL_KIND
from app.db.base import utcnow
from app.integrations.errors import IntegrationError
from app.integrations.moodle_browser import MoodleBrowserClient
from app.integrations.moodle_modes import moodle_auth_mode, moodle_pluginless_transport
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.models.identity import ExternalPrincipal, LMSConnection, MoodleCredential
from app.services.sync import (
    ConnectionTarget,
    _BlockedDelivery,
    _persist_browser_context,
    _principal_browser_read_target,
)

KEEPALIVE_SECONDS = 300
LOGGER = logging.getLogger(__name__)


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def keep_moodle_session_alive(
    db: AsyncSession, settings: Settings, principal_id: uuid.UUID,
) -> str:
    """Never turn a transient connector failure into a local logout.

    Respect existing credential leases/CAS, but never acquire a write lease:
    even a slow heartbeat must not block a student's checkpoint or final save.
    No passwords, course scans, submissions or AI calls are involved.
    """
    principal = await db.get(ExternalPrincipal, principal_id)
    connection = await db.get(LMSConnection, principal.connection_id) if principal else None
    if (
        principal is None or not principal.active or connection is None or not connection.enabled
        or connection.provider != "MOODLE" or moodle_auth_mode(connection) != "PLUGINLESS"
        or moodle_pluginless_transport(connection) != "PLAYWRIGHT"
    ):
        return "SKIPPED"
    credential = await db.scalar(select(MoodleCredential).where(
        MoodleCredential.connection_id == connection.id,
        MoodleCredential.principal_id == principal_id,
        MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
    ).with_for_update().execution_options(populate_existing=True))
    if credential is None or credential.status != "ACTIVE" or credential.revoked_at is not None:
        return "REAUTH_REQUIRED"
    now = utcnow()
    if credential.expires_at and _aware(credential.expires_at) <= now:
        return "REAUTH_REQUIRED"
    if (credential.lease_owner and credential.lease_expires_at
            and _aware(credential.lease_expires_at) > now):
        await db.rollback()
        return "BUSY"
    # Successful foreground/background Moodle requests already renew the same
    # session. Across multiple tabs, also throttle failed keepalive attempts.
    recent = credential.last_verified_at
    try:
        attempted = datetime.fromisoformat((credential.metadata_json or {}).get(
            "keepalive_attempted_at", ""
        ))
    except (TypeError, ValueError):
        attempted = None
    if recent and now - _aware(recent) < timedelta(seconds=KEEPALIVE_SECONDS):
        return "ACTIVE"
    if attempted and now - _aware(attempted) < timedelta(seconds=KEEPALIVE_SECONDS):
        return "BUSY"

    # The short row lock serializes only heartbeat admission across tabs.
    # Commit it before the profile GET; student writes remain unblocked.
    credential.metadata_json = {
        **(credential.metadata_json or {}), "keepalive_attempted_at": now.isoformat(),
    }

    target = ConnectionTarget(
        id=connection.id, base_url=connection.base_url, service_token=None,
        mode="PLUGINLESS", transport="PLAYWRIGHT",
    )
    try:
        target = await _principal_browser_read_target(db, settings, target, principal_id)
    except _BlockedDelivery:
        await db.rollback()
        return "REAUTH_REQUIRED"

    try:
        async with asyncio.timeout(25), httpx.AsyncClient(
            follow_redirects=False, trust_env=False,
        ) as client:
            browser = MoodleBrowserClient(
                settings, client, base_url=target.base_url, storage_state=target.browser_state,
            )
            refreshed = await browser.keepalive(principal.external_subject)
        await _persist_browser_context(db, settings, target, refreshed)
        await db.commit()
        return "ACTIVE"
    except MoodleAuthenticationError:
        await db.rollback()
        expired = await db.execute(update(MoodleCredential).where(
            MoodleCredential.id == target.browser_credential_id,
            MoodleCredential.revision == target.browser_credential_revision,
            or_(
                MoodleCredential.lease_owner.is_(None),
                MoodleCredential.lease_expires_at <= utcnow(),
            ),
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
        ).values(status="EXPIRED", expires_at=utcnow()))
        await db.commit()
        return "REAUTH_REQUIRED" if expired.rowcount == 1 else "BUSY"
    except (IntegrationError, TimeoutError):
        await db.rollback()
        return "UNAVAILABLE"
    finally:
        async def release() -> None:
            try:
                async with asyncio.timeout(5):
                    await db.rollback()
            except Exception:
                LOGGER.warning("Moodle keepalive transaction cleanup was interrupted")

        # No write lease is acquired; finish transaction cleanup on cancellation.
        task = asyncio.create_task(release())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise
