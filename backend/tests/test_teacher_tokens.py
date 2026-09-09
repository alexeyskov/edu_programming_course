from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.auth.sessions import create_principal_session
from app.core.security import hash_opaque_secret, hash_teacher_token, verify_teacher_token_hash
from app.core.teacher_token_crypto import (
    TeacherTokenDecryptionError,
    decrypt_teacher_token,
    encrypt_teacher_token,
)
from app.db.base import utcnow
from app.integrations.moodle_standard import (
    MoodleCourseMembership,
    MoodleStandardClient,
    TokenIdentity,
)
from app.models.courses import Course, CourseMembership
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    LoginTransaction,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import AuditEntry
from app.services.teacher_tokens import generate_teacher_token, teacher_token_public_id


def test_generated_teacher_token_has_exactly_eight_characters() -> None:
    public_id, token = generate_teacher_token()

    assert len(token) == 8
    assert token.isascii()
    assert re.fullmatch(r"[A-Za-z0-9]{8}", token)
    assert len(public_id) == 16
    assert teacher_token_public_id(token) == public_id


def test_legacy_teacher_token_selector_remains_supported() -> None:
    token = "edut_a1b2c3d4e5f60708_abcdefghijklmnopqrstuvwxyz123456"

    assert teacher_token_public_id(token) == "a1b2c3d4e5f60708"


def test_earlier_urlsafe_eight_character_token_remains_supported() -> None:
    token = "aB3-_xY9"

    assert teacher_token_public_id(token) is not None


def test_recoverable_teacher_token_ciphertext_is_row_bound() -> None:
    from app.core.config import Settings

    settings = Settings(
        debug=True,
        secret_key="test-secret-key-with-more-than-thirty-two-characters",
    )
    token_id = uuid.uuid4()
    encrypted = encrypt_teacher_token("aB3dE9xY", settings, token_id=token_id)

    assert "aB3dE9xY" not in encrypted
    assert decrypt_teacher_token(encrypted, settings, token_id=token_id) == "aB3dE9xY"
    try:
        decrypt_teacher_token(encrypted, settings, token_id=uuid.uuid4())
    except TeacherTokenDecryptionError:
        pass
    else:  # pragma: no cover - cryptographic binding must never be bypassed.
        raise AssertionError("ciphertext was accepted for a different teacher-token row")


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


async def _pluginless_connection(session_factory) -> LMSConnection:
    async with session_factory() as db:
        connection = LMSConnection(
            name="University Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={"auth_mode": "PLUGINLESS", "pluginless_transport": "MOBILE_TOKEN"},
        )
        db.add(connection)
        await db.flush()
        db.add_all(
            [
                Course(
                    connection_id=connection.id,
                    external_id="549",
                    title="C++",
                    catalog_enabled=True,
                ),
                Course(
                    connection_id=connection.id,
                    external_id="550",
                    title="Outside catalogue",
                    catalog_enabled=False,
                ),
            ]
        )
        await db.commit()
        return connection


