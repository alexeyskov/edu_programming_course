from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api.auth import _resume_outbox_after_moodle_reauthentication
from app.cli import build_parser
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    decrypt_moodle_browser_state,
)
from app.integrations.errors import IntegrationUnavailable
from app.integrations.moodle_browser import MoodleBrowserClient, MoodleBrowserLoginResult
from app.integrations.moodle_modes import moodle_pluginless_transport
from app.integrations.moodle_standard import (
    MoodleAuthenticationError,
    MoodleCourseMembership,
    TokenIdentity,
)
from app.models.attempts import Attempt
from app.models.courses import Course, CourseMembership
from app.models.enums import SyncOutboxState
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    MoodleCredential,
    MoodleLoginAttempt,
    PrincipalSession,
)
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


async def _connection(session_factory, *, transport: str = "PLAYWRIGHT") -> LMSConnection:
    async with session_factory() as db:
        connection = LMSConnection(
            name="MMCS Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={
                "auth_mode": "PLUGINLESS",
                "pluginless_transport": transport,
            },
        )
        db.add(connection)
        await db.commit()
        return connection


def _state(marker: str) -> dict[str, object]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": marker,
                "domain": "moodle.example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


def _identity(*, role: str = "STUDENT", extra_role: str = "UNKNOWN") -> TokenIdentity:
    return TokenIdentity(
        token="",
        external_subject="42",
        display_name="Ada Browser",
        email="ada@example.test",
        locale="ru",
        courses=(
            MoodleCourseMembership("549", "C++", "CPP", role),
            MoodleCourseMembership("550", "Outside catalogue", "OUT", extra_role),
        ),
        functions=frozenset(),
        upload_files=False,
    )


async def _catalog_course(
    session_factory,
    connection: LMSConnection,
    *,
    external_id: str = "549",
    title: str = "C++",
) -> Course:
    async with session_factory() as db:
        course = Course(
            connection_id=connection.id,
            external_id=external_id,
            title=title,
            short_name="CPP",
            sync_status="CURRENT",
            catalog_enabled=True,
        )
        db.add(course)
        await db.commit()
        return course


async def test_playwright_login_projects_identity_and_only_persists_encrypted_browser_state(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, settings = app_bundle
    connection = await _connection(session_factory)
    course = await _catalog_course(session_factory, connection)
    password = "browser-password-must-never-be-stored"
    calls = 0

    async def authenticate(
        _self: MoodleBrowserClient,
        username: str,
        supplied_password: str,
        *,
        allowed_course_ids: tuple[str, ...] = (),
    ) -> MoodleBrowserLoginResult:
        nonlocal calls
        calls += 1
        assert username == "student"
        assert supplied_password == password
        assert allowed_course_ids == ("549",)
        return MoodleBrowserLoginResult(
            # A compromised or stale connector response may contain another
            # LMS course. The backend must still enforce the global catalogue.
            identity=_identity(extra_role="TEACHER"),
            storage_state=_state(f"session-{calls}"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "student", "password": password},
        )
        second = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "student", "password": password},
        )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["roles"] == ["STUDENT"]
    assert second.json()["roles"] == ["STUDENT"]
    assert password not in first.text + second.text

    async with session_factory() as db:
        principal = await db.scalar(select(ExternalPrincipal))
        credentials = list((await db.scalars(select(MoodleCredential))).all())
        courses = list((await db.scalars(select(Course).order_by(Course.external_id))).all())
        session_count = int(await db.scalar(select(func.count(PrincipalSession.id))) or 0)
        assert principal is not None
        assert [row.external_id for row in courses] == ["549"]
        assert courses[0].id == course.id
        assert len(credentials) == 1
        credential = credentials[0]
        assert credential.kind == BROWSER_STATE_CREDENTIAL_KIND
        assert credential.revision == 2
        assert credential.metadata_json == {
            "auth_mode": "PLUGINLESS",
            "pluginless_transport": "PLAYWRIGHT",
            "state_schema": BROWSER_STATE_CREDENTIAL_KIND,
        }
        assert password not in credential.encrypted_secret
        assert decrypt_moodle_browser_state(
            credential.encrypted_secret,
            settings,
            connection_id=connection.id,
            principal_id=principal.id,
        ) == _state("session-2")
        assert session_count == 2


async def test_playwright_login_with_empty_catalogue_projects_no_lms_courses(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)

    async def authenticate(
        *_args, allowed_course_ids: tuple[str, ...] = (), **_kwargs
    ) -> MoodleBrowserLoginResult:
        assert allowed_course_ids == ()
        return MoodleBrowserLoginResult(
            identity=_identity(role="TEACHER", extra_role="STUDENT"),
            storage_state=_state("teacher-session"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "teacher", "password": "not-persisted"},
        )

    assert response.status_code == 201, response.text
    assert response.json()["roles"] == []
    async with session_factory() as db:
        assert await db.scalar(select(ExternalPrincipal)) is not None
        assert await db.scalar(select(Course)) is None
        credential = await db.scalar(select(MoodleCredential))
        assert credential is not None and credential.kind == BROWSER_STATE_CREDENTIAL_KIND


