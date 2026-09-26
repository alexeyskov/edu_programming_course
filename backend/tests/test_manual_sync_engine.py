from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.courses import Course, CourseMembership, CourseSection
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment
from app.services.manual_sync import assessment_sync_statuses, queue_manual_assessment_sync
from app.services.moodle_materialization import merge_moodle_course_index
from app.services.sync import (
    _BlockedDelivery,
    _BrowserDeliveryResult,
    _prepare_grade,
    process_outbox_once,
)
from tests.test_moodle_history import _browser_state, _seed_history_target
from tests.test_sync_workers import _seed_app_quiz_grade_delivery


async def test_course_index_keeps_details_but_does_not_restore_absent_activities(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        mapping = await db.scalar(select(ExternalMapping))
        old = {
            "module": "quiz",
            "cmid": 777,
            "name": "Old title",
            "description": "Saved task",
            "schedule_confirmed": True,
            "grade_confirmed": True,
            "grade_max": 10,
            "due_at_epoch": 1800001000,
            "section_external_id": "s1",
        }
        mapping.metadata_json = {**mapping.metadata_json, "activity": old}
        await db.flush()
        merged = await merge_moodle_course_index(
            db,
            course=course,
            activities=[
                {
                    "module": "quiz",
                    "cmid": 777,
                    "name": "New title",
                    "title_confirmed": True,
                    "grade_confirmed": False,
                    "grade_max": 0,
                    "section_external_id": "s2",
                }
            ],
        )
        assert merged == [
            {**old, "name": "New title", "title_confirmed": True, "section_external_id": "s2"}
        ]
        assert await merge_moodle_course_index(db, course=course, activities=[]) == []


@pytest.mark.parametrize("incomplete", [False, True])
async def test_manual_activity_metadata_is_isolated_and_partial_reads_keep_old_data(
    app_bundle,
    incomplete,
):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        section = CourseSection(course_id=course.id, external_id="s1", title="Лабораторные")
        db.add(section)
        await db.flush()
        assessment.section_id = section.id
        mapping = await db.scalar(select(ExternalMapping))
        mapping.metadata_json = {**mapping.metadata_json, "managed_by": "MOODLE_ACTIVITY_IMPORT"}
        other = Assessment(course_id=course.id, title="Other task", created_by_id=ids["teacher_id"])
        db.add(other)
        await db.flush()
        other_mapping = ExternalMapping(
            connection_id=ids["connection_id"],
            local_id=other.id,
            local_type="Assessment",
            external_id="778",
            external_type="mod_quiz",
            metadata_json={"module": "quiz", "cmid": 778, "sync_state": "CURRENT"},
        )
        db.add(other_mapping)
        course.policies = {
            "lms_activities": [
                {"module": "quiz", "cmid": 777, "name": assessment.title},
                {"module": "quiz", "cmid": 778, "name": other.title},
            ]
        }
        original_title = assessment.title
        await db.flush()
        await queue_manual_assessment_sync(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
        )
        other_id = other_mapping.id

    class Connector:
        async def discover_historical_submissions(self, payload):
            assert payload["include_activity_metadata"] is True
            assert payload["manual_run_id"]
            return _BrowserDeliveryResult(
                value={
                    "scan_only": True,
                    "items": [],
                    "candidates": [],
                    "complete": True,
                    "next_cursor": None,
                    "warnings": ["ACTIVITY_METADATA_INCOMPLETE"] if incomplete else [],
                    "activity_metadata": {
                        "module": "quiz",
                        "cmid": 777,
                        "name": "Updated selected task",
                        "title_confirmed": True,
                        "grade_max": 10,
                        "grade_confirmed": True,
                        "description": "Updated condition",
                        "statement_confirmed": True,
                    },
                },
                storage_state=_browser_state("after-manual"),
            )

    assert await process_outbox_once(
        sessions,
        settings,
        bridge_factory=lambda *_: Connector(),
        history_imports_only=True,
    )
    async with sessions() as db:
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert assessment.title == (original_title if incomplete else "Updated selected task")
        assert (await db.get(ExternalMapping, other_id)).metadata_json["sync_state"] == "CURRENT"
        rows = await assessment_sync_statuses(db, course_id=ids["course_id"])
        current = next(row for row in rows if row["assessment_id"] == assessment.id)
        assert current["status"] == ("PARTIAL" if incomplete else "COMPLETED")
        if incomplete:
            assert current["error_code"] == "ACTIVITY_METADATA_INCOMPLETE"
        if not incomplete:
            course = await db.get(Course, ids["course_id"])
            assert course.policies["lms_activities"][0]["name"] == "Updated selected task"
            assert course.policies["lms_activities"][0]["section_external_id"] == "s1"


@pytest.mark.parametrize("removed", ["student", "activity"])
async def test_grade_export_of_removed_target_blocks_without_moodle_write(app_bundle, removed):
    _, sessions, settings = app_bundle
    claim, target, _, _, assessment_id = await _seed_app_quiz_grade_delivery(sessions, settings)
    async with sessions() as db, db.begin():
        if removed == "student":
            membership = await db.scalar(select(CourseMembership))
            membership.active = False
        else:
            mapping = await db.scalar(
                select(ExternalMapping).where(
                    ExternalMapping.local_id == assessment_id,
                )
            )
            mapping.metadata_json = {**mapping.metadata_json, "sync_state": "MISSING_IN_MOODLE"}
    async with sessions() as db:
        with pytest.raises(_BlockedDelivery) as caught:
            await _prepare_grade(db, settings, claim, target)
        assert caught.value.code == "LMS_EXPORT_UNAVAILABLE"
        assert "Свяжитесь с администратором" in str(caught.value)
