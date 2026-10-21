from __future__ import annotations

import importlib
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient

from app.api.courses import _project_course
from app.auth.sessions import create_principal_session
from app.models.courses import Course, CourseMembership
from app.models.identity import ExternalPrincipal
from app.models.integration import AuditEntry
from app.services.sync import _apply_course_discovery
from tests.test_moodle_browser_courses import _csrf, _discovery, _seed_teacher


async def _course_for(sessions, connection_id):
    async with sessions() as db:
        return await db.scalar(sa.select(Course).where(Course.connection_id == connection_id))


async def test_comment_is_local_shared_with_both_roles_and_does_not_rename_moodle(app_bundle):
    app, sessions, settings = app_bundle
    connection, bearer, _ = await _seed_teacher(sessions, settings)
    course = await _course_for(sessions, connection.id)
    async with sessions() as db, db.begin():
        student = ExternalPrincipal(
            connection_id=connection.id, external_subject="77", display_name="Student"
        )
        db.add(student)
        await db.flush()
        db.add(CourseMembership(course_id=course.id, principal_id=student.id, role="STUDENT"))
        session = await create_principal_session(db, student.id, settings)
        student_bearer = session.bearer
        other = Course(
            connection_id=connection.id, external_id="200", title=course.title, catalog_enabled=True
        )
        db.add(other)
        await db.flush()
        other_id = other.id
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        headers = await _csrf(client)
        for _ in range(2):  # An unchanged save must not duplicate the audit record.
            response = await client.patch(
                f"/api/v1/system/course-catalog/{course.id}",
                headers=headers,
                json={"comment": "  Осень   2026\n "},
            )
            assert response.status_code == 200, response.text
            assert response.json()["comment"] == "Осень 2026"
            assert response.json()["title"] == course.title
        catalog = (await client.get("/api/v1/system/course-catalog")).json()
        assert next(row for row in catalog if row["id"] == str(other_id))["comment"] == ""
        teacher_courses = (await client.get("/api/v1/courses")).json()
        assert (
            next(row for row in teacher_courses if row["id"] == str(course.id))["comment"]
            == "Осень 2026"
        )
        client.cookies.set(settings.session_cookie_name, student_bearer)
        student_courses = (await client.get("/api/v1/courses")).json()
        assert student_courses[0]["comment"] == "Осень 2026"
    async with sessions() as db:
        stored = await db.get(Course, course.id)
        assert stored.title == course.title and stored.comment == "Осень 2026"
        logs = list(
            (
                await db.scalars(
                    sa.select(AuditEntry).where(
                        AuditEntry.action == "course_catalog.comment_updated",
                    )
                )
            ).all()
        )
        assert len(logs) == 1 and logs[0].metadata_json == {"has_comment": True}


async def test_comment_survives_both_background_discovery_and_explicit_reimport(app_bundle):
    _, sessions, settings = app_bundle
    connection, _, _ = await _seed_teacher(sessions, settings)
    async with sessions() as db, db.begin():
        course = await db.scalar(sa.select(Course).where(Course.connection_id == connection.id))
        teacher = await db.scalar(
            sa.select(ExternalPrincipal).where(
                ExternalPrincipal.connection_id == connection.id,
            )
        )
        course.comment = "Осень 2026"
        original_id = course.id
        discovery = _discovery(course.external_id, revision=2)
        await _apply_course_discovery(db, course.id, discovery, actor_external_subject="42")
        assert course.comment == "Осень 2026" and course.title == "C++ course revision 2"
        discovery = _discovery(course.external_id, revision=3)
        updated = await _project_course(
            db,
            connection=connection,
            preview=discovery.preview,
            capabilities=discovery.capabilities,
            created_by_id=teacher.id,
            actor_external_subject="42",
        )
        assert updated.id == original_id
        assert updated.comment == "Осень 2026" and updated.title == "C++ course revision 3"