async def test_playwright_teacher_markup_does_not_bypass_global_teacher_token(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)
    async with session_factory() as db:
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Ada Before Teacher Evidence",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            short_name="CPP",
            sync_status="CURRENT",
            catalog_enabled=True,
        )
        db.add_all([principal, course])
        await db.flush()
        student = CourseMembership(
            course_id=course.id,
            principal_id=principal.id,
            role="STUDENT",
            active=True,
        )
        db.add(student)
        await db.commit()
        course_id = course.id
        principal_id = principal.id
        student_id = student.id

    async def authenticate(*_args, **_kwargs) -> MoodleBrowserLoginResult:
        return MoodleBrowserLoginResult(
            identity=_identity(role="TEACHER"),
            storage_state=_state("confirmed-teacher-session"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "teacher", "password": "not-persisted"},
        )

    assert response.status_code == 201, response.text
    assert response.json()["roles"] == ["STUDENT"]
    assert response.json()["memberships"] == [
        {
            "course_id": str(course_id),
            "course_name": "C++",
            "role": "STUDENT",
            "group_name": None,
        }
    ]
    async with session_factory() as db:
        student = await db.get(CourseMembership, student_id)
        teacher = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == course_id,
                CourseMembership.principal_id == principal_id,
                CourseMembership.role == "TEACHER",
            )
        )
    assert student is not None and student.active is True
    assert teacher is None


async def test_playwright_relogin_replaces_legacy_teacher_role_without_token(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)
    async with session_factory() as db:
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Ada Before Relogin",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            short_name="CPP",
            sync_status="CURRENT",
            catalog_enabled=True,
        )
        db.add_all([principal, course])
        await db.flush()
        membership = CourseMembership(
            course_id=course.id,
            principal_id=principal.id,
            role="TEACHER",
            active=True,
            external_revision="discovery-confirmed-v1",
        )
        db.add(membership)
        await db.commit()
        membership_id = membership.id

    async def authenticate(*_args, **_kwargs) -> MoodleBrowserLoginResult:
        return MoodleBrowserLoginResult(
            identity=_identity(role="UNKNOWN"),
            storage_state=_state("refreshed-teacher-session"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "teacher", "password": "not-persisted"},
        )

    assert response.status_code == 201, response.text
    assert response.json()["roles"] == ["STUDENT"]
    async with session_factory() as db:
        membership = await db.get(CourseMembership, membership_id)
        student = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == membership.course_id,
                CourseMembership.principal_id == membership.principal_id,
                CourseMembership.role == "STUDENT",
            )
        )
        assert membership is not None
        assert membership.active is False
        assert membership.external_revision == "discovery-confirmed-v1"
        assert student is not None and student.active is True
        assert student.external_revision == "pluginless-login"


async def test_playwright_relogin_resumes_only_checkpoint_events_blocked_by_expired_session(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)
    async with session_factory() as db, db.begin():
        principal = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Ada Before Reauthentication",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            sync_status="CURRENT",
            catalog_enabled=True,
        )
        db.add_all([principal, course])
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role="STUDENT",
                active=True,
            )
        )
        assessment = Assessment(
            course_id=course.id,
            title="Independent work",
            created_by_id=principal.id,
            status="PUBLISHED",
        )
        db.add(assessment)
        await db.flush()
        attempt = Attempt(
            assessment_id=assessment.id,
            principal_id=principal.id,
        )
        db.add(attempt)
        await db.flush()
        resumable = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"reauth-resume:{attempt.id}",
            state=SyncOutboxState.FAILED.value,
            attempts=8,
            last_error="MOODLE_AUTHENTICATION_FAILED: Moodle browser session expired",
        )
        unrelated = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"reauth-unrelated:{attempt.id}",
            state=SyncOutboxState.BLOCKED.value,
            attempts=1,
            last_error="QUIZ_ESSAY_MAPPING_REQUIRED: mapping is missing",
        )
        db.add_all([resumable, unrelated])
        await db.flush()
        resumable_id = resumable.id
        unrelated_id = unrelated.id

    async def authenticate(*_args, **_kwargs) -> MoodleBrowserLoginResult:
        return MoodleBrowserLoginResult(
            identity=_identity(),
            storage_state=_state("reauthenticated-session"),
        )

    monkeypatch.setattr(MoodleBrowserClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "student", "password": "not-persisted"},
        )

    assert response.status_code == 201, response.text
    async with session_factory() as db:
        resumed = await db.get(SyncOutbox, resumable_id)
        untouched = await db.get(SyncOutbox, unrelated_id)
        assert resumed is not None
        assert resumed.state == SyncOutboxState.PENDING.value
        assert resumed.attempts == 0
        assert resumed.last_error == ""
        assert resumed.locked_at is None
        assert untouched is not None
        assert untouched.state == SyncOutboxState.BLOCKED.value
        assert untouched.last_error.startswith("QUIZ_ESSAY_MAPPING_REQUIRED:")


