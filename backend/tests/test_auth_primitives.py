from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.auth.context import CurrentAuth
from app.auth.login_verifier import create_login_secrets, verify_login_verifier
from app.auth.sessions import create_principal_session
from app.core.config import Settings
from app.core.security import hash_admin_token, verify_admin_token
from app.models.courses import Course, CourseMembership
from app.models.identity import ExternalPrincipal, LMSConnection, PrincipalSession
from app.services.common import DomainError
from app.services.policy import require_membership


async def test_server_side_session_authenticates_and_cookie_secret_is_not_stored(app_bundle):
    app, session_factory, settings = app_bundle
    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle", provider="MOODLE", base_url="https://lms.example.test"
        )
        db.add(connection)
        await db.flush()
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="student-1",
            display_name="Student One",
        )
        db.add(principal)
        await db.flush()
        course = Course(
            connection_id=connection.id,
            external_id="course-1",
            title="C++",
            catalog_enabled=True,
        )
        db.add(course)
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role="STUDENT",
            )
        )
        credentials = await create_principal_session(db, principal.id, settings)
        await db.commit()

    @app.get("/api/v1/_test/auth")
    async def authenticated(context: CurrentAuth):
        return {"principal_id": str(context.principal_id), "roles": context.roles}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, credentials.bearer)
        response = await client.get("/api/v1/_test/auth")

    assert response.status_code == 200
    assert response.json() == {"principal_id": str(principal.id), "roles": ["STUDENT"]}
    async with session_factory() as db:
        stored = await db.scalar(select(PrincipalSession))
    assert stored is not None
    assert stored.token_hash != credentials.bearer


async def test_disabling_lms_connection_revokes_existing_session_and_course_access(app_bundle):
    app, session_factory, settings = app_bundle
    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle", provider="MOODLE", base_url="https://disabled-lms.example.test"
        )
        db.add(connection)
        await db.flush()
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="student-disabled-connection",
            display_name="Student",
        )
        db.add(principal)
        await db.flush()
        course = Course(
            connection_id=connection.id,
            external_id="course-disabled",
            title="C++",
            catalog_enabled=True,
        )
        db.add(course)
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role="STUDENT",
            )
        )
        credentials = await create_principal_session(db, principal.id, settings)
        connection_id = connection.id
        session_id = credentials.session_id
        await db.commit()

    @app.get("/api/v1/_test/disabled-connection")
    async def authenticated(context: CurrentAuth):
        return {"principal_id": str(context.principal_id)}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, credentials.bearer)
        assert (await client.get("/api/v1/courses")).status_code == 200

        async with session_factory() as db:
            connection = await db.get(LMSConnection, connection_id)
            assert connection is not None
            connection.enabled = False
            await db.commit()

        denied = await client.get("/api/v1/courses")
        assert denied.status_code == 401
        assert denied.json()["code"] == "AUTHENTICATION_REQUIRED"
        cleared_cookie = denied.headers.get("set-cookie", "")
        assert f"{settings.session_cookie_name}=" in cleared_cookie
        assert "Max-Age=0" in cleared_cookie

        async with session_factory() as db:
            session_row = await db.get(PrincipalSession, session_id)
            assert session_row is not None
            assert session_row.revoked_at is not None
            connection = await db.get(LMSConnection, connection_id)
            assert connection is not None
            connection.enabled = True
            await db.commit()

        # Replaying a retained bearer after re-enabling the connector must not revive it.
        client.cookies.clear()
        client.cookies.set(settings.session_cookie_name, credentials.bearer)
        assert (await client.get("/api/v1/_test/disabled-connection")).status_code == 401


async def test_membership_policy_rejects_disabled_lms_connection(db):
    connection = LMSConnection(
        name="Disabled Moodle",
        provider="MOODLE",
        base_url="https://disabled-membership.example.test",
        enabled=False,
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="student-policy",
        display_name="Student",
    )
    course = Course(
        connection_id=connection.id,
        external_id="policy-course",
        title="C++",
        catalog_enabled=True,
    )
    db.add_all([principal, course])
    await db.flush()
    db.add(
        CourseMembership(
            course_id=course.id,
            principal_id=principal.id,
            role="STUDENT",
        )
    )
    await db.flush()

    with pytest.raises(DomainError) as caught:
        await require_membership(db, principal_id=principal.id, course_id=course.id)

    assert caught.value.status_code == 403
    assert caught.value.code == "COURSE_MEMBERSHIP_REQUIRED"


def test_admin_token_uses_argon2id_and_plaintext_is_dev_only():
    token = "a-long-administrator-token-for-tests"
    encoded = hash_admin_token(token)
    production = Settings(
        debug=False,
        secret_key="production-secret-key-with-more-than-thirty-two-characters",
        admin_token_hash=encoded,
    )
    development = Settings(
        debug=True,
        secret_key="development-secret-key-with-more-than-thirty-two-characters",
        admin_token=token,
    )

    assert encoded.startswith("$argon2id$")
    assert verify_admin_token(token, production)
    assert not verify_admin_token("wrong-token", production)
    assert verify_admin_token(token, development)


def test_malformed_admin_token_hash_fails_closed() -> None:
    with pytest.raises(ValueError, match="Argon2id verifier"):
        Settings(
            debug=False,
            secret_key="production-secret-key-with-more-than-thirty-two-characters",
            admin_token_hash="plain-text-is-not-a-hash",
        )

    malformed = Settings(
        debug=False,
        secret_key="production-secret-key-with-more-than-thirty-two-characters",
        admin_token_hash="$argon2id$malformed",
    )
    assert not verify_admin_token("a-long-administrator-token-for-tests", malformed)


def test_login_state_requires_separate_browser_verifier():
    login_secrets = create_login_secrets()

    assert login_secrets.state != login_secrets.verifier
    assert verify_login_verifier(login_secrets.verifier, login_secrets.verifier_hash)
    assert not verify_login_verifier(login_secrets.state, login_secrets.verifier_hash)
