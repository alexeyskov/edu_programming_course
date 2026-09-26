"""Read-only local diagnosis of imported answers missing from a teacher's queue.

No Moodle requests, changes, source files, names, tokens or session secrets.
Can be piped into an already running worker without rebuilding containers.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import uuid
from collections import Counter
from datetime import UTC

from sqlalchemy import select, text

import app.models  # noqa: F401 -- register mappings before querying
from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import create_engine, create_session_factory
from app.models.attempts import Attempt, Submission
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.identity import ExternalPrincipal
from app.models.tasks import Assessment
from app.services.moodle_attempt_selection import (
    is_completed_moodle_submission,
    latest_reviewable_moodle_submission_ids,
)
from app.services.policy import (
    assessment_review_scope_ids,
    legacy_subgroup_is_assigned_to_teacher,
    visible_submission_ids_for_review,
)
from app.services.teacher_tokens import teacher_membership_is_authorized


async def read_history_report(db, assessment_id, teacher_subject):
    assessment = await db.get(Assessment, assessment_id)
    if assessment is None:
        return {"diagnosis": "ASSESSMENT_NOT_FOUND"}
    course = await db.get(Course, assessment.course_id)
    teacher = await db.scalar(select(ExternalPrincipal).where(
        ExternalPrincipal.connection_id == course.connection_id,
        ExternalPrincipal.external_subject == teacher_subject,
    ))
    if teacher is None:
        return {"diagnosis": "TEACHER_NOT_FOUND"}
    scope = await assessment_review_scope_ids(db, assessment.id)
    rows = list((await db.execute(
        select(Submission, Attempt, Assessment)
        .join(Attempt, Attempt.id == Submission.attempt_id)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .where(Assessment.id.in_(scope))
    )).all())
    visible = await visible_submission_ids_for_review(
        db, principal_id=teacher.id, assessment_id=assessment.id,
    )
    system_visible = await visible_submission_ids_for_review(
        db, principal_id=teacher.id, assessment_id=assessment.id, allow_system_settings_read=True,
    )
    eligible = [row for row in rows if row[0].id in system_visible]
    latest = await latest_reviewable_moodle_submission_ids(db, eligible)
    current = {row[0].id for row in eligible if (
        not is_completed_moodle_submission(*row) or row[0].id in latest
    )}
    groups = list((await db.scalars(select(CourseGroup).where(
        CourseGroup.course_id == course.id, CourseGroup.active.is_(True),
    ))).all())
    membership = await db.scalar(select(CourseMembership).where(
        CourseMembership.course_id == course.id, CourseMembership.principal_id == teacher.id,
        CourseMembership.role == "TEACHER",
    ))
    memberships = list((await db.scalars(select(CourseMembership).where(
        CourseMembership.course_id == course.id, CourseMembership.role == "STUDENT",
        CourseMembership.active.is_(True),
    ))).all())
    links = list((await db.execute(select(
        CourseMembershipGroup.coursemembership_id, CourseMembershipGroup.coursegroup_id,
    ).where(CourseMembershipGroup.coursegroup_id.in_([group.id for group in groups])))).all())
    authorized = await teacher_membership_is_authorized(db, teacher.id)
    teacher_active = bool(membership and membership.active and (
        membership.valid_until is None
        or membership.valid_until.replace(tzinfo=membership.valid_until.tzinfo or UTC) > utcnow()
    ))
    reasons = []
    if not rows:
        reasons.append("NO_LOCAL_SUBMISSIONS")
    if not course.catalog_enabled or course.archived_at:
        reasons.append("COURSE_NOT_VISIBLE")
    if not teacher.active or not authorized or not teacher_active:
        reasons.append("TEACHER_NOT_AUTHORIZED")
    if rows and not eligible:
        reasons.append("NO_ACTIVE_SUBMISSIONS")
    if eligible and not visible:
        reasons.append("NO_SUBMISSIONS_IN_TEACHER_GROUP_SCOPE")
    if visible and not (visible & current):
        reasons.append("NEWER_ATTEMPTS_HIDE_IMPORTED_SUBMISSIONS")
    grouped_memberships = {member_id for member_id, _ in links}
    return {
        "assessment_id": assessment.id,
        "course_id": course.id,
        "diagnosis": reasons or ["REVIEWABLE_ANSWERS_PRESENT"],
        "answers": {
            "stored": len(rows),
            "students": len({row[1].principal_id for row in rows}),
            "attempt_states": dict(Counter(row[1].state for row in rows)),
            "visible_with_system_access": len(system_visible),
            "visible_in_teacher_groups": len(visible),
            "latest_with_system_access": len(current),
            "latest_in_teacher_groups": len(current & visible),
        },
        "teacher": {
            "active": teacher.active,
            "token_authorized": authorized,
            "course_membership_active": teacher_active,
            "explicit_groups": len({group_id for member_id, group_id in links
                                    if membership and member_id == membership.id}),
            "named_subgroups": sum(legacy_subgroup_is_assigned_to_teacher(
                group.name, teacher.display_name,
            ) for group in groups),
        },
        "roster": {
            "active_groups": len(groups),
            "active_students": len(memberships),
            "active_students_with_groups": sum(
                member.id in grouped_memberships for member in memberships
            ),
        },
    }


async def diagnose(assessment_id, teacher_subject):
    logging.disable(logging.CRITICAL)
    engine = create_engine(Settings(db_echo=False))
    try:
        async with create_session_factory(engine)() as db:
            if engine.dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await db.execute(text("SET LOCAL statement_timeout = '5s'"))
            report = await read_history_report(db, assessment_id, teacher_subject)
        print(json.dumps(report, default=str, ensure_ascii=False, indent=2), flush=True)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assessment_id", type=uuid.UUID)
    parser.add_argument("teacher_subject")
    args = parser.parse_args()
    try:
        asyncio.run(diagnose(args.assessment_id, args.teacher_subject))
    except Exception as exc:
        print(json.dumps({"diagnostic_error_type": type(exc).__name__}), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