async def test_teacher_token_pool_binds_once_persists_and_revokes_immediately(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    settings.dev_auth_enabled = True
    connection = await _pluginless_connection(session_factory)

    current_subject = "42"
    upstream_calls = 0

    async def authenticate(
        _self: MoodleStandardClient,
        username: str,
        _password: str,
    ) -> TokenIdentity:
        nonlocal upstream_calls
        upstream_calls += 1
        subject = current_subject if username == "teacher" else "99"
        return TokenIdentity(
            token=f"mobile-token-{subject}-1234567890",
            external_subject=subject,
            display_name="Коваленко Алексей" if subject == "42" else "Другой Пользователь",
            email="",
            locale="ru",
            courses=(
                # Upstream role is deliberately STUDENT: the platform token is
                # the global teacher boundary.
                MoodleCourseMembership("549", "C++", "CPP", "STUDENT"),
                MoodleCourseMembership("550", "Outside", "OUT", "TEACHER"),
            ),
            functions=frozenset(),
            upload_files=False,
        )

    monkeypatch.setattr(MoodleStandardClient, "authenticate", authenticate)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as admin_client:
        headers = await _csrf(admin_client)
        login = await admin_client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={
                "role": "STUDENT",
                "admin_token": "development-administrator-token",
            },
        )
        assert login.status_code == 201

        issued = await admin_client.post(
            "/api/v1/system/teacher-tokens",
            headers=headers,
            json={"label": "Коваленко А."},
        )
        assert issued.status_code == 201, issued.text
        assert issued.headers["cache-control"] == "no-store"
        assert issued.headers["pragma"] == "no-cache"
        issued_body = issued.json()
        raw_token = issued_body["token"]
        token_id = issued_body["id"]
        assert len(raw_token) == 8
        assert teacher_token_public_id(raw_token) == issued_body["public_id"]
        assert "secret_hash" not in issued_body

        listed = await admin_client.get("/api/v1/system/teacher-tokens")
        assert listed.status_code == 200
        assert raw_token not in listed.text
        assert "$argon2" not in listed.text
        assert listed.json()[0]["can_reveal"] is True
        assert listed.json()[0]["bound_principal_id"] is None
        revealed = await admin_client.get(f"/api/v1/system/teacher-tokens/{token_id}/secret")
        assert revealed.status_code == 200
        assert revealed.headers["cache-control"] == "no-store"
        assert revealed.headers["pragma"] == "no-cache"
        assert revealed.json() == {"token": raw_token}

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as teacher_client:
            teacher_headers = await _csrf(teacher_client)
            teacher_login = await teacher_client.post(
                f"/api/v1/auth/lms/{connection.id}/credentials",
                headers=teacher_headers,
                json={
                    "username": "teacher",
                    "password": "not-stored",
                    "teacher_token": raw_token,
                },
            )
            assert teacher_login.status_code == 201, teacher_login.text
            assert teacher_login.json()["roles"] == ["TEACHER"]
            assert teacher_login.json()["capabilities"] == []
            assert [row["course_name"] for row in teacher_login.json()["memberships"]] == ["C++"]

            # The binding persists; the raw token is not required again.
            relogin = await teacher_client.post(
                f"/api/v1/auth/lms/{connection.id}/credentials",
                headers=teacher_headers,
                json={"username": "teacher", "password": "not-stored-again"},
            )
            assert relogin.status_code == 201, relogin.text
            assert relogin.json()["roles"] == ["TEACHER"]

            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://testserver"
            ) as other_client:
                other_response = await other_client.post(
                    f"/api/v1/auth/lms/{connection.id}/credentials",
                    headers=await _csrf(other_client),
                    json={
                        "username": "other",
                        "password": "also-not-stored",
                        "teacher_token": raw_token,
                    },
                )
                assert other_response.status_code == 409
                assert other_response.json()["code"] == "TEACHER_TOKEN_ALREADY_BOUND"

            bound = await admin_client.get("/api/v1/system/teacher-tokens")
            assert bound.json()[0]["bound_display_name"] == "Коваленко Алексей"
            assert bound.json()[0]["use_count"] >= 1

            revoked = await admin_client.delete(
                f"/api/v1/system/teacher-tokens/{token_id}",
                headers=headers,
            )
            assert revoked.status_code == 204

            # Middleware and session projection re-evaluate the grant on every
            # request, so an already-open browser loses teacher access now.
            session = await teacher_client.get("/api/v1/auth/session")
            assert session.status_code == 200
            assert session.json()["roles"] == ["STUDENT"]
            assert session.json()["memberships"][0]["role"] == "STUDENT"

    assert upstream_calls == 3
    async with session_factory() as db:
        assert await db.scalar(select(TeacherAccessToken)) is None
        assert await db.scalar(select(TeacherTokenGrant)) is None
        memberships = list((await db.scalars(select(CourseMembership))).all())
        assert any(row.role == "STUDENT" and row.active for row in memberships)
        assert not any(row.role == "TEACHER" and row.active for row in memberships)
        audit_metadata = [
            row.metadata_json
            for row in (
                await db.scalars(
                    select(AuditEntry).where(
                        AuditEntry.action.in_(["teacher_token.created", "teacher_token.deleted"])
                    )
                )
            ).all()
        ]
        serialized_audit = json.dumps(audit_metadata, ensure_ascii=False)
        assert raw_token not in serialized_audit
        assert "$argon2" not in serialized_audit


