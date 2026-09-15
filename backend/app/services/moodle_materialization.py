from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.integrations.moodle_transport import confirmed_moodle_activity_answer_transport
from app.models.courses import Course, CourseSection
from app.models.enums import AssessmentStatus, AssessmentType, TaskScope, TaskVersionStatus
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment, AssessmentItem, TaskBankItem, TaskVersion
from app.services.common import canonical_hash, positive_decimal
from app.services.moodle_source import (
    moodle_source_confirmation_from_activity,
    moodle_source_is_confirmed,
    moodle_statement_is_deferred,
)

_SUPPORTED_MODULES = {"assign", "quiz"}
_TYPE_WORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (AssessmentType.EXAM.value, ("экзамен", "exam")),
    (AssessmentType.INDEPENDENT.value, ("самостоятель", "independent")),
    (
        AssessmentType.CONTROL.value,
        ("контрольн", "проверочн", "control"),
    ),
    (AssessmentType.LAB.value, ("лаборатор", "практич", "lab")),
)
_ARCHIVE_WORDS = ("архив", "archive")


def classify_moodle_assessment(title: str, section_title: str) -> str | None:
    evidence = re.sub(r"\s+", " ", f"{section_title} {title}".casefold()).strip()
    if any(marker in evidence for marker in _ARCHIVE_WORDS):
        return None
    for assessment_type, markers in _TYPE_WORDS:
        if any(marker in evidence for marker in markers):
            return assessment_type
    return None


def _epoch(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value or 0)
        return datetime.fromtimestamp(number, UTC) if number > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _bounded(value: object, maximum: int) -> str:
    return str(value or "").strip()[:maximum]


def _task_content(version: TaskVersion) -> dict[str, Any]:
    return {
        "title": version.title,
        "statement": version.statement,
        "language": version.language,
        "language_standard": version.language_standard,
        "multi_file": version.multi_file,
        "starter_files": version.starter_files,
        "build_profile": version.build_profile,
        "public_examples": version.public_examples,
        "hidden_test_manifest": version.hidden_test_manifest,
        "max_score": format(version.max_score.quantize(Decimal("0.01")), "f"),
        "difficulty": version.difficulty,
        "ai_policy": version.ai_policy,
    }


async def _available_slug(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    module: str,
    cmid: int,
) -> str:
    prefix = f"moodle-{module}-{cmid}"
    candidates = [prefix, *(f"{prefix}-{index}" for index in range(2, 101))]
    occupied = set(
        (
            await db.scalars(
                select(TaskBankItem.slug).where(
                    TaskBankItem.course_id == course_id,
                    TaskBankItem.slug.in_(candidates),
                )
            )
        ).all()
    )
    for candidate in candidates:
        if candidate not in occupied:
            return candidate
    # A course cannot reasonably contain one hundred local collisions for one
    # Moodle cmid.  The digest keeps the final fallback deterministic.
    return f"{prefix}-{canonical_hash(str(course_id))[:12]}"[:160]


def _mapping_activity(activity: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "cmid",
        "instance_id",
        "module",
        "name",
        "visible",
        "user_visible",
        "section_external_id",
        "url",
        "opens_at_epoch",
        "due_at_epoch",
        "cutoff_at_epoch",
        "grade_max",
        "duration_seconds",
        "attempt_limit",
        "attempt_limit_unlimited",
        "quiz_grading_method",
        "quiz_grading_method_confirmed",
        "quiz_questions_confirmed",
        "question_count",
        "essay_question_count",
        "random_question_count",
        "random_essay_confirmed",
        "statement_deferred",
        "import_supported",
        "answer_transport",
        "available_answer_transports",
        "submission_drafts",
        "requires_submission_statement",
        "max_submission_files",
        "max_submission_bytes",
        "max_submission_bytes_inherited",
        "accepted_file_types",
        "file_types_confirmed",
        "team_submission",
        "title_confirmed",
        "settings_confirmed",
        "statement_confirmed",
        "schedule_confirmed",
        "duration_confirmed",
        "grade_confirmed",
        "attempt_policy_confirmed",
        "user_overrides",
        "user_overrides_confirmed",
    }
    return {key: activity[key] for key in allowed if key in activity}


