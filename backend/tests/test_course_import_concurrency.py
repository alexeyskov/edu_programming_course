from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.core.credential_crypto import decrypt_moodle_browser_state, encrypt_moodle_browser_state
from app.integrations.moodle_browser import MoodleBrowserClient, MoodleBrowserDiscoveryResult
from app.integrations.moodle_standard import MoodleAuthenticationError
from app.models.courses import Course, CourseImportJob
from app.models.identity import MoodleCredential
from tests.test_moodle_browser_courses import _csrf, _discovery, _seed_teacher, _state


@pytest.mark.parametrize("worker_finishes", [False, True])
async def test_add_course_during_another_course_sync_preserves_worker_session(
    app_bundle,
    monkeypatch,
    worker_finishes,
):
    app, sessions, settings = app_bundle
    connection, bearer, credential = await _seed_teacher(
        sessions,
        settings,
        credential_mode="busy",
    )
    async with sessions() as db, db.begin():
        other = await db.scalar(select(Course).where(Course.external_id == "100"))
        other.sync_status = "SYNCING"
        other_id = other.id
    calls = []

    async def discover(browser, external_id, actor, *, interactive=False):
        calls.append(external_id)
        assert interactive and actor == "42"
        assert browser.storage_state == _state("initial-session")
        async with sessions() as db, db.begin():
            current = await db.get(MoodleCredential, credential.id)
            assert current.lease_owner == "another-request"
            if worker_finishes:
                # A background request finished after the import read its
                # snapshot. Its newer cookies must win over this late response.
                current.revision += 1
                current.lease_owner = None
                current.lease_expires_at = None
                current.encrypted_secret = encrypt_moodle_browser_state(
                    _state("worker-refreshed"),
                    settings,
                    connection_id=connection.id,
                    principal_id=current.principal_id,
                )
        return MoodleBrowserDiscoveryResult(
            discovery=_discovery(external_id, revision=1),
            storage_state=_state("late-import-cookies"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", discover)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        imported = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
        assert imported.status_code == 201, imported.text
        confirmed = await client.post(
            f"/api/v1/course-imports/{imported.json()['id']}/confirm",
            headers=await _csrf(client),
            json={},
        )
        assert confirmed.status_code == 200, confirmed.text
    assert calls == ["549"]
    async with sessions() as db:
        current = await db.get(MoodleCredential, credential.id)
        assert current.status == "ACTIVE"
        assert current.revision == (2 if worker_finishes else 1)
        assert current.lease_owner == (None if worker_finishes else "another-request")
        assert decrypt_moodle_browser_state(
            current.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=current.principal_id,
        ) == _state("worker-refreshed" if worker_finishes else "initial-session")
        other = await db.get(Course, other_id)
        assert other.sync_status == "SYNCING"
        assert other.title == "Existing teacher course"
        assert await db.scalar(select(Course).where(Course.external_id == "549")) is not None


async def test_rejected_parallel_import_does_not_expire_another_workers_lease(
    app_bundle,
    monkeypatch,
):
    app, sessions, settings = app_bundle
    _, bearer, credential = await _seed_teacher(sessions, settings, credential_mode="busy")
    calls = []

    async def rejected(*_args, **_kwargs):
        calls.append(True)
        raise MoodleAuthenticationError("session rejected by fixture")

    monkeypatch.setattr(MoodleBrowserClient, "discover_course", rejected)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.post(
            "/api/v1/course-imports",
            headers=await _csrf(client),
            json={"url": "https://moodle.example.test/course/view.php?id=549"},
        )
    assert calls == [True]
    assert response.status_code == 502 and response.json()["code"] == "NOT_CONFIGURED"
    async with sessions() as db:
        current = await db.get(MoodleCredential, credential.id)
        assert current.status == "ACTIVE" and current.revision == 1
        assert current.lease_owner == "another-request"
        assert current.lease_expires_at is not None
        job = await db.scalar(select(CourseImportJob))
        assert job.state == "FAILED"