async def test_playwright_relogin_resumes_teacher_history_import_for_the_same_course(
    app_bundle, monkeypatch
) -> None:
    _, session_factory, _ = app_bundle
    connection = await _connection(session_factory)
    async with session_factory() as db, db.begin():
        teacher = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Teacher",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add_all([teacher, course])
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=teacher.id,
                role="TEACHER",
                active=True,
            )
        )
        event = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"history-reauth:{course.id}",
            state=SyncOutboxState.BLOCKED.value,
            attempts=8,
            last_error="LMS_REAUTH_REQUIRED: Moodle browser session expired",
            payload={"actor_external_subject": "42"},
        )
        other_actor = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=uuid.uuid4(),
            idempotency_key=f"history-reauth-other:{course.id}",
            state=SyncOutboxState.BLOCKED.value,
            attempts=8,
            last_error="LMS_REAUTH_REQUIRED: Moodle browser session expired",
            payload={"actor_external_subject": "84"},
        )
        db.add_all([event, other_actor])
        await db.flush()
        teacher_id = teacher.id
        event_id = event.id
        other_actor_id = other_actor.id

    async def authorized(*_args, **_kwargs) -> bool:
        return True

    monkeypatch.setattr("app.api.auth.teacher_membership_is_authorized", authorized)
    async with session_factory() as db, db.begin():
        resumed = await _resume_outbox_after_moodle_reauthentication(
            db,
            connection_id=connection.id,
            principal_id=teacher_id,
        )

    async with session_factory() as db:
        stored = await db.get(SyncOutbox, event_id)
        other_stored = await db.get(SyncOutbox, other_actor_id)

    assert resumed == 1
    assert stored is not None
    assert stored.state == SyncOutboxState.PENDING.value
    assert stored.attempts == 0
    assert stored.last_error == ""
    assert other_stored is not None
    assert other_stored.state == SyncOutboxState.BLOCKED.value
    assert other_stored.attempts == 8


async def test_playwright_login_maps_credentials_and_browser_outage_without_secrets(
    app_bundle, monkeypatch
) -> None:
    app, session_factory, _ = app_bundle
    connection = await _connection(session_factory)

    async def authenticate(
        _self: MoodleBrowserClient,
        username: str,
        _password: str,
        *,
        allowed_course_ids: tuple[str, ...] = (),
    ) -> MoodleBrowserLoginResult:
        assert allowed_course_ids == ()
        if username == "invalid-user":
            raise MoodleAuthenticationError("safe rejection")
        raise IntegrationUnavailable("browser unavailable")

    monkeypatch.setattr(MoodleBrowserClient, "authenticate", authenticate)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        invalid = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "invalid-user", "password": "invalid-secret"},
        )
        unavailable = await client.post(
            f"/api/v1/auth/lms/{connection.id}/credentials",
            headers=await _csrf(client),
            json={"username": "another-user", "password": "outage-secret"},
        )

    assert invalid.status_code == 401
    assert invalid.json()["code"] == "INVALID_CREDENTIALS"
    assert "invalid-secret" not in invalid.text
    assert unavailable.status_code == 503
    assert unavailable.json()["code"] == "MOODLE_BROWSER_UNAVAILABLE"
    assert "outage-secret" not in unavailable.text
    async with session_factory() as db:
        assert await db.scalar(select(ExternalPrincipal)) is None
        assert await db.scalar(select(MoodleCredential)) is None
        assert await db.scalar(select(PrincipalSession)) is None
        attempts = list(
            (
                await db.scalars(
                    select(MoodleLoginAttempt).order_by(MoodleLoginAttempt.attempted_at)
                )
            ).all()
        )
        assert {attempt.outcome for attempt in attempts} == {
            "MOODLE_AUTHENTICATION_FAILED",
            "UNAVAILABLE",
        }


def test_pluginless_transport_resolver_and_bootstrap_default_are_explicit() -> None:
    legacy = LMSConnection(
        name="Legacy",
        provider="MOODLE",
        base_url="https://legacy.example.test",
        config={"auth_mode": "PLUGINLESS"},
    )
    browser = LMSConnection(
        name="Browser",
        provider="MOODLE",
        base_url="https://browser.example.test",
        config={"auth_mode": "PLUGINLESS", "pluginless_transport": "PLAYWRIGHT"},
    )
    mobile = LMSConnection(
        name="Mobile web service",
        provider="MOODLE",
        base_url="https://mobile.example.test",
        config={"auth_mode": "PLUGINLESS", "pluginless_transport": "MOBILE_TOKEN"},
    )
    parser = build_parser()
    bootstrap = parser.parse_args(
        [
            "bootstrap-connection",
            "--name",
            "Moodle",
            "--base-url",
            "https://moodle.example.test",
        ]
    )

    assert moodle_pluginless_transport(legacy) == "MOBILE_TOKEN"
    assert moodle_pluginless_transport(browser) == "PLAYWRIGHT"
    assert moodle_pluginless_transport(mobile) == "MOBILE_TOKEN"
    assert bootstrap.pluginless_transport == "PLAYWRIGHT"
