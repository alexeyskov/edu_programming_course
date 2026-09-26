from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.errors import IntegrationUnavailable
from app.models.courses import Course
from app.models.identity import MoodleCredential
from app.models.integration import SyncOutbox
from app.services import sync
from tests.test_sync_workers import _seed_attempt, _seed_playwright_course_sync


async def _queued_event(sessions, now):
    attempt_id, course_id = await _seed_attempt(
        sessions,
        now=now,
        deadline=now + timedelta(minutes=20),
    )
    async with sessions() as db, db.begin():
        course = await db.get(Course, course_id)
        row = SyncOutbox(
            connection_id=course.connection_id,
            course_id=course_id,
            attempt_id=attempt_id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            payload={"reason": "SUBMISSION"},
            idempotency_key=f"lease-test:{attempt_id}",
            next_attempt_at=now,
        )
        db.add(row)
        await db.flush()
        return row.id


async def _seed_exclusive_browser_write(sessions, settings, monkeypatch):
    """Exercise real write-session leasing without coupling to course reads."""
    event_id, credential_id, _, _ = await _seed_playwright_course_sync(sessions, settings)
    async with sessions() as db, db.begin():
        credential = await db.get(MoodleCredential, credential_id)
        principal_id = credential.principal_id
        event = await db.get(SyncOutbox, event_id)
        event.event_type = "attempt.checkpoint"
        event.aggregate_type = "Snapshot"
        event.payload = {"reason": "SUBMISSION"}

    async def prepare_exclusive_write(db, config, _claim, connection):
        leased = await sync._principal_credential_target(db, config, connection, principal_id)
        return sync._CheckpointDelivery(connection=leased, payload={"reason": "SUBMISSION"})

    # Snapshot validation is unrelated to cancellation at COMMIT/session close.
    # Keep the actual credential lease and the production preparation cleanup.
    monkeypatch.setattr(sync, "_prepare_checkpoint", prepare_exclusive_write)
    return event_id, credential_id


async def test_live_heartbeat_prevents_reclaim_without_invalidating_delivery(app_bundle):
    _, sessions, settings = app_bundle
    settings.sync_lease_seconds = 5
    now = datetime.now(UTC)
    event_id = await _queued_event(sessions, now)
    claim = await sync.claim_next_outbox_event(sessions, settings, now=now)
    assert claim is not None and claim.id == event_id
    assert await sync._renew_outbox_claim(sessions, claim, now=now + timedelta(seconds=4))
    assert (
        await sync.claim_next_outbox_event(
            sessions,
            settings,
            now=now + timedelta(seconds=6),
        )
        is None
    )
    # Completion must still recognize the original owner after the heartbeat
    # changed locked_at; last_attempt_at remains its immutable fence.
    assert await sync._finish_failure(
        sessions,
        settings,
        claim,
        IntegrationUnavailable(),
        now=now + timedelta(seconds=7),
    )
    async with sessions() as db:
        row = await db.get(SyncOutbox, event_id)
        assert row.state == "RETRY"
        assert row.attempts == 1


async def test_expired_worker_cannot_renew_or_commit_over_new_owner(app_bundle):
    _, sessions, settings = app_bundle
    settings.sync_lease_seconds = 5
    now = datetime.now(UTC)
    event_id = await _queued_event(sessions, now)
    old = await sync.claim_next_outbox_event(sessions, settings, now=now)
    newer = await sync.claim_next_outbox_event(sessions, settings, now=now + timedelta(seconds=6))
    assert old is not None and newer is not None
    assert newer.id == old.id == event_id
    assert newer.attempts == 2
    assert not await sync._renew_outbox_claim(sessions, old)
    assert not await sync._finish_failure(
        sessions,
        settings,
        old,
        IntegrationUnavailable(),
        now=now,
    )
    await sync._requeue_interrupted_claim_best_effort(sessions, old)
    async with sessions() as db:
        row = await db.get(SyncOutbox, event_id)
        assert row.state == "PROCESSING" and row.attempts == 2
    assert await sync._renew_outbox_claim(sessions, newer)


async def test_claim_fence_is_not_reused_after_non_billable_retry(app_bundle):
    _, sessions, settings = app_bundle
    now = datetime.now(UTC)
    await _queued_event(sessions, now)
    old = await sync.claim_next_outbox_event(sessions, settings, now=now)
    assert old is not None
    await sync._requeue_interrupted_claim_best_effort(sessions, old)
    async with sessions() as db, db.begin():
        row = await db.get(SyncOutbox, old.id)
        row.next_attempt_at = now  # A clock adjustment or frozen maintenance time.
    newer = await sync.claim_next_outbox_event(sessions, settings, now=now)
    assert newer is not None and newer.attempts == old.attempts
    assert newer.locked_at > old.locked_at
    assert not await sync._renew_outbox_claim(sessions, old)
    assert await sync._renew_outbox_claim(sessions, newer)


