from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.api import auth as auth_module
from app.api.auth import _project_pluginless_identity, _upsert_launch_identity
from app.db.base import utcnow
from app.integrations.moodle_standard import MoodleCourseMembership, TokenIdentity
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.enums import CourseRole
from app.models.identity import ExternalPrincipal, LMSConnection


@pytest.mark.parametrize("previous_active", [None, False, True])
@pytest.mark.parametrize("live_access", [False, True])
async def test_login_does_not_import_or_reactivate_manual_roster(
    db, previous_active: bool | None, live_access: bool,
):
    connection = LMSConnection(
        provider="MOODLE", name="Moodle", base_url="https://moodle.example.test",
    )
    db.add(connection)
    await db.flush()
    course = Course(
        connection_id=connection.id, external_id="549", title="C++", catalog_enabled=True,
    )
    principal = ExternalPrincipal(
        connection_id=connection.id, external_subject="77", display_name="Student",
    )
    db.add_all([course, principal])
    await db.flush()
    if previous_active is not None:
        membership = CourseMembership(
            course_id=course.id, principal_id=principal.id,
            role="STUDENT", active=previous_active,
        )
        db.add(membership)
        await db.flush()
        group = CourseGroup(course_id=course.id, external_id="9", name="Manual roster group")
        db.add(group)
        await db.flush()
        db.add(CourseMembershipGroup(coursemembership_id=membership.id, coursegroup_id=group.id))
        await db.flush()
    identity = TokenIdentity(
        token="", external_subject="77", display_name="Student", email="", locale="ru",
        courses=(MoodleCourseMembership("549", "C++", "CPP", "STUDENT"),) if live_access else (),
        functions=frozenset(), upload_files=False,
    )

    await _project_pluginless_identity(db, connection=connection, identity=identity)

    memberships = (await db.scalars(select(CourseMembership))).all()
    groups = (await db.scalars(select(CourseMembershipGroup))).all()
    if previous_active is None:
        assert memberships == [] and groups == []
    else:
        assert len(memberships) == 1
        assert memberships[0].active is (previous_active and live_access)
        # Logging in neither discovers new groups nor replaces manually read
        # membership groups, even when live access is revoked.
        assert len(groups) == 1


@pytest.mark.parametrize("previous", ["absent", "inactive", "expired", "active"])
@pytest.mark.parametrize("teacher_token", [False, True])
async def test_signed_launch_obeys_manual_roster_and_only_switches_imported_member_role(
    db, monkeypatch, previous: str, teacher_token: bool,
):
    connection = LMSConnection(
        provider="MOODLE", name="Moodle", base_url="https://moodle.example.test",
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id, external_subject="77", display_name="Student",
    )
    course = Course(
        connection_id=connection.id, external_id="549", title="C++", catalog_enabled=True,
    )
    db.add_all([principal, course])
    await db.flush()
    previous_membership = None
    if previous != "absent":
        previous_membership = CourseMembership(
            course_id=course.id, principal_id=principal.id, role="STUDENT",
            active=previous != "inactive",
            valid_until=utcnow() - timedelta(days=1) if previous == "expired" else None,
        )
        db.add(previous_membership)
        await db.flush()

    async def token_for_principal(*_args):
        return SimpleNamespace(id=principal.id) if teacher_token else None

    monkeypatch.setattr(auth_module, "teacher_token_for_principal", token_for_principal)
    await _upsert_launch_identity(
        db, connection=connection, subject="77", display_name="Student",
        role=CourseRole.TEACHER, course_external_id=549,
    )
    await db.flush()
    memberships = (await db.scalars(select(CourseMembership))).all()
    if previous == "absent":
        assert memberships == []
    elif previous == "inactive":
        assert memberships == [previous_membership] and not memberships[0].active
    elif previous == "expired":
        assert memberships == [previous_membership]
        assert memberships[0].valid_until is not None
    else:
        active = [row for row in memberships if row.active]
        assert len(active) == 1
        assert active[0].role == ("TEACHER" if teacher_token else "STUDENT")