async def test_admin_can_rotate_and_reveal_token_without_revoking_grant(
    app_bundle,
) -> None:
    app, session_factory, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    settings.dev_auth_enabled = True
    replacement = "Ab3Def90"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as admin_client:
        headers = await _csrf(admin_client)
        login = await admin_client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={"role": "STUDENT", "admin_token": "development-administrator-token"},
        )
        assert login.status_code == 201
        issued_response = await admin_client.post(
            "/api/v1/system/teacher-tokens",
            headers=headers,
            json={"label": "Редактируемый токен"},
        )
        assert issued_response.status_code == 201
        issued = issued_response.json()
        old_token = issued["token"]
        token_id = uuid.UUID(issued["id"])

        async with session_factory() as db:
            token = await db.get(TeacherAccessToken, token_id)
            assert token is not None
            creator = await db.get(ExternalPrincipal, token.created_by_id)
            assert creator is not None
            bound_principal = ExternalPrincipal(
                connection_id=creator.connection_id,
                external_subject="rotation-bound-teacher",
                display_name="Привязанный преподаватель",
                active=True,
            )
            db.add(bound_principal)
            await db.flush()
            grant = TeacherTokenGrant(token_id=token.id, principal_id=bound_principal.id)
            transaction = LoginTransaction(
                connection_id=creator.connection_id,
                state_hash=hash_opaque_secret("pending-rotation-state"),
                verifier_hash=hash_opaque_secret("pending-rotation-verifier"),
                teacher_token_id=token.id,
                expires_at=utcnow() + timedelta(minutes=5),
            )
            db.add_all([grant, transaction])
            await db.commit()
            principal_id = bound_principal.id
            transaction_id = transaction.id

        updated = await admin_client.patch(
            f"/api/v1/system/teacher-tokens/{token_id}",
            headers=headers,
            json={"token": replacement},
        )
        assert updated.status_code == 200, updated.text
        assert updated.headers["cache-control"] == "no-store"
        assert updated.json()["can_reveal"] is True
        assert updated.json()["bound_principal_id"] == str(principal_id)

        revealed = await admin_client.get(f"/api/v1/system/teacher-tokens/{token_id}/secret")
        assert revealed.status_code == 200
        assert revealed.json() == {"token": replacement}

    async with session_factory() as db:
        token = await db.get(TeacherAccessToken, token_id)
        assert token is not None
        assert verify_teacher_token_hash(replacement, token.secret_hash)
        assert not verify_teacher_token_hash(old_token, token.secret_hash)
        assert replacement not in (token.encrypted_secret or "")
        assert await db.scalar(
            select(TeacherTokenGrant).where(
                TeacherTokenGrant.token_id == token_id,
                TeacherTokenGrant.principal_id == principal_id,
            )
        )
        transaction = await db.get(LoginTransaction, transaction_id)
        assert transaction is not None
        assert transaction.teacher_token_id is None
        audit_rows = list(
            (
                await db.scalars(
                    select(AuditEntry).where(
                        AuditEntry.action.in_(["teacher_token.updated", "teacher_token.revealed"])
                    )
                )
            ).all()
        )
        serialized_audit = json.dumps(
            [row.metadata_json for row in audit_rows],
            ensure_ascii=False,
        )
        assert old_token not in serialized_audit
        assert replacement not in serialized_audit
        updated_audit = next(row for row in audit_rows if row.action == "teacher_token.updated")
        assert updated_audit.metadata_json["grant_preserved"] is True
        assert updated_audit.metadata_json["invalidated_login_transactions"] == 1


