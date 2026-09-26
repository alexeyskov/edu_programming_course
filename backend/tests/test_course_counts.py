from sqlalchemy import select

from app.api.courses import _course_payload
from app.models.courses import Course, CourseMembership, CourseSection
from app.models.tasks import Assessment, AvailabilityRule
from app.services.moodle_materialization import materialize_moodle_activity_drafts
from tests.test_attempt_review_services import _seed_course


async def test_counts_are_per_course_even_with_identical_course_titles(db):
    _, teacher, _, first_work = await _seed_course(db)
    first = await db.get(Course, first_work.course_id)
    second = Course(connection_id=first.connection_id, external_id="second", title=first.title)
    db.add(second)
    await db.flush()
    member = CourseMembership(course_id=second.id, principal_id=teacher.id, role="TEACHER")
    db.add_all(
        [
            member,
            Assessment(course_id=second.id, created_by_id=teacher.id, title="One"),
            Assessment(course_id=second.id, created_by_id=teacher.id, title="Two"),
            Assessment(
                course_id=second.id, created_by_id=teacher.id, title="Closed", status="CLOSED"
            ),
            Assessment(
                course_id=second.id,
                created_by_id=teacher.id,
                title="Internal question",
                policy={"historical_import_only": True},
            ),
        ]
    )
    await db.flush()
    first_member = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == first.id,
            CourseMembership.principal_id == teacher.id,
        )
    )
    first_payload = await _course_payload(db, first_member, first, teacher=True)
    second_payload = await _course_payload(db, member, second, teacher=True)
    assert first_payload["active_count"] == 1
    assert second_payload["active_count"] == 2


async def test_archive_section_activities_are_counted_but_not_published(db):
    _, teacher, _, first_work = await _seed_course(db)
    course = await db.get(Course, first_work.course_id)
    section = CourseSection(course_id=course.id, external_id="archive", title="АРХИВ")
    db.add(section)
    await db.flush()
    activities = [
        {
            "module": "quiz",
            "cmid": 123,
            "name": "Экзамен 2024",
            "section_external_id": "archive",
            "title_confirmed": True,
        }
    ]
    assert (
        await materialize_moodle_activity_drafts(
            db,
            course=course,
            activities=activities,
            created_by_id=teacher.id,
        )
        == 1
    )
    assert (
        await materialize_moodle_activity_drafts(
            db,
            course=course,
            activities=activities,
            created_by_id=teacher.id,
        )
        == 0
    )
    imported = await db.scalar(select(Assessment).where(Assessment.section_id == section.id))
    assert imported.status == "DRAFT" and imported.type == "EXAM"
    assert (
        await db.scalar(
            select(AvailabilityRule).where(
                AvailabilityRule.assessment_id == imported.id,
            )
        )
        is None
    )
    member = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == teacher.id,
        )
    )
    assert (await _course_payload(db, member, course, teacher=True))["active_count"] == 2
