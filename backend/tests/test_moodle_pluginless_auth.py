from __future__ import annotations

import asyncio

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.core.credential_crypto import decrypt_moodle_credential
from app.integrations.errors import IntegrationUnavailable
from app.integrations.moodle_standard import (
    MoodleAuthenticationError,
    MoodleCourseMembership,
    MoodleStandardClient,
    MoodleWebServicesDisabled,
    TokenIdentity,
)
from app.models.courses import Course, CourseMembership
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    MoodleCredential,
    MoodleLoginAttempt,
    PrincipalSession,
)
from app.models.integration import AuditEntry


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


async def _connection(session_factory) -> LMSConnection:
    async with session_factory() as db:
        row = LMSConnection(
            name="University Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={
                "auth_mode": "PLUGINLESS",
                "pluginless_transport": "MOBILE_TOKEN",
            },
        )
        db.add(row)
        await db.commit()
        return row


async def test_pluginless_login_stores_only_bound_encrypted_token(app_bundle, monkeypatch) -> None:
    app, session_factory, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    connection = await _connection(session_factory)
    async with session_factory() as db:
        db.add_all(
            [
                Course(
                    connection_id=connection.id,
                    external_id="549",
                    title="C++",
                    short_name="CPP",
                    catalog_enabled=True,
                ),
                Course(
                    connection_id=connection.id,
                    external_id="550",
                    title="Not admitted",
                    catalog_enabled=False,
                ),
            ]
        )
        await db.commit()
    upstream_password = "never-store-this-password"

    async def authenticate(
        _self: MoodleStandardClient, username: str, password: str
    ) -> TokenIdentity:
        assert username == "teacher"
        assert password == upstream_password
        return TokenIdentity(
            token="mobile-token-1234567890abcdef",
            external_subject="42",
            display_name="Ada Teacher",
            email="ada@example.test",
            locale="ru",
            courses=(
                MoodleCourseMembership("549", "C++", "CPP", "TEACHER"),
                MoodleCourseMembership("550", "Algorithms", "ALG", "STUDENT"),
            ),
            functions=frozenset({"core_enrol_get_users_courses", "mod_assign_save_grade"}),
            upload_files=True,
        )

    monkeypatch.setattr(MoodleStandardClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        connections = await client.get("/api/v1/auth/connections")
        assert connections.status_code == 200
        assert connections.json()[0]["login_mode"] == "CREDENTIALS"
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={
                "username": "teacher",
                "password": upstream_password,
                "admin_token": "development-administrator-token",
            },
        )
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["roles"] == ["STUDENT"]
        assert len(response.json()["memberships"]) == 1
        assert response.json()["memberships"][0]["course_name"] == "C++"
        assert response.json()["capabilities"] == ["SYSTEM_SETTINGS"]
        assert settings.session_cookie_name in client.cookies

    async with session_factory() as db:
        principal = await db.scalar(select(ExternalPrincipal))
        credential = await db.scalar(select(MoodleCredential))
        attempt = await db.scalar(select(MoodleLoginAttempt))
        local_session = await db.scalar(select(PrincipalSession))
        audit = await db.scalar(
            select(AuditEntry).where(AuditEntry.action == "auth.moodle_pluginless_login")
        )
        memberships = list((await db.scalars(select(CourseMembership))).all())
        assert principal is not None and credential is not None
        assert attempt is not None and attempt.succeeded and attempt.outcome == "SUCCESS"
        assert local_session is not None and audit is not None
        assert [membership.role for membership in memberships] == ["STUDENT"]
        assert upstream_password not in credential.encrypted_secret
        assert credential.metadata_json["upload_files"] is True
        assert (
            decrypt_moodle_credential(
                credential.encrypted_secret,
                settings,
                connection_id=connection.id,
                principal_id=principal.id,
                kind=credential.kind,
            )
            == "mobile-token-1234567890abcdef"
        )


async def test_pluginless_login_has_generic_failures_and_database_rate_limit(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    settings.moodle_login_rate_limit_attempts = 1
    connection = await _connection(session_factory)

    async def reject(*_args, **_kwargs) -> TokenIdentity:
        raise MoodleAuthenticationError("safe rejection")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", reject)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        rejected = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={"username": "student", "password": "wrong-secret-password"},
        )
        limited = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={"username": "student", "password": "another-secret-password"},
        )
        assert rejected.status_code == 401
        assert rejected.json()["code"] == "INVALID_CREDENTIALS"
        assert "wrong-secret-password" not in rejected.text
        assert limited.status_code == 429
        assert "another-secret-password" not in limited.text

    async with session_factory() as db:
        assert await db.scalar(select(ExternalPrincipal)) is None
        assert await db.scalar(select(MoodleCredential)) is None
        assert await db.scalar(select(PrincipalSession)) is None


async def test_invalid_admin_token_is_reserved_and_rate_limited_before_moodle(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    settings.admin_token = SecretStr("valid-administrator-token")
    settings.moodle_login_rate_limit_attempts = 1
    connection = await _connection(session_factory)
    upstream_calls = 0

    async def must_not_authenticate(*_args, **_kwargs) -> TokenIdentity:
        nonlocal upstream_calls
        upstream_calls += 1
        raise AssertionError("Moodle must not receive an invalid admin-token attempt")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", must_not_authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        rejected = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={
                "username": "teacher",
                "password": "not-forwarded",
                "admin_token": "invalid-administrator-token",
            },
        )
        limited = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={
                "username": "teacher",
                "password": "still-not-forwarded",
                "admin_token": "another-invalid-token",
            },
        )

    assert rejected.status_code == 403
    assert rejected.json()["code"] == "INVALID_ADMIN_TOKEN"
    assert limited.status_code == 429
    assert upstream_calls == 0
    async with session_factory() as db:
        attempt = await db.scalar(select(MoodleLoginAttempt))
        assert attempt is not None
        assert attempt.outcome == "INVALID_ADMIN_TOKEN"