async def test_legacy_hash_only_token_stays_valid_and_can_be_made_recoverable(
    app_bundle,
) -> None:
    app, session_factory, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    settings.dev_auth_enabled = True
    legacy_token = "edut_a1b2c3d4e5f60708_abcdefghijklmnopqrstuvwxyz123456"

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as admin_client:
        headers = await _csrf(admin_client)
        login = await admin_client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={"role": "STUDENT", "admin_token": "development-administrator-token"},
        )
        assert login.status_code == 201
        async with session_factory() as db:
            creator = await db.scalar(select(ExternalPrincipal).order_by(ExternalPrincipal.id))
            assert creator is not None
            legacy = TeacherAccessToken(
                public_id="a1b2c3d4e5f60708",
                label="Старый токен",
                secret_hash=hash_teacher_token(legacy_token),
                encrypted_secret=None,
                created_by_id=creator.id,
            )
            db.add(legacy)
            await db.commit()
            legacy_id = legacy.id

        listed = await admin_client.get("/api/v1/system/teacher-tokens")
        legacy_item = next(row for row in listed.json() if row["id"] == str(legacy_id))
        assert legacy_item["can_reveal"] is False
        unavailable = await admin_client.get(f"/api/v1/system/teacher-tokens/{legacy_id}/secret")
        assert unavailable.status_code == 409
        assert unavailable.json()["code"] == "TEACHER_TOKEN_SECRET_UNAVAILABLE"

        replacement = "Legacy90"
        updated = await admin_client.patch(
            f"/api/v1/system/teacher-tokens/{legacy_id}",
            headers=headers,
            json={"token": replacement},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["can_reveal"] is True
        revealed = await admin_client.get(f"/api/v1/system/teacher-tokens/{legacy_id}/secret")
        assert revealed.json() == {"token": replacement}


async def test_teacher_token_rotation_rejects_non_ascii_or_punctuation(app_bundle) -> None:
    app, _, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        assert (
            await client.post(
                "/api/v1/auth/dev-login",
                headers=headers,
                json={"role": "STUDENT", "admin_token": "development-administrator-token"},
            )
        ).status_code == 201
        issued = (
            await client.post(
                "/api/v1/system/teacher-tokens",
                headers=headers,
                json={"label": "Validation"},
            )
        ).json()
        second = (
            await client.post(
                "/api/v1/system/teacher-tokens",
                headers=headers,
                json={"label": "Collision"},
            )
        ).json()
        for invalid in ("Ab_cd-90", "Абвг1234", "short"):
            response = await client.patch(
                f"/api/v1/system/teacher-tokens/{issued['id']}",
                headers=headers,
                json={"token": invalid},
            )
            assert response.status_code == 422
        collision = await client.patch(
            f"/api/v1/system/teacher-tokens/{second['id']}",
            headers=headers,
            json={"token": issued["token"]},
        )
        assert collision.status_code == 409
        assert collision.json()["code"] == "TEACHER_TOKEN_SELECTOR_COLLISION"


async def test_invalid_teacher_token_is_rate_limited_before_moodle(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.moodle_login_rate_limit_attempts = 1
    connection = await _pluginless_connection(session_factory)
    upstream_calls = 0

    async def must_not_authenticate(*_args, **_kwargs) -> TokenIdentity:
        nonlocal upstream_calls
        upstream_calls += 1
        raise AssertionError("Moodle must not receive an invalid teacher token attempt")

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
                "teacher_token": "edut_nonexistent_abcdefghijklmnopqrstuvwxyz123456",
            },
        )
        limited = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=headers,
            json={
                "username": "teacher",
                "password": "still-not-forwarded",
                "teacher_token": "another-invalid-token",
            },
        )

    assert rejected.status_code == 403
    assert rejected.json()["code"] == "INVALID_TEACHER_TOKEN"
    assert limited.status_code == 429
    assert upstream_calls == 0


async def test_principal_cannot_replace_its_bound_teacher_token(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    settings.dev_auth_enabled = True
    connection = await _pluginless_connection(session_factory)

    async def authenticate(
        _self: MoodleStandardClient,
        _username: str,
        _password: str,
    ) -> TokenIdentity:
        return TokenIdentity(
            token="mobile-token-42-1234567890",
            external_subject="42",
            display_name="Коваленко Алексей",
            email="",
            locale="ru",
            courses=(MoodleCourseMembership("549", "C++", "CPP", "STUDENT"),),
            functions=frozenset(),
            upload_files=False,
        )

    monkeypatch.setattr(MoodleStandardClient, "authenticate", authenticate)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as admin_client:
        admin_headers = await _csrf(admin_client)
        assert (
            await admin_client.post(
                "/api/v1/auth/dev-login",
                headers=admin_headers,
                json={
                    "role": "STUDENT",
                    "admin_token": "development-administrator-token",
                },
            )
        ).status_code == 201
        first = (
            await admin_client.post(
                "/api/v1/system/teacher-tokens",
                headers=admin_headers,
                json={"label": "Первый токен"},
            )
        ).json()
        second = (
            await admin_client.post(
                "/api/v1/system/teacher-tokens",
                headers=admin_headers,
                json={"label": "Второй токен"},
            )
        ).json()

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as teacher_client:
            teacher_headers = await _csrf(teacher_client)
            initial = await teacher_client.post(
                f"/api/v1/auth/lms/{connection.id}/credentials",
                headers=teacher_headers,
                json={
                    "username": "teacher",
                    "password": "not-stored",
                    "teacher_token": first["token"],
                },
            )
            assert initial.status_code == 201, initial.text

            replacement = await teacher_client.post(
                f"/api/v1/auth/lms/{connection.id}/credentials",
                headers=teacher_headers,
                json={
                    "username": "teacher",
                    "password": "not-stored-again",
                    "teacher_token": second["token"],
                },
            )
            assert replacement.status_code == 409, replacement.text
            assert replacement.json()["code"] == "TEACHER_PRINCIPAL_ALREADY_BOUND"

        listed = {
            row["id"]: row
            for row in (await admin_client.get("/api/v1/system/teacher-tokens")).json()
        }
        assert listed[first["id"]]["bound_display_name"] == "Коваленко Алексей"
        assert listed[second["id"]]["bound_principal_id"] is None

    async with session_factory() as db:
        grants = list((await db.scalars(select(TeacherTokenGrant))).all())
        assert len(grants) == 1
        assert str(grants[0].token_id) == first["id"]


async def test_empty_pool_never_authorizes_a_legacy_teacher_membership(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    connection = await _pluginless_connection(session_factory)
    async with session_factory() as db:
        course = await db.scalar(
            select(Course).where(
                Course.connection_id == connection.id,
                Course.external_id == "549",
            )
        )
        assert course is not None
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="legacy-teacher",
            display_name="Старый Преподаватель",
            active=True,
        )
        db.add(principal)
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role="TEACHER",
                active=True,
                external_revision="legacy-before-token-pool",
            )
        )
        credentials = await create_principal_session(db, principal.id, settings)
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, credentials.bearer)
        session = await client.get("/api/v1/auth/session")
        assert session.status_code == 200
        assert "TEACHER" not in session.json()["roles"]
        assert not session.json()["memberships"]

        teacher_only = await client.get(f"/api/v1/courses/{course.id}/memberships")
        assert teacher_only.status_code == 403