def _statement_from_moodle(activity: dict[str, Any]) -> str:
    description = _bounded(activity.get("description"), 50_000)
    if not description and moodle_statement_is_deferred(activity):
        # A random Moodle slot has no canonical statement before the LMS binds
        # a concrete question to a student's attempt.  Keep that absence
        # explicit; the attempt-start adapter will persist the chosen question.
        return ""
    return description or "Условие не опубликовано в Moodle."


def _dynamic_statement_policy(activity: dict[str, Any]) -> dict[str, Any]:
    deferred = moodle_statement_is_deferred(activity)
    random_count = activity.get("random_question_count")
    return {
        "statement_deferred": deferred,
        "random_question_count": (
            random_count
            if isinstance(random_count, int)
            and not isinstance(random_count, bool)
            and 0 <= random_count <= 10_000
            else 0
        ),
        "random_essay_confirmed": activity.get("random_essay_confirmed") is True,
        "quiz_questions_confirmed": activity.get("quiz_questions_confirmed") is True,
    }


def _moodle_attempt_limit(activity: dict[str, Any]) -> int | None:
    """Project only an explicitly parsed finite/unlimited attempt policy."""

    if activity.get("attempt_limit_unlimited") is True:
        return None
    return _positive_int(activity.get("attempt_limit"), maximum=100)


