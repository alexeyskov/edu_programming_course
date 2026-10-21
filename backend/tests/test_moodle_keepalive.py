from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.sessions import create_principal_session
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    decrypt_moodle_browser_state,
    encrypt_moodle_browser_state,
)
from app.db.base import utcnow
from app.integrations.errors import IntegrationUnavailable
from app.integrations.moodle_browser import MoodleBrowserClient
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.models.identity import AdminElevation, ExternalPrincipal, LMSConnection, MoodleCredential
from app.models.integration import SyncOutbox
from app.services.moodle_keepalive import keep_moodle_session_alive


def state(value="opaque"):
    return {"cookies": [{
        "name": "MoodleSession", "value": value, "domain": "moodle.example.test",
        "path": "/", "expires": -1, "secure": True, "httpOnly": True, "sameSite": "Lax",
    }], "origins": []}


async def seed(db, settings):
    connection = LMSConnection(
        name="Moodle", provider="MOODLE", base_url="https://moodle.example.test",
        config={"auth_mode": "PLUGINLESS", "pluginless_transport": "PLAYWRIGHT"},
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id, external_subject="42", display_name="Student",
    )
    db.add(principal)
    await db.flush()
    credential = MoodleCredential(
        connection_id=connection.id, principal_id=principal.id,
        kind=BROWSER_STATE_CREDENTIAL_KIND,
        encrypted_secret=encrypt_moodle_browser_state(
            state(), settings, connection_id=connection.id, principal_id=principal.id,
        ),
    )
    db.add(credential)
    await db.commit()
    return principal, credential


async def test_keepalive_refreshes_only_owned_session_and_throttles_tabs(
    db, app_bundle, monkeypatch,
):
    settings = app_bundle[2]
    principal, credential = await seed(db, settings)
    calls = []

    async def ping(self, subject):
        calls.append(subject)
        assert self.storage_state == state()
        assert not db.in_transaction()
        assert credential.lease_owner is None
        return state("refreshed")

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", ping)
    assert await keep_moodle_session_alive(db, settings, principal.id) == "ACTIVE"
    assert await keep_moodle_session_alive(db, settings, principal.id) == "ACTIVE"
    await db.refresh(credential)
    assert calls == ["42"]
    assert credential.revision == 2
    assert credential.last_verified_at is not None
    assert credential.lease_owner is None
    assert credential.lease_expires_at is None
    assert decrypt_moodle_browser_state(
        credential.encrypted_secret, settings,
        connection_id=credential.connection_id, principal_id=principal.id,
    ) == state("refreshed")
    assert (await db.scalars(select(SyncOutbox))).all() == []


@pytest.mark.parametrize("failure,expected", [
    (IntegrationUnavailable("network"), "UNAVAILABLE"),
    (TimeoutError(), "UNAVAILABLE"),
    (MoodleAuthenticationError("expired"), "REAUTH_REQUIRED"),
])
async def test_transient_failure_never_expires_session_and_releases_lease(
    db, app_bundle, monkeypatch, failure, expected,
):
    settings = app_bundle[2]
    principal, credential = await seed(db, settings)
    principal_id = principal.id

    async def ping(*_args):
        raise failure

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", ping)
    assert await keep_moodle_session_alive(db, settings, principal_id) == expected
    await db.refresh(credential)
    assert credential.status == ("EXPIRED" if expected == "REAUTH_REQUIRED" else "ACTIVE")
    assert credential.lease_owner is None
    assert credential.lease_expires_at is None
    if expected == "UNAVAILABLE":
        assert await keep_moodle_session_alive(db, settings, principal_id) == "BUSY"


async def test_in_progress_student_save_has_priority_over_keepalive(db, app_bundle, monkeypatch):
    settings = app_bundle[2]
    principal, credential = await seed(db, settings)
    principal_id = principal.id
    credential.lease_owner = "student-save"
    credential.lease_expires_at = utcnow() + timedelta(minutes=2)
    await db.commit()

    async def forbidden(*_args):
        pytest.fail("A heartbeat must not steal the student's save lease")

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", forbidden)
    assert await keep_moodle_session_alive(db, settings, principal_id) == "BUSY"
    await db.refresh(credential)
    assert credential.lease_owner == "student-save"