async def test_admin_can_clear_comment_without_removing_course(app_bundle):
    app, sessions, settings = app_bundle
    connection, bearer, _ = await _seed_teacher(sessions, settings)
    course = await _course_for(sessions, connection.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        headers = await _csrf(client)
        url = f"/api/v1/system/course-catalog/{course.id}"
        assert (
            await client.patch(url, headers=headers, json={"comment": "Весна 2026"})
        ).status_code == 200
        response = await client.patch(url, headers=headers, json={"comment": "  "})
        assert response.status_code == 200 and response.json()["comment"] == ""
    async with sessions() as db:
        stored = await db.get(Course, course.id)
        assert stored.catalog_enabled and stored.title == course.title and stored.comment == ""


@pytest.mark.parametrize("role", ["TEACHER", "STUDENT"])
async def test_only_elevated_admin_can_write_comment(app_bundle, role):
    app, sessions, settings = app_bundle
    connection, bearer, _ = await _seed_teacher(sessions, settings, elevated=False)
    course = await _course_for(sessions, connection.id)
    if role == "STUDENT":
        async with sessions() as db, db.begin():
            principal = await db.scalar(
                sa.select(ExternalPrincipal).where(
                    ExternalPrincipal.connection_id == connection.id,
                )
            )
            membership = await db.scalar(
                sa.select(CourseMembership).where(
                    CourseMembership.course_id == course.id,
                )
            )
            membership.role = "STUDENT"
            session = await create_principal_session(db, principal.id, settings)
            bearer = session.bearer
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.patch(
            f"/api/v1/system/course-catalog/{course.id}",
            headers=await _csrf(client),
            json={"comment": "forbidden"},
        )
        assert response.status_code == 403
    assert (await _course_for(sessions, connection.id)).comment == ""


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"comment": None},
        {"comment": 12},
        {"comment": "x" * 256},
        {"comment": "hidden\x00text"},
        {"comment": "note", "title": "Do not rename"},
    ],
)
async def test_invalid_comment_or_other_course_mutation_is_rejected(app_bundle, payload):
    app, sessions, settings = app_bundle
    connection, bearer, _ = await _seed_teacher(sessions, settings)
    course = await _course_for(sessions, connection.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.patch(
            f"/api/v1/system/course-catalog/{course.id}", headers=await _csrf(client), json=payload
        )
        assert response.status_code == 422, response.text
    stored = await _course_for(sessions, connection.id)
    assert stored.comment == "" and stored.title == course.title


async def test_comment_write_requires_csrf_and_an_existing_catalog_course(app_bundle):
    app, sessions, settings = app_bundle
    connection, bearer, _ = await _seed_teacher(sessions, settings)
    course = await _course_for(sessions, connection.id)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        client.cookies.set(settings.session_cookie_name, bearer)
        response = await client.patch(
            f"/api/v1/system/course-catalog/{course.id}", json={"comment": "no csrf"}
        )
        assert response.status_code == 403
        response = await client.patch(
            f"/api/v1/system/course-catalog/{uuid.uuid4()}",
            headers=await _csrf(client),
            json={"comment": "missing"},
        )
        assert response.status_code == 404
    assert (await _course_for(sessions, connection.id)).comment == ""


def test_migration_backfills_empty_comments_and_preserves_existing_courses():
    migration = importlib.import_module("app.db.migrations.versions.20261020_0029_course_comment")
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "core_course",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
    )
    original_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(table.insert().values(id=original_id, title="Keep original title"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert connection.scalar(sa.text("SELECT comment FROM core_course")) == ""
            assert connection.scalar(sa.select(table.c.id)) == original_id
            assert connection.scalar(sa.select(table.c.title)) == "Keep original title"
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert "comment" not in {
                column["name"] for column in sa.inspect(connection).get_columns("core_course")
            }
            assert connection.scalar(sa.select(table.c.id)) == original_id
    finally:
        engine.dispose()