async def test_teacher_token_pool_requires_admin_elevation_and_csrf(app_bundle) -> None:
    app, _, settings = app_bundle
    settings.admin_token = SecretStr("development-administrator-token")
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        student = await client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={"role": "STUDENT"},
        )
        assert student.status_code == 201
        assert (await client.get("/api/v1/system/teacher-tokens")).status_code == 403
        assert (
            await client.post(
                "/api/v1/system/teacher-tokens",
                headers=headers,
                json={"label": "Forbidden"},
            )
        ).status_code == 403
        assert (
            await client.delete(
                f"/api/v1/system/teacher-tokens/{uuid.uuid4()}",
                headers=headers,
            )
        ).status_code == 403
        assert (
            await client.get(
                f"/api/v1/system/teacher-tokens/{uuid.uuid4()}/secret",
            )
        ).status_code == 403
        assert (
            await client.patch(
                f"/api/v1/system/teacher-tokens/{uuid.uuid4()}",
                headers=headers,
                json={"token": "Ab12Cd34"},
            )
        ).status_code == 403

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as admin_client:
        headers = await _csrf(admin_client)
        elevated = await admin_client.post(
            "/api/v1/auth/dev-login",
            headers=headers,
            json={
                "role": "STUDENT",
                "admin_token": "development-administrator-token",
            },
        )
        assert elevated.status_code == 201
        missing_csrf = await admin_client.post(
            "/api/v1/system/teacher-tokens",
            json={"label": "Missing CSRF"},
        )
        assert missing_csrf.status_code == 403
        missing_patch_csrf = await admin_client.patch(
            f"/api/v1/system/teacher-tokens/{uuid.uuid4()}",
            json={"token": "Ab12Cd34"},
        )
        assert missing_patch_csrf.status_code == 403


async def test_bridge_teacher_token_verification_is_rate_limited(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    settings.moodle_login_rate_limit_attempts = 1
    async with session_factory() as db:
        connection = LMSConnection(
            name="Moodle bridge",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={"auth_mode": "BRIDGE"},
        )
        db.add(connection)
        await db.commit()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        payload = {
            "teacher_token": "edut_nonexistent_abcdefghijklmnopqrstuvwxyz123456",
        }
        rejected = await client.post(
            f"/api/v1/auth/lms/{connection.id}/start",
            headers=headers,
            json=payload,
        )
        limited = await client.post(
            f"/api/v1/auth/lms/{connection.id}/start",
            headers=headers,
            json=payload,
        )

    assert rejected.status_code == 403
    assert rejected.json()["code"] == "INVALID_TEACHER_TOKEN"
    assert limited.status_code == 429
    assert limited.json()["code"] == "LOGIN_RATE_LIMITED"