async def test_pluginless_disabled_service_and_validation_never_echo_password(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)

    async def disabled(*_args, **_kwargs) -> TokenIdentity:
        raise MoodleWebServicesDisabled("safe disabled service")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", disabled)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        invalid = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={"username": "x" * 300, "password": "validation-secret-password"},
        )
        unavailable = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={"username": "student", "password": "service-secret-password"},
        )
        assert invalid.status_code == 422
        assert "validation-secret-password" not in invalid.text
        assert unavailable.status_code == 503
        assert unavailable.json()["code"] == "MOODLE_WEB_SERVICE_UNAVAILABLE"
        assert "service-secret-password" not in unavailable.text


async def test_explicit_mobile_transport_unavailable_explains_playwright_migration(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)

    async def unavailable(*_args, **_kwargs) -> TokenIdentity:
        raise IntegrationUnavailable("External HTTP request returned status 403")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", unavailable)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "student", "password": "never-echo-this-password"},
        )

    assert response.status_code == 503
    assert response.json()["code"] == "MOODLE_WEB_SERVICE_UNAVAILABLE"
    assert "Playwright" in response.json()["message"]
    assert "never-echo-this-password" not in response.text


async def test_pluginless_login_rate_limit_reservation_is_atomic_under_burst(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    settings.moodle_login_rate_limit_attempts = 1
    connection = await _connection(session_factory)
    upstream_calls = 0

    async def reject(*_args, **_kwargs) -> TokenIdentity:
        nonlocal upstream_calls
        upstream_calls += 1
        await asyncio.sleep(0.05)
        raise MoodleAuthenticationError("safe rejection")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", reject)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/auth/lms/{connection.id}/credentials",
                    headers=headers,
                    json={"username": "same-user", "password": f"wrong-{index}"},
                )
                for index in range(12)
            )
        )

    statuses = [response.status_code for response in responses]
    assert statuses.count(401) == 1
    assert statuses.count(429) == 11
    assert upstream_calls == 1
    async with session_factory() as db:
        attempts = list((await db.scalars(select(MoodleLoginAttempt))).all())
        assert len(attempts) == 1
        assert attempts[0].succeeded is False


async def test_pluginless_login_has_separate_network_budget_for_shared_nat(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    settings.moodle_login_rate_limit_attempts = 1
    settings.moodle_login_network_rate_limit_attempts = 16
    connection = await _connection(session_factory)
    upstream_usernames: list[str] = []

    async def reject(_self: MoodleStandardClient, username: str, _password: str) -> TokenIdentity:
        upstream_usernames.append(username)
        raise MoodleAuthenticationError("safe rejection")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", reject)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        responses = await asyncio.gather(
            *(
                client.post(
                    f"/api/v1/auth/lms/{connection.id}/credentials",
                    headers=headers,
                    json={"username": f"student-{index}", "password": "wrong"},
                )
                for index in range(20)
            )
        )

    statuses = [response.status_code for response in responses]
    assert statuses.count(401) == 16
    assert statuses.count(429) == 4
    assert len(set(upstream_usernames)) == 16
    async with session_factory() as db:
        attempts = list((await db.scalars(select(MoodleLoginAttempt))).all())
        assert len(attempts) == 16


async def test_pluginless_password_login_fails_closed_without_https(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    connection = await _connection(session_factory)
    settings.debug = False
    upstream_calls = 0

    async def must_not_authenticate(*_args, **_kwargs) -> TokenIdentity:
        nonlocal upstream_calls
        upstream_calls += 1
        raise AssertionError("Moodle must not receive credentials over an HTTP application request")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", must_not_authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={"username": "teacher", "password": "transport-secret-password"},
        )

    assert response.status_code == 400
    assert response.json()["code"] == "HTTPS_REQUIRED"
    assert "transport-secret-password" not in response.text
    assert upstream_calls == 0
    async with session_factory() as db:
        assert await db.scalar(select(MoodleLoginAttempt)) is None


async def test_pluginless_password_login_can_explicitly_allow_temporary_http(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    connection = await _connection(session_factory)
    settings.debug = False
    settings.moodle_credential_login_allow_insecure_http = True
    upstream_calls = 0

    async def reject(*_args, **_kwargs) -> TokenIdentity:
        nonlocal upstream_calls
        upstream_calls += 1
        raise MoodleAuthenticationError("safe rejection")

    monkeypatch.setattr(MoodleStandardClient, "authenticate", reject)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={"username": "teacher", "password": "temporary-http-password"},
        )

    assert response.status_code == 401
    assert response.json()["code"] == "INVALID_CREDENTIALS"
    assert "temporary-http-password" not in response.text
    assert upstream_calls == 1
    async with session_factory() as db:
        attempt = await db.scalar(select(MoodleLoginAttempt))
        assert attempt is not None and attempt.outcome == "MOODLE_AUTHENTICATION_FAILED"
