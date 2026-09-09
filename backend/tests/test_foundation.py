from __future__ import annotations

from httpx import ASGITransport, AsyncClient
from sqlalchemy import inspect

from app.core.config import Settings
from app.db.base import Base
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.identity import ExternalPrincipal, LMSConnection


def test_comma_separated_origin_settings_are_decoded_before_validation(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", "code.example.edu, localhost,backend")
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://code.example.edu, http://localhost:8080",
    )

    settings = Settings(_env_file=None)

    assert settings.allowed_hosts == ["code.example.edu", "localhost", "backend"]
    assert settings.cors_allowed_origins == [
        "https://code.example.edu",
        "http://localhost:8080",
    ]


def test_json_origin_settings_remain_supported(monkeypatch):
    monkeypatch.setenv("ALLOWED_HOSTS", '["localhost","127.0.0.1","backend"]')
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", '["http://localhost:8080"]')

    settings = Settings(_env_file=None)

    assert settings.allowed_hosts == ["localhost", "127.0.0.1", "backend"]
    assert settings.cors_allowed_origins == ["http://localhost:8080"]


async def test_health_and_readiness_check_database(app_bundle):
    app, _, _ = app_bundle
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        health = await client.get("/api/v1/system/health")
        readiness = await client.get("/api/v1/system/readiness")

    assert health.status_code == 200
    assert health.json()["status"] == "ok"
    assert health.json()["services"][0]["name"] == "database"
    assert readiness.status_code == 200
    assert health.headers["X-Request-ID"]


async def test_lifespan_runs_terminal_checkpoint_safety_worker(app_bundle):
    app, _, settings = app_bundle
    settings.sync_embedded_terminal_worker_enabled = True

    async with app.router.lifespan_context(app):
        task = app.state.terminal_checkpoint_worker_task
        assert task is not None
        assert not task.done()
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            health = await client.get("/api/v1/system/health")

        assert health.status_code == 200
        assert {
            service["name"]: service["status"] for service in health.json()["services"]
        }["terminal-checkpoint-worker"] == "ok"

    assert task.done()


async def test_complete_metadata_is_created(app_bundle):
    app, _, _ = app_bundle

    async with app.state.engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync: set(inspect(sync).get_table_names()))

    assert table_names == set(Base.metadata.tables)
    assert len(table_names) == 48
    assert "core_attempt" in table_names
    assert "core_principalsession" in table_names
    assert "core_coursemembership_groups" in table_names
    assert "core_moodlecredential" in table_names
    assert "core_moodleloginattempt" in table_names


async def test_csrf_double_submit_contract(app_bundle):
    app, _, _ = app_bundle
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        issued = await client.get("/api/v1/auth/csrf")
        token = issued.json()["csrf_token"]
        accepted_by_csrf = await client.post(
            "/api/v1/not-implemented", headers={"X-CSRFToken": token}
        )

    assert issued.status_code == 200
    assert issued.headers["X-CSRFToken"] == token
    assert accepted_by_csrf.status_code == 404


async def test_membership_group_through_table_autoincrements_on_sqlite(db):
    connection = LMSConnection(name="LMS", provider="MOCK", base_url="https://mock.test")
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="subject",
        display_name="User",
    )
    course = Course(connection_id=connection.id, external_id="course", title="Course")
    db.add_all([principal, course])
    await db.flush()
    membership = CourseMembership(
        course_id=course.id,
        principal_id=principal.id,
        role="STUDENT",
    )
    group = CourseGroup(course_id=course.id, external_id="group", name="Group")
    db.add_all([membership, group])
    await db.flush()
    association = CourseMembershipGroup(
        coursemembership_id=membership.id,
        coursegroup_id=group.id,
    )
    db.add(association)
    await db.flush()

    assert association.id > 0