def _positive_int(value: object, *, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= maximum else None


async def _refresh_managed_task(
    db: AsyncSession,
    *,
    assessment: Assessment,
    mapping: ExternalMapping,
    title: str,
    statement: str,
    score: Decimal,
    source_confirmation: dict[str, bool],
    activity: dict[str, Any],
) -> None:
    """Project Moodle-owned content while preserving immutable attempt snapshots.

    A published task version may already be referenced by attempts.  When
    Moodle changes title/condition/grade, create a successor and move only the
    assessment item to it; old attempts keep their assigned version.
    """

    attached = await db.scalar(
        select(AssessmentItem)
        .where(AssessmentItem.assessment_id == assessment.id)
        .order_by(AssessmentItem.position, AssessmentItem.created_at)
        .limit(1)
    )
    if attached is None:
        return
    version = await db.get(TaskVersion, attached.task_version_id)
    if version is None:
        return
    starter_files = version.starter_files if isinstance(version.starter_files, list) else []
    if not starter_files:
        starter_files = [{"path": "main.cpp", "content": ""}]
    ai_policy = dict(version.ai_policy or {})
    ai_policy.pop("lms_import_requires_configuration", None)
    ai_policy.update(
        {
            "source": "MOODLE_ACTIVITY",
            "moodle_metadata_read_only": True,
            "moodle_source_confirmation": source_confirmation,
            **_dynamic_statement_policy(activity),
        }
    )
    changed = (
        version.title != title
        or version.statement != statement
        or version.max_score != score
        or version.starter_files != starter_files
        or version.ai_policy != ai_policy
    )
    if changed and version.status != TaskVersionStatus.DRAFT.value:
        latest = await db.scalar(
            select(TaskVersion)
            .where(TaskVersion.item_id == version.item_id)
            .order_by(TaskVersion.number.desc())
            .limit(1)
        )
        successor = TaskVersion(
            item_id=version.item_id,
            number=(latest.number if latest is not None else version.number) + 1,
            title=title,
            statement=statement,
            language=version.language,
            language_standard=version.language_standard,
            multi_file=version.multi_file,
            starter_files=starter_files,
            build_profile=version.build_profile,
            public_examples=version.public_examples,
            hidden_test_manifest=version.hidden_test_manifest,
            max_score=score,
            difficulty=version.difficulty,
            ai_policy=ai_policy,
            content_hash="",
            status=(
                TaskVersionStatus.PUBLISHED.value
                if assessment.status == AssessmentStatus.PUBLISHED.value
                else TaskVersionStatus.DRAFT.value
            ),
            authored_by_id=assessment.created_by_id,
            published_at=(
                utcnow() if assessment.status == AssessmentStatus.PUBLISHED.value else None
            ),
        )
        successor.content_hash = canonical_hash(_task_content(successor))
        db.add(successor)
        await db.flush()
        attached.task_version_id = successor.id
        version = successor
    elif changed:
        version.title = title
        version.statement = statement
        version.starter_files = starter_files
        version.max_score = score
        version.ai_policy = ai_policy
        version.content_hash = canonical_hash(_task_content(version))
    attached.points = score
    metadata = dict(mapping.metadata_json or {})
    metadata["task_version_id"] = str(version.id)
    mapping.metadata_json = metadata


async def materialize_moodle_activity_drafts(
    db: AsyncSession,
    *,
    course: Course,
    activities: list[dict[str, Any]],
    created_by_id: uuid.UUID,
) -> int:
    """Project Moodle activities into locally enableable programming works.

    Moodle remains authoritative for title, condition, schedule, grade range
    and attempt policy.  Local state owns only IDE/review policy and the group
    visibility switch used by explicit publication.
    """

    sections = {
        row.external_id: row
        for row in (
            await db.scalars(select(CourseSection).where(CourseSection.course_id == course.id))
        ).all()
    }
    created = 0
    for activity in activities:
        module = str(activity.get("module", "")).removeprefix("mod_")
        cmid = activity.get("cmid")
        if (
            module not in _SUPPORTED_MODULES
            or isinstance(cmid, bool)
            or not str(cmid).isdigit()
            or int(cmid) <= 0
        ):
            continue
        cmid = int(cmid)
        external_types = (
            {"mod_assign", "assign", "moodle_assignment", "moodle_mod_assign"}
            if module == "assign"
            else {"mod_quiz", "quiz", "moodle_quiz", "moodle_mod_quiz"}
        )
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.connection_id == course.connection_id,
                ExternalMapping.external_type.in_(external_types),
                ExternalMapping.external_id == str(cmid),
            )
        )
        if mapping is not None:
            metadata = dict(mapping.metadata_json or {})
            if metadata.get("managed_by") == "MOODLE_ACTIVITY_IMPORT":
                source_confirmation = moodle_source_confirmation_from_activity(activity)
                source_confirmed = moodle_source_is_confirmed(source_confirmation)
                answer_transport = confirmed_moodle_activity_answer_transport(
                    activity,
                    module=module,
                )
                transport_confirmed = answer_transport is not None or (
                    module == "quiz"
                    and activity.get("quiz_questions_confirmed") is True
                    and moodle_statement_is_deferred(activity)
                )
                metadata.update(
                    {
                        "activity": _mapping_activity(activity),
                        "last_seen_revision": course.external_revision,
                        "submission_mode": answer_transport or "REQUIRES_CONFIGURATION",
                        "moodle_source_confirmation": source_confirmation,
                        "sync_state": (
                            "CURRENT"
                            if transport_confirmed and source_confirmed
                            else (
                                "MOODLE_SOURCE_UNCONFIRMED"
                                if not source_confirmed
                                else "ANSWER_TRANSPORT_UNSUPPORTED"
                            )
                        ),
                    }
                )
                mapping.metadata_json = metadata
                mapping.external_revision = course.external_revision
                assessment = await db.get(Assessment, mapping.local_id)
                if assessment is not None and assessment.course_id == course.id:
                    parsed_title = _bounded(activity.get("name"), 255)
                    title = (
                        parsed_title
                        if source_confirmation["title"] and parsed_title
                        else assessment.title
                    )
                    parsed_statement = _bounded(activity.get("description"), 50_000)
                    statement = (
                        parsed_statement
                        if source_confirmation["statement"] and parsed_statement
                        else (
                            ""
                            if source_confirmation["statement_deferred"]
                            else assessment.instructions
                        )
                    )
                    section = sections.get(str(activity.get("section_external_id", "")))
                    parsed_score = positive_decimal(activity.get("grade_max"))
                    score = (
                        parsed_score
                        if source_confirmation["grade"] and parsed_score is not None
                        else assessment.max_score
                    )
                    assessment.title = title
                    assessment.instructions = statement
                    if source_confirmation["schedule"]:
                        assessment.opens_at = _epoch(activity.get("opens_at_epoch"))
                        assessment.closes_at = _epoch(activity.get("cutoff_at_epoch")) or _epoch(
                            activity.get("due_at_epoch")
                        )
                    if source_confirmation["duration"]:
                        assessment.duration_seconds = _positive_int(
                            activity.get("duration_seconds"), maximum=31_536_000
                        )
                    if source_confirmation["attempt_policy"]:
                        assessment.attempt_limit = _moodle_attempt_limit(activity)
                    assessment.max_score = score
                    if section is not None:
                        assessment.section_id = section.id
                        assessment_type = classify_moodle_assessment(title, section.title)
                        if assessment_type is not None:
                            assessment.type = assessment_type
                    policy = dict(assessment.policy or {})
                    activity_mapping = dict(policy.get("lms_activity_mapping") or {})
                    activity_mapping["submission_mode"] = metadata["submission_mode"]
                    activity_mapping["sync_deadlines"] = True
                    policy["lms_activity_mapping"] = activity_mapping
                    policy.pop("lms_import_requires_configuration", None)
                    policy["moodle_metadata_read_only"] = True
                    policy["moodle_source_confirmation"] = source_confirmation
                    assessment.policy = policy
                    await _refresh_managed_task(
                        db,
                        assessment=assessment,
                        mapping=mapping,
                        title=title,
                        statement=statement,
                        score=score,
                        source_confirmation=source_confirmation,
                        activity=activity,
                    )
                    item_id = metadata.get("task_item_id")
                    if item_id:
                        try:
                            item = await db.get(TaskBankItem, uuid.UUID(str(item_id)))
                        except (TypeError, ValueError):
                            item = None
                        if item is not None:
                            item.tags = ["moodle-import", "moodle-managed", f"moodle-{module}"]
            # Explicit/manual mappings remain local and are not rewritten.
            continue

        source_confirmation = moodle_source_confirmation_from_activity(activity)
        source_confirmed = moodle_source_is_confirmed(source_confirmation)
        parsed_title = _bounded(activity.get("name"), 255)
        title = parsed_title or f"Moodle {module} {cmid}"
        section = sections.get(str(activity.get("section_external_id", "")))
        section_title = section.title if section is not None else ""
        assessment_type = classify_moodle_assessment(title, section_title)
        if assessment_type is None:
            continue
        parsed_score = positive_decimal(activity.get("grade_max"))
        score = (
            parsed_score
            if source_confirmation["grade"] and parsed_score is not None
            else Decimal("0.00")
        )
        description = _bounded(activity.get("description"), 50_000)
        statement = _statement_from_moodle(activity)
        source_description_confirmed = source_confirmation["statement"] and bool(description)
        quiz_single_essay = (
            module == "quiz"
            and activity.get("question_count") == 1
            and activity.get("import_supported") is True
        )
        answer_transport = confirmed_moodle_activity_answer_transport(
            activity,
            module=module,
        )
        transport_confirmed = answer_transport is not None or (
            module == "quiz"
            and activity.get("quiz_questions_confirmed") is True
            and moodle_statement_is_deferred(activity)
        )
        slug = await _available_slug(db, course_id=course.id, module=module, cmid=cmid)
        item = TaskBankItem(
            scope=TaskScope.COURSE.value,
            course_id=course.id,
            slug=slug,
            category=section_title[:255],
            tags=["moodle-import", "moodle-managed", f"moodle-{module}"],
            created_by_id=created_by_id,
        )
        db.add(item)
        await db.flush()
        ai_policy = {
            "source": "MOODLE_ACTIVITY",
            "moodle_metadata_read_only": True,
            "source_description_confirmed": source_description_confirmed,
            "quiz_single_essay_confirmed": quiz_single_essay,
            "answer_transport_confirmed": answer_transport,
            "moodle_source_confirmation": source_confirmation,
            **_dynamic_statement_policy(activity),
        }
        version = TaskVersion(
            item_id=item.id,
            number=1,
            title=title,
            statement=statement,
            language="CPP",
            language_standard="C++17",
            multi_file=False,
            starter_files=[{"path": "main.cpp", "content": ""}],
            build_profile="cpp-gcc-c++20-single",
            public_examples=[],
            hidden_test_manifest={},
            max_score=score,
            difficulty="",
            ai_policy=ai_policy,
            content_hash="",
            status=TaskVersionStatus.DRAFT.value,
            authored_by_id=created_by_id,
        )
        version.content_hash = canonical_hash(_task_content(version))
        db.add(version)
        await db.flush()
        closes_at = (
            _epoch(activity.get("cutoff_at_epoch")) or _epoch(activity.get("due_at_epoch"))
            if source_confirmation["schedule"]
            else None
        )
        assessment = Assessment(
            course_id=course.id,
            section_id=section.id if section is not None else None,
            type=assessment_type,
            title=title,
            instructions=statement,
            opens_at=(
                _epoch(activity.get("opens_at_epoch")) if source_confirmation["schedule"] else None
            ),
            closes_at=closes_at,
            duration_seconds=(
                int(activity["duration_seconds"])
                if source_confirmation["duration"]
                and isinstance(activity.get("duration_seconds"), int)
                and not isinstance(activity.get("duration_seconds"), bool)
                and int(activity["duration_seconds"]) > 0
                else None
            ),
            attempt_limit=(
                _moodle_attempt_limit(activity) if source_confirmation["attempt_policy"] else None
            ),
            max_score=score,
            paste_policy="INTERNAL_ONLY",
            student_ai_enabled=assessment_type == AssessmentType.LAB.value,
            teacher_ai_enabled=True,
            review_required=assessment_type != AssessmentType.LAB.value,
            decision_support_enabled=assessment_type != AssessmentType.LAB.value,
            autosubmit=True,
            multi_file=False,
            status=AssessmentStatus.DRAFT.value,
            policy={
                "moodle_metadata_read_only": True,
                "moodle_source_confirmation": source_confirmation,
                "lms_activity_mapping": {
                    "provider": "MOODLE",
                    "module": module,
                    "cmid": cmid,
                    "sync_deadlines": True,
                    "submission_mode": answer_transport or "REQUIRES_CONFIGURATION",
                },
            },
            created_by_id=created_by_id,
        )
        db.add(assessment)
        await db.flush()
        # SQLAlchemy's local column default is intentionally one attempt for
        # locally authored work.  Re-assert the Moodle value after INSERT so
        # an explicitly unlimited or still-unconfirmed imported policy remains
        # NULL instead of inheriting that local default.
        assessment.attempt_limit = (
            _moodle_attempt_limit(activity) if source_confirmation["attempt_policy"] else None
        )
        db.add(
            AssessmentItem(
                assessment_id=assessment.id,
                task_version_id=version.id,
                position=0,
                points=score,
                assignment_rule={},
            )
        )
        db.add(
            ExternalMapping(
                connection_id=course.connection_id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type=f"mod_{module}",
                external_id=str(cmid),
                external_revision=course.external_revision,
                metadata_json={
                    "managed_by": "MOODLE_ACTIVITY_IMPORT",
                    "module": module,
                    "cmid": cmid,
                    "submission_mode": answer_transport or "REQUIRES_CONFIGURATION",
                    "sync_deadlines": True,
                    "sync_state": (
                        "CURRENT"
                        if transport_confirmed and source_confirmed
                        else (
                            "MOODLE_SOURCE_UNCONFIRMED"
                            if not source_confirmed
                            else "ANSWER_TRANSPORT_UNSUPPORTED"
                        )
                    ),
                    "moodle_source_confirmation": source_confirmation,
                    "activity": _mapping_activity(activity),
                    "task_item_id": str(item.id),
                    "task_version_id": str(version.id),
                },
            )
        )
        created += 1
    await db.flush()
    return created