@pytest.mark.parametrize("replacement", [None, "owner", "revision"])
async def test_cancelled_credential_commit_releases_only_its_unreturned_lease(
    app_bundle,
    monkeypatch,
    replacement,
):
    _, sessions, settings = app_bundle
    event_id, credential_id = await _seed_exclusive_browser_write(sessions, settings, monkeypatch)
    committed = asyncio.Event()
    original_commit = AsyncSession.commit

    async def delayed_acknowledgement(db):
        await original_commit(db)
        committed.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(AsyncSession, "commit", delayed_acknowledgement)
    worker = asyncio.create_task(sync.process_outbox_once(sessions, settings))
    await asyncio.wait_for(committed.wait(), timeout=2)
    async with sessions() as db, db.begin():
        credential = await db.get(MoodleCredential, credential_id)
        assert credential.lease_owner
        original_owner = credential.lease_owner
        original_revision = credential.revision
        if replacement == "owner":
            credential.lease_owner = "new-worker-owner"
        elif replacement == "revision":
            credential.revision += 1

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, timeout=2)

    async with sessions() as db:
        credential = await db.get(MoodleCredential, credential_id)
        if replacement is None:
            assert credential.lease_owner is None
            assert credential.lease_expires_at is None
        else:
            assert credential.lease_owner == (
                "new-worker-owner" if replacement == "owner" else original_owner
            )
            assert credential.lease_expires_at is not None
        assert credential.revision == original_revision + (replacement == "revision")
        event = await db.get(SyncOutbox, event_id)
        assert event.state == "RETRY" and event.locked_at is None


async def test_cancelled_preparation_session_exit_releases_committed_credential(
    app_bundle,
    monkeypatch,
):
    _, sessions, settings = app_bundle
    event_id, credential_id = await _seed_exclusive_browser_write(sessions, settings, monkeypatch)
    closed = asyncio.Event()
    original_commit = AsyncSession.commit
    original_exit = AsyncSession.__aexit__

    async def mark_lease_commit(db):
        await original_commit(db)
        db.info["test_lease_committed"] = True

    async def delayed_context_return(db, *args):
        await original_exit(db, *args)
        if db.info.get("test_lease_committed"):
            closed.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(AsyncSession, "commit", mark_lease_commit)
    monkeypatch.setattr(AsyncSession, "__aexit__", delayed_context_return)
    worker = asyncio.create_task(sync.process_outbox_once(sessions, settings))
    await asyncio.wait_for(closed.wait(), timeout=2)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(worker, timeout=2)
    async with sessions() as db:
        credential = await db.get(MoodleCredential, credential_id)
        assert credential.lease_owner is None
        assert credential.lease_expires_at is None
        event = await db.get(SyncOutbox, event_id)
        assert event.state == "RETRY" and event.locked_at is None


async def test_cancelled_worker_immediately_requeues_instead_of_waiting_for_lease(app_bundle):
    _, sessions, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, _, _, _ = await _seed_playwright_course_sync(sessions, settings)
    started = asyncio.Event()

    async def handler(_request):
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = asyncio.create_task(sync.process_outbox_once(sessions, settings, client=client))
        await asyncio.wait_for(started.wait(), timeout=2)
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    async with sessions() as db:
        row = await db.get(SyncOutbox, event_id)
        assert row.state == "RETRY"
        assert row.attempts == 0
        assert row.locked_at is None
        assert row.last_error.startswith("WORKER_INTERRUPTED:")
    assert await sync.claim_next_outbox_event(sessions, settings) is not None


async def test_lost_lease_cancels_external_work_and_preserves_new_claim(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, _, _, _ = await _seed_playwright_course_sync(sessions, settings)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def lost_lease(_sessions, _settings, claim):
        await started.wait()
        async with sessions() as db, db.begin():
            row = await db.get(SyncOutbox, claim.id)
            row.attempts += 1
            row.last_attempt_at = row.last_attempt_at + timedelta(seconds=1)

    monkeypatch.setattr(sync, "_keep_outbox_claim_alive", lost_lease)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await asyncio.wait_for(
            sync.process_outbox_once(sessions, settings, client=client),
            timeout=2,
        )
    assert cancelled.is_set()
    async with sessions() as db:
        row = await db.get(SyncOutbox, event_id)
        assert row.state == "PROCESSING"
        assert row.attempts == 2
        assert row.last_error == ""


async def test_stalled_renewal_stops_delivery_before_lease_expires(app_bundle, monkeypatch):
    _, sessions, settings = app_bundle
    settings.sync_lease_seconds = 5
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    event_id, _, _, _ = await _seed_playwright_course_sync(sessions, settings)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    renewal_cancelled = asyncio.Event()

    async def handler(_request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def stuck_renewal(*_args):
        try:
            await asyncio.Event().wait()
        finally:
            renewal_cancelled.set()

    monkeypatch.setattr(sync, "_renew_outbox_claim", stuck_renewal)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        worker = asyncio.create_task(sync.process_outbox_once(sessions, settings, client=client))
        await asyncio.wait_for(started.wait(), timeout=1)
        done, _ = await asyncio.wait([worker], timeout=4.8)
        if not done:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        assert done, "renewal must stop delivery before the five-second lease expires"
        with pytest.raises(TimeoutError):
            await worker
    assert cancelled.is_set() and renewal_cancelled.is_set()
    async with sessions() as db:
        row = await db.get(SyncOutbox, event_id)
        assert row.state == "RETRY" and row.locked_at is None
