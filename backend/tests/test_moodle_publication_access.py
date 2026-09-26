from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.api.authoring import publish_assessment
from app.models.courses import CourseGroup
from app.models.integration import ExternalMapping
from app.models.tasks import AvailabilityRule
from app.schemas.assessments import AssessmentPublishRequest
from app.services.common import DomainError
from app.services.delivery_profile import resolve_assessment_workspace_delivery_profile
from app.services.moodle_quiz_runtime import resolve_moodle_quiz_context
from app.services.policy import effective_assessment_policy, require_membership
from tests.test_attempt_review_services import _map_deferred_random_quiz, _seed_course
from tests.test_student_work_continuation import auth


async def _managed_target(db):
    student, teacher, _, assessment = await _seed_course(db)
    await _map_deferred_random_quiz(db, assessment)
    assessment.policy = {"moodle_metadata_read_only": True}
    mapping = await db.scalar(
        select(ExternalMapping).where(ExternalMapping.local_id == assessment.id)
    )
    mapping.metadata_json = {
        **mapping.metadata_json,
        "managed_by": "MOODLE_ACTIVITY_IMPORT",
        "sync_state": "CURRENT",
        "submission_mode": "ESSAY_ATTACHMENT",
        "activity": {
            **mapping.metadata_json["activity"],
            "quiz_grading_method": "LAST",
            "quiz_grading_method_confirmed": True,
        },
    }
    group = await db.scalar(
        select(CourseGroup).where(CourseGroup.course_id == assessment.course_id)
    )
    db.add(
        AvailabilityRule(
            assessment_id=assessment.id,
            target_type="GROUP",
            target_external_id=group.external_id,
            allowed=True,
            authored_by_id=teacher.id,
        )
    )
    await db.flush()
    return student, teacher, assessment, mapping, group


@pytest.mark.parametrize("grading_confirmed", [True, False])
async def test_teacher_can_revoke_all_groups_and_restore_access(db, grading_confirmed):
    student, teacher, assessment, mapping, group = await _managed_target(db)
    if not grading_confirmed:
        mapping.metadata_json = {**mapping.metadata_json, "activity": {"module": "quiz"}}
    result = await publish_assessment(
        assessment.id,
        AssessmentPublishRequest(group_ids=[]),
        auth(teacher),
        db,
    )
    assert result.status == "PUBLISHED" and result.availability_rules == []
    membership = await require_membership(
        db,
        course_id=assessment.course_id,
        principal_id=student.id,
        role="STUDENT",
    )
    with pytest.raises(DomainError) as denied:
        await effective_assessment_policy(db, assessment, membership)
    assert denied.value.code == "ASSESSMENT_NOT_ASSIGNED"
    mapping.metadata_json = {
        **mapping.metadata_json,
        "activity": {
            "module": "quiz",
            "quiz_grading_method": "LAST",
            "quiz_grading_method_confirmed": True,
        },
    }
    await publish_assessment(
        assessment.id,
        AssessmentPublishRequest(group_ids=[group.id]),
        auth(teacher),
        db,
    )
    await effective_assessment_policy(db, assessment, membership)


async def test_teacher_changes_only_own_groups_but_admin_can_revoke_every_group(db):
    _, teacher, assessment, _, group = await _managed_target(db)
    db.add(CourseGroup(course_id=assessment.course_id, external_id="other", name="Other group"))
    db.add(
        AvailabilityRule(
            assessment_id=assessment.id,
            target_type="GROUP",
            target_external_id="other",
            allowed=True,
            authored_by_id=teacher.id,
        )
    )
    await db.flush()
    for selected, expected in [([], {"other"}), ([group.id], {"other", group.external_id})]:
        result = await publish_assessment(
            assessment.id,
            AssessmentPublishRequest(group_ids=selected),
            auth(teacher),
            db,
        )
        assert {rule.target_external_id for rule in result.availability_rules} == expected
    result = await publish_assessment(
        assessment.id,
        AssessmentPublishRequest(group_ids=[]),
        replace(auth(teacher), capabilities=("SYSTEM_SETTINGS",)),
        db,
    )
    assert result.availability_rules == []


@pytest.mark.parametrize(("status", "group_ids"), [("DRAFT", []), ("PUBLISHED", None)])
async def test_empty_initial_publication_and_unspecified_targets_still_rejected(
    db, status, group_ids
):
    _, teacher, assessment, _, _ = await _managed_target(db)
    assessment.status = status
    with pytest.raises(HTTPException) as denied:
        await publish_assessment(
            assessment.id,
            AssessmentPublishRequest(group_ids=group_ids),
            auth(teacher),
            db,
        )
    assert denied.value.detail["code"] == "MOODLE_PUBLICATION_TARGETS_REQUIRED"


@pytest.mark.parametrize("local_type", ["Assessment", "core.assessment"])
@pytest.mark.parametrize(
    "marker_type", ["moodle_deleted_quiz_attempt", "moodle_attempt_observation"]
)
async def test_legacy_attempt_markers_are_not_activity_bindings(db, local_type, marker_type):
    _, _, assessment, mapping, _ = await _managed_target(db)
    db.add(
        ExternalMapping(
            connection_id=mapping.connection_id,
            local_id=assessment.id,
            local_type=local_type,
            external_type=marker_type,
            external_id="quiz:30354:141716",
            metadata_json={"module": "quiz", "cmid": 30354, "remote_attempt_id": "141716"},
        )
    )
    await db.flush()
    context = await resolve_moodle_quiz_context(db, assessment.id)
    assert context is not None and context.mapping.id == mapping.id
    delivery = await resolve_assessment_workspace_delivery_profile(db, assessment.id)
    assert delivery.external and delivery.profile is not None
    assert delivery.mapping.id == mapping.id


async def test_two_real_activity_bindings_still_fail_closed(db):
    _, _, assessment, mapping, _ = await _managed_target(db)
    db.add(
        ExternalMapping(
            connection_id=mapping.connection_id,
            local_id=assessment.id,
            local_type="Assessment",
            external_type="mod_quiz",
            external_id="30355",
            metadata_json=mapping.metadata_json,
        )
    )
    await db.flush()
    with pytest.raises(DomainError) as denied:
        await resolve_moodle_quiz_context(db, assessment.id)
    assert denied.value.code == "MOODLE_RUNTIME_MAPPING_UNAVAILABLE"
    delivery = await resolve_assessment_workspace_delivery_profile(db, assessment.id)
    assert delivery.external and delivery.profile is None