async def test_cancelled_keepalive_does_not_block_student_save(db, app_bundle, monkeypatch):
    settings = app_bundle[2]
    principal, credential = await seed(db, settings)
    entered = asyncio.Event()

    async def ping(*_args):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", ping)
    task = asyncio.create_task(keep_moodle_session_alive(db, settings, principal.id))
    await asyncio.wait_for(entered.wait(), timeout=3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await db.refresh(credential)
    assert credential.status == "ACTIVE"
    assert credential.lease_owner is None


@pytest.mark.parametrize("old_cookie_rejected", [False, True])
async def test_new_login_cannot_be_overwritten_by_old_keepalive(
    db, app_bundle, monkeypatch, old_cookie_rejected,
):
    settings = app_bundle[2]
    principal, credential = await seed(db, settings)
    principal_id = principal.id

    async def new_login(*_args):
        credential.revision += 1
        credential.lease_owner = "new-session-owner"
        credential.encrypted_secret = "new-login-encrypted-state"
        await db.commit()
        if old_cookie_rejected:
            raise MoodleAuthenticationError("old cookie expired after new login")
        return state("old-refreshed")

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", new_login)
    expected = "BUSY" if old_cookie_rejected else "ACTIVE"
    assert await keep_moodle_session_alive(db, settings, principal_id) == expected
    await db.refresh(credential)
    assert credential.status == "ACTIVE"
    assert credential.encrypted_secret == "new-login-encrypted-state"
    assert credential.lease_owner == "new-session-owner"


async def test_student_can_acquire_save_lease_while_keepalive_is_in_flight(
    db, app_bundle, monkeypatch,
):
    from app.services.sync import ConnectionTarget, _principal_browser_credential_target

    settings = app_bundle[2]
    principal, credential = await seed(db, settings)
    principal_id, connection_id = principal.id, credential.connection_id
    original_secret = credential.encrypted_secret
    saved_owners = []

    async def ping(*_args):
        assert not db.in_transaction()
        target = await _principal_browser_credential_target(
            db, settings, ConnectionTarget(
                id=connection_id, base_url="https://moodle.example.test", service_token=None,
                mode="PLUGINLESS", transport="PLAYWRIGHT",
            ), principal_id,
        )
        saved_owners.append(target.browser_lease_owner)
        return state("must-not-overwrite-an-active-save")

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", ping)
    assert await keep_moodle_session_alive(db, settings, principal_id) == "ACTIVE"
    await db.refresh(credential)
    assert saved_owners[0] is not None
    assert credential.lease_owner == saved_owners[0]
    assert credential.encrypted_secret == original_secret


async def test_keepalive_endpoint_requires_auth_and_csrf_and_never_exposes_cookies(
    app_bundle, monkeypatch,
):
    app, session_factory, settings = app_bundle
    async with session_factory() as db:
        principal, _ = await seed(db, settings)
        session = await create_principal_session(db, principal.id, settings)
        await db.commit()

    async def ping(*_args):
        return state("private-cookie")

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", ping)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        headers = {"X-CSRFToken": token}
        response = await client.post("/api/v1/auth/moodle/keepalive", headers=headers)
        assert response.status_code == 401
        client.cookies.set(settings.session_cookie_name, session.bearer)
        assert (await client.post("/api/v1/auth/moodle/keepalive")).status_code == 403
        response = await client.post("/api/v1/auth/moodle/keepalive", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"status": "ACTIVE", "retry_after_seconds": 300}
        assert "private-cookie" not in response.text
        assert settings.session_cookie_name + "=" in response.headers["set-cookie"]
        assert "HttpOnly" in response.headers["set-cookie"]


async def test_heartbeat_does_not_extend_privileged_admin_elevation(app_bundle, monkeypatch):
    app, session_factory, settings = app_bundle
    async with session_factory() as db:
        principal, _ = await seed(db, settings)
        session = await create_principal_session(db, principal.id, settings)
        now = utcnow()
        elevation = AdminElevation(
            principal_id=principal.id, session_key=session.token_hash,
            granted_at=now, last_used_at=now - timedelta(minutes=5),
            expires_at=now + timedelta(minutes=2),
            absolute_expires_at=now + timedelta(hours=1), request_ip_prefix="",
        )
        db.add(elevation)
        await db.commit()
        elevation_id = elevation.id
        expires, last_used = elevation.expires_at, elevation.last_used_at

    async def ping(*_args):
        return state("refreshed")

    monkeypatch.setattr(MoodleBrowserClient, "keepalive", ping)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        token = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
        client.cookies.set(settings.session_cookie_name, session.bearer)
        result = await client.post(
            "/api/v1/auth/moodle/keepalive", headers={"X-CSRFToken": token},
        )
        assert result.status_code == 200
    async with session_factory() as db:
        saved = await db.get(AdminElevation, elevation_id)
        assert saved.expires_at.replace(tzinfo=None) == expires.replace(tzinfo=None)
        assert saved.last_used_at.replace(tzinfo=None) == last_used.replace(tzinfo=None)
