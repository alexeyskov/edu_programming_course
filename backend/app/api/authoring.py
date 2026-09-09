from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import CurrentAuth
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.delivery_profile import project_activity_delivery_transport
from app.integrations.moodle_modes import moodle_auth_mode
from app.models.attempts import Attempt
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseSection,
)
from app.models.enums import (
    AssessmentStatus,
    AttemptState,
    AvailabilityTarget,
    CourseRole,
    LMSProvider,
    TaskScope,
    TaskVersionStatus,
)
from app.models.identity import LMSConnection
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.tasks import (
    Assessment,
    AssessmentItem,
    AvailabilityRule,
    TaskBankItem,
    TaskVersion,
)
from app.schemas.assessments import (
    AssessmentCreateRequest,
    AssessmentItemCreateRequest,
    AssessmentItemTeacherRead,
    AssessmentPublicationGroupRead,
    AssessmentPublicationTargetsRead,
    AssessmentPublishRequest,
    AssessmentStudentRead,
    AssessmentTeacherRead,
    AssessmentUpdateRequest,
    AssessmentValidateRequest,
    AssessmentValidationRead,
    AvailabilityRuleCreateRequest,
    AvailabilityRuleRead,
)
from app.schemas.common import ValidationIssue
from app.schemas.tasks import (
    TaskBankItemCreateRequest,
    TaskBankItemRead,
    TaskBankItemUpdateRequest,
    TaskVersionCreateRequest,
    TaskVersionPublishRequest,
    TaskVersionTeacherRead,
    TaskVersionValidateRequest,
    TaskVersionValidationRead,
    parse_hidden_test_manifest,
    valid_single_file_starter_paths,
)
from app.services.common import (
    DomainError,
    canonical_hash,
    canonical_json,
    positive_decimal,
    sha256_text,
)
from app.services.moodle_source import (
    missing_moodle_source_confirmations,
    moodle_quiz_uses_latest_attempt_grade,
)
from app.services.policy import (
    MembershipContext,
    effective_assessment_policy,
    ensure_assessment_available,
    publication_group_ids_for_teacher,
    require_membership,
)
from app.services.teacher_tokens import teacher_membership_is_authorized

router = APIRouter(tags=["authoring"])
DBSession = Annotated[AsyncSession, Depends(get_db)]


def _error(status_code: int, code: str, message: str, **details: Any) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, **details},
    )


def _domain_error(exc: DomainError) -> HTTPException:
    return _error(exc.status_code, exc.code, exc.message, **exc.details)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _json_size_guard(value: Any, *, field: str, maximum: int) -> None:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise _error(422, "INVALID_JSON_VALUE", f"{field} contains an invalid JSON value") from exc
    if len(encoded) > maximum:
        raise _error(413, "JSON_VALUE_TOO_LARGE", f"{field} is too large")


def _score_text(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _moodle_activity_spec(policy: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(policy, dict):
        return None
    raw = policy.get("lms_activity_mapping")
    if raw is None:
        # Backward compatibility for assessments created before Quiz Essay support.
        raw = policy.get("lms_grade_mapping")
    if not isinstance(raw, dict) or not raw:
        return None
    provider = str(raw.get("provider", "MOODLE")).upper()
    module = str(raw.get("module", "assign")).lower().replace("-", "_")
    cmid = raw.get("cmid")
    module_aliases = {
        "assign": "assign",
        "mod_assign": "assign",
        "quiz": "quiz",
        "mod_quiz": "quiz",
    }
    if provider != "MOODLE" or module not in module_aliases:
        raise _error(
            422,
            "UNSUPPORTED_LMS_MAPPING",
            "Only Moodle Assignment and Quiz Essay activities are supported",
        )
    if isinstance(cmid, bool) or not str(cmid).isdigit() or int(cmid) <= 0:
        raise _error(422, "INVALID_LMS_CMID", "Moodle activity cmid must be positive")
    normalized_module = module_aliases[module]
    spec: dict[str, Any] = {
        "provider": "MOODLE",
        "module": normalized_module,
        "cmid": int(cmid),
        "sync_deadlines": bool(raw.get("sync_deadlines", True)),
    }
    if normalized_module == "quiz" and raw.get("question_slot") is not None:
        raise _error(
            422,
            "MOODLE_QUESTION_SLOT_UNSUPPORTED",
            "Quiz Essay mapping must contain exactly one Essay question",
        )
    return spec


def _epoch_seconds(value: object) -> datetime | None:
    if isinstance(value, bool):
        return None
    try:
        epoch = int(value or 0)
    except (TypeError, ValueError, OverflowError):
        return None
    if epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=UTC)
    except (OSError, OverflowError, ValueError):
        return None


async def _sync_assessment_mapping(db: AsyncSession, assessment: Assessment) -> None:
    """Keep the explicit local-assessment -> Moodle activity mapping authoritative."""

    course = await db.get(Course, assessment.course_id)
    if course is None:
        raise _error(422, "COURSE_NOT_FOUND", "Assessment course was not found")
    rows = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.local_id == assessment.id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                )
            )
        ).all()
    )
    spec = _moodle_activity_spec(assessment.policy)
    if spec is None:
        for row in rows:
            await db.delete(row)
        return
    cmid = str(spec["cmid"])
    external_type = f"mod_{spec['module']}"
    conflict = await db.scalar(
        select(ExternalMapping).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.external_type == external_type,
            ExternalMapping.external_id == cmid,
            ExternalMapping.local_id != assessment.id,
        )
    )
    if conflict is not None:
        raise _error(
            409,
            "LMS_ACTIVITY_ALREADY_MAPPED",
            "This Moodle activity is already mapped to another assessment",
        )
    mapping = rows[0] if rows else None
    for stale in rows[1:]:
        await db.delete(stale)
    if mapping is None:
        mapping = ExternalMapping(
            connection_id=course.connection_id,
            local_type="Assessment",
            local_id=assessment.id,
            external_type=external_type,
            external_id=cmid,
        )
        db.add(mapping)
    mapping.external_type = external_type
    mapping.external_id = cmid
    activity_rows = course.policies.get("lms_activities", []) if course.policies else []
    if not isinstance(activity_rows, list):
        activity_rows = []
    activity = next(
        (
            row
            for row in activity_rows
            if isinstance(row, dict)
            and str(row.get("module", "")).lower().removeprefix("mod_") == spec["module"]
            and str(row.get("cmid")) == cmid
        ),
        None,
    )
    answer_transport = project_activity_delivery_transport(
        provider=str(spec["provider"]),
        module=str(spec["module"]),
        activity=activity,
    )
    spec["submission_mode"] = answer_transport or "REQUIRES_CONFIGURATION"
    if spec["module"] == "assign" and activity is not None and "grade_max" in activity:
        grade_max = positive_decimal(activity.get("grade_max"))
        if grade_max is None:
            raise _error(
                422,
                "UNSUPPORTED_MOODLE_GRADING",
                "Mapped Moodle Assignment must use a positive numeric grade",
            )
        if grade_max != assessment.max_score:
            raise _error(
                422,
                "LMS_GRADE_RANGE_MISMATCH",
                "Assessment maximum score must match the Moodle Assignment grade",
                assessment_max_score=_score_text(assessment.max_score),
                moodle_grade_max=str(grade_max),
            )
    mapping.metadata_json = {
        **dict(mapping.metadata_json or {}),
        **spec,
        "activity": activity,
        "sync_state": (
            "MISSING_IN_MOODLE"
            if activity is None
            else "CURRENT"
            if answer_transport is not None
            else "ANSWER_TRANSPORT_UNSUPPORTED"
        ),
    }
    if activity is not None and spec["sync_deadlines"]:
        opens_at = _epoch_seconds(activity.get("opens_at_epoch"))
        closes_at = _epoch_seconds(activity.get("cutoff_at_epoch")) or _epoch_seconds(
            activity.get("due_at_epoch")
        )
        if opens_at is not None and closes_at is not None and closes_at <= opens_at:
            mapping.metadata_json = {
                **mapping.metadata_json,
                "sync_state": "INVALID_MOODLE_WINDOW",
            }
        else:
            if opens_at is not None:
                assessment.opens_at = opens_at
            if closes_at is not None:
                assessment.closes_at = closes_at


def _task_mirror_definition(item: TaskBankItem, version: TaskVersion) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "task_ref": str(item.id),
        "slug": item.slug,
        "category": item.category,
        "tags": item.tags,
        "version": version.number,
        "content_hash": version.content_hash,
        "title": version.title,
        "statement": version.statement,
        "language": version.language,
        "language_standard": version.language_standard,
        "multi_file": version.multi_file,
        "starter_files": version.starter_files,
        "build_profile": version.build_profile,
        "public_examples": version.public_examples,
        "hidden_test_manifest": version.hidden_test_manifest,
        "max_score": _score_text(version.max_score),
        "difficulty": version.difficulty,
        "ai_policy": version.ai_policy,
    }


async def _enqueue_task_mirror(
    db: AsyncSession,
    *,
    item: TaskBankItem,
    version: TaskVersion,
    course_id: uuid.UUID,
) -> None:
    course = await db.get(Course, course_id)
    if course is None:
        raise _error(422, "COURSE_NOT_FOUND", "Task mirror course was not found")
    connection = await db.get(LMSConnection, course.connection_id)
    if (
        connection is None
        or connection.provider != LMSProvider.MOODLE.value
        or moodle_auth_mode(connection) != "BRIDGE"
    ):
        # Pluginless Moodle has no connector-owned immutable mirror table.
        # The authoritative task version remains local and publishes normally.
        return
    definition_json = canonical_json(_task_mirror_definition(item, version))
    if len(definition_json.encode("utf-8")) > 4 * 1024 * 1024:
        raise _error(
            413,
            "MOODLE_TASK_MIRROR_TOO_LARGE",
            "Published task exceeds the Moodle mirror limit",
        )
    key = f"task:{course.id}:{version.id}:{version.status}"[:100]
    if await db.scalar(select(SyncOutbox.id).where(SyncOutbox.idempotency_key == key)):
        return
    db.add(
        SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            event_type="task.version",
            aggregate_type="TaskVersion",
            aggregate_id=version.id,
            idempotency_key=key,
            payload={
                "course_id": course.external_id,
                "task_ref": str(item.id),
                "version": version.number,
                "content_hash": version.content_hash,
                "definition_sha256": sha256_text(definition_json),
                "definition_json": definition_json,
                "status": version.status,
            },
        )
    )


async def _course_membership(
    db: AsyncSession,
    context: CurrentAuth,
    course_id: uuid.UUID,
    role: CourseRole | None = None,
) -> MembershipContext:
    try:
        return await require_membership(
            db,
            principal_id=context.principal_id,
            course_id=course_id,
            role=role,
        )
    except DomainError as exc:
        raise _domain_error(exc) from exc


async def _teacher_course_ids(db: AsyncSession, principal_id: uuid.UUID) -> set[uuid.UUID]:
    if not await teacher_membership_is_authorized(db, principal_id):
        return set()
    now = utcnow()
    return set(
        (
            await db.scalars(
                select(CourseMembership.course_id)
                .join(Course, Course.id == CourseMembership.course_id)
                .where(
                    CourseMembership.principal_id == principal_id,
                    CourseMembership.role == CourseRole.TEACHER.value,
                    CourseMembership.active.is_(True),
                    Course.catalog_enabled.is_(True),
                    Course.archived_at.is_(None),
                    or_(
                        CourseMembership.valid_until.is_(None),
                        CourseMembership.valid_until > now,
                    ),
                )
            )
        ).all()
    )


async def _require_item_access(
    db: AsyncSession,
    context: CurrentAuth,
    item_id: uuid.UUID,
    *,
    lock: bool = False,
) -> TaskBankItem:
    query = select(TaskBankItem).where(TaskBankItem.id == item_id)
    if lock:
        query = query.with_for_update()
    item = await db.scalar(query)
    if item is None:
        raise _error(404, "TASK_ITEM_NOT_FOUND", "Task-bank item was not found")
    if item.scope == TaskScope.SYSTEM.value:
        if not context.has_capability("SYSTEM_SETTINGS"):
            raise _error(403, "SYSTEM_SCOPE_REQUIRED", "System administrator elevation is required")
    elif item.course_id is None:
        raise _error(409, "INVALID_TASK_SCOPE", "Course-scoped task has no course")
    else:
        await _course_membership(db, context, item.course_id, CourseRole.TEACHER)
    return item


async def _version_and_item(
    db: AsyncSession,
    context: CurrentAuth,
    version_id: uuid.UUID,
    *,
    lock: bool = False,
) -> tuple[TaskVersion, TaskBankItem]:
    query = select(TaskVersion).where(TaskVersion.id == version_id)
    if lock:
        query = query.with_for_update()
    version = await db.scalar(query)
    if version is None:
        raise _error(404, "TASK_VERSION_NOT_FOUND", "Task version was not found")
    item = await _require_item_access(db, context, version.item_id)
    return version, item


async def _managed_moodle_mapping(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> ExternalMapping | None:
    rows = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.local_id == assessment_id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                )
            )
        ).all()
    )
    managed = [
        row
        for row in rows
        if isinstance(row.metadata_json, dict)
        and row.metadata_json.get("managed_by") == "MOODLE_ACTIVITY_IMPORT"
    ]
    # An imported assessment must map to exactly one Moodle activity. Treat an
    # ambiguous mapping as unavailable instead of silently choosing whichever
    # row the database happened to return first.
    return managed[0] if len(managed) == 1 else None


def _managed_moodle_mapping_issue(
    assessment: Assessment,
    course: Course | None,
    mapping: ExternalMapping | None,
) -> ValidationIssue | None:
    """Validate stable identities without depending on discovery freshness."""

    invalid = ValidationIssue(
        field="policy.lms_activity_mapping",
        code="MOODLE_RUNTIME_MAPPING_UNAVAILABLE",
        message=(
            "The imported assessment has no single valid Moodle activity mapping. "
            "Synchronize the course before publishing it"
        ),
    )
    if course is None or mapping is None:
        return invalid
    if mapping.connection_id != course.connection_id:
        return invalid
    course_external_id = str(course.external_id).strip()
    external_cmid = str(mapping.external_id).strip()
    if (
        not course_external_id.isdigit()
        or int(course_external_id) <= 0
        or not external_cmid.isdigit()
        or int(external_cmid) <= 0
    ):
        return invalid
    external_module = mapping.external_type.lower().replace("-", "_").removeprefix("mod_")
    if external_module not in {"assign", "quiz"}:
        return invalid
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    metadata_module = str(metadata.get("module", external_module)).lower().replace("-", "_")
    metadata_module = metadata_module.removeprefix("mod_")
    if metadata_module != external_module:
        return invalid
    metadata_cmid = str(metadata.get("cmid", external_cmid)).strip()
    if (
        not metadata_cmid.isdigit()
        or int(metadata_cmid) <= 0
        or int(metadata_cmid) != int(external_cmid)
    ):
        return invalid
    if mapping.local_id != assessment.id:
        return invalid
    return None


async def _managed_moodle_mapping_for_version(
    db: AsyncSession,
    version_id: uuid.UUID,
) -> ExternalMapping | None:
    assessment_ids = list(
        (
            await db.scalars(
                select(AssessmentItem.assessment_id).where(
                    AssessmentItem.task_version_id == version_id
                )
            )
        ).all()
    )
    for assessment_id in assessment_ids:
        mapping = await _managed_moodle_mapping(db, assessment_id)
        if mapping is not None:
            return mapping
    return None


async def _managed_moodle_mapping_for_item(
    db: AsyncSession,
    item_id: uuid.UUID,
) -> ExternalMapping | None:
    assessment_ids = list(
        (
            await db.scalars(
                select(AssessmentItem.assessment_id)
                .join(TaskVersion, TaskVersion.id == AssessmentItem.task_version_id)
                .where(TaskVersion.item_id == item_id)
                .distinct()
            )
        ).all()
    )
    for assessment_id in assessment_ids:
        mapping = await _managed_moodle_mapping(db, assessment_id)
        if mapping is not None:
            return mapping
    return None


def _task_content(value: TaskVersion | TaskVersionCreateRequest) -> dict[str, Any]:
    if isinstance(value, TaskVersionCreateRequest):
        starter_files = [row.model_dump(mode="json") for row in value.starter_files]
        public_examples = [row.model_dump(mode="json") for row in value.public_examples]
        return {
            "title": value.title,
            "statement": value.statement,
            "language": value.language,
            "language_standard": value.language_standard,
            "multi_file": value.multi_file,
            "starter_files": starter_files,
            "build_profile": value.build_profile,
            "public_examples": public_examples,
            "hidden_test_manifest": value.hidden_test_manifest,
            "max_score": _score_text(value.max_score),
            "difficulty": value.difficulty,
            "ai_policy": value.ai_policy,
        }
    return {
        "title": value.title,
        "statement": value.statement,
        "language": value.language,
        "language_standard": value.language_standard,
        "multi_file": value.multi_file,
        "starter_files": value.starter_files,
        "build_profile": value.build_profile,
        "public_examples": value.public_examples,
        "hidden_test_manifest": value.hidden_test_manifest,
        "max_score": _score_text(value.max_score),
        "difficulty": value.difficulty,
        "ai_policy": value.ai_policy,
    }


def _task_version_read(row: TaskVersion) -> TaskVersionTeacherRead:
    return TaskVersionTeacherRead.model_validate(row)


async def _task_item_read(db: AsyncSession, item: TaskBankItem) -> TaskBankItemRead:
    latest = await db.scalar(
        select(TaskVersion)
        .where(TaskVersion.item_id == item.id)
        .order_by(TaskVersion.number.desc())
        .limit(1)
    )
    return TaskBankItemRead(
        id=item.id,
        course=item.course_id,
        scope=item.scope,
        slug=item.slug,
        category=item.category,
        tags=item.tags,
        created_at=item.created_at,
        archived_at=item.archived_at,
        latest_version=_task_version_read(latest) if latest else None,
    )


@router.get("/task-bank/items", response_model=list[TaskBankItemRead])
async def list_task_items(
    context: CurrentAuth,
    db: DBSession,
    course_id: uuid.UUID | None = None,
    include_archived: bool = False,
) -> list[TaskBankItemRead]:
    course_ids = await _teacher_course_ids(db, context.principal_id)
    conditions = []
    if course_ids:
        conditions.append(TaskBankItem.course_id.in_(course_ids))
    if context.has_capability("SYSTEM_SETTINGS"):
        conditions.append(TaskBankItem.scope == TaskScope.SYSTEM.value)
    if not conditions:
        raise _error(403, "TEACHER_ROLE_REQUIRED", "A teacher role is required")
    query = select(TaskBankItem).where(or_(*conditions))
    if course_id is not None:
        if course_id not in course_ids:
            raise _error(403, "COURSE_MEMBERSHIP_REQUIRED", "Teacher membership is required")
        query = query.where(TaskBankItem.course_id == course_id)
    if not include_archived:
        query = query.where(TaskBankItem.archived_at.is_(None))
    rows = list((await db.scalars(query.order_by(TaskBankItem.slug))).all())
    return [await _task_item_read(db, row) for row in rows]


@router.post(
    "/task-bank/items",
    response_model=TaskBankItemRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_item(
    payload: TaskBankItemCreateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> TaskBankItemRead:
    if payload.scope == TaskScope.SYSTEM:
        if not context.has_capability("SYSTEM_SETTINGS"):
            raise _error(403, "SYSTEM_SCOPE_REQUIRED", "System administrator elevation is required")
    else:
        assert payload.course is not None
        await _course_membership(db, context, payload.course, CourseRole.TEACHER)
    duplicate = await db.scalar(
        select(TaskBankItem.id).where(
            TaskBankItem.scope == payload.scope.value,
            TaskBankItem.course_id == payload.course,
            TaskBankItem.slug == payload.slug,
        )
    )
    if duplicate is not None:
        raise _error(409, "TASK_SLUG_EXISTS", "Task slug already exists in this scope")
    item = TaskBankItem(
        scope=payload.scope.value,
        course_id=payload.course,
        slug=payload.slug,
        category=payload.category,
        tags=payload.tags,
        created_by_id=context.principal_id,
    )
    db.add(item)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _error(409, "TASK_SLUG_EXISTS", "Task slug already exists in this scope") from exc
    result = await _task_item_read(db, item)
    await db.commit()
    return result


@router.get("/task-bank/items/{item_id}", response_model=TaskBankItemRead)
async def get_task_item(
    item_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> TaskBankItemRead:
    return await _task_item_read(db, await _require_item_access(db, context, item_id))


@router.patch("/task-bank/items/{item_id}", response_model=TaskBankItemRead)
async def update_task_item(
    item_id: uuid.UUID,
    payload: TaskBankItemUpdateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> TaskBankItemRead:
    item = await _require_item_access(db, context, item_id, lock=True)
    if payload.category is not None:
        item.category = payload.category
    if payload.tags is not None:
        item.tags = payload.tags
    if payload.archived is not None:
        item.archived_at = utcnow() if payload.archived else None
    await db.commit()
    return await _task_item_read(db, item)


@router.post(
    "/task-bank/items/{item_id}/versions",
    response_model=TaskVersionTeacherRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_task_version(
    item_id: uuid.UUID,
    payload: TaskVersionCreateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> TaskVersionTeacherRead:
    item = await _require_item_access(db, context, item_id, lock=True)
    if item.archived_at is not None:
        raise _error(409, "TASK_ITEM_ARCHIVED", "Cannot add a version to an archived item")
    if await _managed_moodle_mapping_for_item(db, item.id) is not None:
        raise _error(
            409,
            "MOODLE_METADATA_READ_ONLY",
            "Moodle-managed work is versioned by synchronization, not the local task bank",
        )
    latest_number = await db.scalar(
        select(func.max(TaskVersion.number)).where(TaskVersion.item_id == item.id)
    )
    content = _task_content(payload)
    _json_size_guard(content, field="task version content", maximum=16 * 1024 * 1024)
    row = TaskVersion(
        item_id=item.id,
        number=int(latest_number or 0) + 1,
        title=payload.title,
        statement=payload.statement,
        language=payload.language,
        language_standard=payload.language_standard,
        multi_file=payload.multi_file,
        starter_files=[item.model_dump(mode="json") for item in payload.starter_files],
        build_profile=payload.build_profile,
        public_examples=[item.model_dump(mode="json") for item in payload.public_examples],
        hidden_test_manifest=payload.hidden_test_manifest,
        max_score=payload.max_score,
        difficulty=payload.difficulty,
        ai_policy=payload.ai_policy,
        content_hash=canonical_hash(content),
        status=TaskVersionStatus.DRAFT.value,
        authored_by_id=context.principal_id,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise _error(
            409, "TASK_VERSION_CONFLICT", "Concurrent version creation conflicted"
        ) from exc
    result = _task_version_read(row)
    await db.commit()
    return result


@router.get("/task-versions", response_model=list[TaskVersionTeacherRead])
async def list_task_versions(
    context: CurrentAuth,
    db: DBSession,
    item_id: uuid.UUID | None = None,
) -> list[TaskVersionTeacherRead]:
    course_ids = await _teacher_course_ids(db, context.principal_id)
    conditions = []
    if course_ids:
        conditions.append(TaskBankItem.course_id.in_(course_ids))
    if context.has_capability("SYSTEM_SETTINGS"):
        conditions.append(TaskBankItem.scope == TaskScope.SYSTEM.value)
    if not conditions:
        raise _error(403, "TEACHER_ROLE_REQUIRED", "A teacher role is required")
    query = (
        select(TaskVersion)
        .join(TaskBankItem, TaskBankItem.id == TaskVersion.item_id)
        .where(or_(*conditions))
    )
    if item_id is not None:
        await _require_item_access(db, context, item_id)
        query = query.where(TaskVersion.item_id == item_id)
    rows = list(
        (
            await db.scalars(
                query.order_by(TaskVersion.created_at.desc(), TaskVersion.number.desc())
            )
        ).all()
    )
    return [_task_version_read(row) for row in rows]


@router.get("/task-versions/{version_id}", response_model=TaskVersionTeacherRead)
async def get_task_version(
    version_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> TaskVersionTeacherRead:
    version, _ = await _version_and_item(db, context, version_id)
    return _task_version_read(version)


async def _refresh_imported_assessment_after_task_edit(
    db: AsyncSession,
    version: TaskVersion,
) -> None:
    """Keep runtime-only settings of a Moodle-managed draft coherent."""

    rows = list(
        (
            await db.execute(
                select(AssessmentItem, Assessment)
                .join(Assessment, Assessment.id == AssessmentItem.assessment_id)
                .where(AssessmentItem.task_version_id == version.id)
            )
        ).all()
    )
    for attached, assessment in rows:
        if assessment.status != AssessmentStatus.DRAFT.value:
            continue
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.local_id == assessment.id,
                ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
            )
        )
        metadata = dict(mapping.metadata_json or {}) if mapping is not None else {}
        if metadata.get("managed_by") != "MOODLE_ACTIVITY_IMPORT":
            continue
        # Moodle-owned text/score is never rewritten from the local editor.
        # Runtime-only IDE settings may still be adjusted through the legacy
        # API, so keep the assessment file mode coherent without reintroducing
        # the former "configure imported placeholder" gate.
        assessment.multi_file = version.multi_file
        attached.points = assessment.max_score
        policy = dict(assessment.policy or {})
        policy.pop("lms_import_requires_configuration", None)
        policy["moodle_metadata_read_only"] = True
        assessment.policy = policy


@router.put("/task-versions/{version_id}", response_model=TaskVersionTeacherRead)
async def update_task_version(
    version_id: uuid.UUID,
    payload: TaskVersionCreateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> TaskVersionTeacherRead:
    version, _item = await _version_and_item(db, context, version_id, lock=True)
    if version.status != TaskVersionStatus.DRAFT.value:
        raise _error(
            409,
            "TASK_VERSION_IMMUTABLE",
            "Only a draft task version can be edited",
        )
    managed_mapping = await _managed_moodle_mapping_for_version(db, version.id)
    if managed_mapping is not None and (
        payload.title != version.title
        or payload.statement != version.statement
        or payload.max_score != version.max_score
    ):
        raise _error(
            409,
            "MOODLE_METADATA_READ_ONLY",
            "Title, condition and maximum score are synchronized from Moodle",
        )
    content = _task_content(payload)
    _json_size_guard(content, field="task version content", maximum=16 * 1024 * 1024)
    version.title = payload.title
    version.statement = payload.statement
    version.language = payload.language
    version.language_standard = payload.language_standard
    version.multi_file = payload.multi_file
    version.starter_files = [item.model_dump(mode="json") for item in payload.starter_files]
    version.build_profile = payload.build_profile
    version.public_examples = [item.model_dump(mode="json") for item in payload.public_examples]
    version.hidden_test_manifest = payload.hidden_test_manifest
    version.max_score = payload.max_score
    version.difficulty = payload.difficulty
    version.ai_policy = payload.ai_policy
    if managed_mapping is not None:
        version.ai_policy = {
            **version.ai_policy,
            "source": "MOODLE_ACTIVITY",
            "moodle_metadata_read_only": True,
        }
    version.content_hash = canonical_hash(_task_content(version))
    await _refresh_imported_assessment_after_task_edit(db, version)
    await db.commit()
    return _task_version_read(version)


def _validate_task_version(row: TaskVersion) -> tuple[list[ValidationIssue], list[ValidationIssue]]:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    ai_policy = row.ai_policy if isinstance(row.ai_policy, dict) else {}
    if ai_policy.get("lms_import_requires_configuration") is True:
        warnings.append(
            ValidationIssue(
                field="ai_policy",
                code="LMS_IMPORT_REQUIRES_CONFIGURATION",
                message=(
                    "Legacy imported-task configuration marker is ignored for local group "
                    "publication; the LMS adapter resolves delivery when an attempt starts"
                ),
            )
        )
    starter_files = row.starter_files if isinstance(row.starter_files, list) else []
    if not starter_files:
        errors.append(
            ValidationIssue(field="starter_files", code="REQUIRED", message="Add a starter file")
        )
    paths: list[str] = []
    for source in starter_files:
        if not isinstance(source, dict) or not isinstance(source.get("path"), str):
            errors.append(
                ValidationIssue(
                    field="starter_files",
                    code="INVALID_FILE",
                    message="Starter file entry is invalid",
                )
            )
            continue
        paths.append(source["path"])
    if len(paths) != len(set(paths)):
        errors.append(
            ValidationIssue(
                field="starter_files",
                code="DUPLICATE_PATH",
                message="Starter file paths must be unique",
            )
        )
    if not row.multi_file and not valid_single_file_starter_paths(paths):
        errors.append(
            ValidationIssue(
                field="multi_file",
                code="FILE_MODE_MISMATCH",
                message=(
                    "Single-file task must contain exactly one C/C++ translation unit "
                    "and may additionally contain .txt data files"
                ),
            )
        )
    if row.max_score <= 0:
        errors.append(
            ValidationIssue(
                field="max_score",
                code="POSITIVE_SCORE_REQUIRED",
                message="Maximum score must be positive",
            )
        )
    if not row.build_profile:
        errors.append(
            ValidationIssue(
                field="build_profile",
                code="REQUIRED",
                message="Build profile is required",
            )
        )
    try:
        parse_hidden_test_manifest(row.hidden_test_manifest)
    except ValueError as exc:
        errors.append(
            ValidationIssue(
                field="hidden_test_manifest",
                code="INVALID_HIDDEN_TEST_MANIFEST",
                message=str(exc)[:2_000],
            )
        )
    if canonical_hash(_task_content(row)) != row.content_hash:
        errors.append(
            ValidationIssue(
                field="content_hash",
                code="CONTENT_HASH_MISMATCH",
                message="Stored task content hash does not match immutable content",
            )
        )
    if not row.public_examples:
        warnings.append(
            ValidationIssue(
                field="public_examples",
                code="NO_PUBLIC_EXAMPLES",
                message="No public examples are configured",
            )
        )
    return errors, warnings


@router.post("/task-versions/{version_id}/validate", response_model=TaskVersionValidationRead)
async def validate_task_version(
    version_id: uuid.UUID,
    _: TaskVersionValidateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> TaskVersionValidationRead:
    version, _ = await _version_and_item(db, context, version_id)
    errors, warnings = _validate_task_version(version)
    return TaskVersionValidationRead(
        task_version_id=version.id,
        valid=not errors,
        errors=errors,
        warnings=warnings,
    )


@router.post("/task-versions/{version_id}/publish", response_model=TaskVersionTeacherRead)
async def publish_task_version(
    version_id: uuid.UUID,
    _: TaskVersionPublishRequest,
    context: CurrentAuth,
    db: DBSession,
) -> TaskVersionTeacherRead:
    version, item = await _version_and_item(db, context, version_id, lock=True)
    if await _managed_moodle_mapping_for_version(db, version.id) is not None:
        raise _error(
            409,
            "MOODLE_GROUP_PUBLICATION_REQUIRED",
            "Enable the Moodle work for selected groups instead of publishing its task version",
        )
    if item.archived_at is not None:
        raise _error(409, "TASK_ITEM_ARCHIVED", "Archived task item cannot be published")
    if version.status == TaskVersionStatus.ARCHIVED.value:
        raise _error(409, "TASK_VERSION_ARCHIVED", "Archived task version cannot be published")
    if version.status == TaskVersionStatus.PUBLISHED.value:
        return _task_version_read(version)
    errors, _ = _validate_task_version(version)
    if errors:
        raise _error(
            409,
            "TASK_VERSION_INVALID",
            "Task version is not ready for publication",
            errors=[item.model_dump(mode="json") for item in errors],
        )
    version.status = TaskVersionStatus.PUBLISHED.value
    version.published_at = utcnow()
    if item.course_id is not None:
        await _enqueue_task_mirror(db, item=item, version=version, course_id=item.course_id)
    await db.commit()
    return _task_version_read(version)


@router.post("/task-versions/{version_id}/archive", response_model=TaskVersionTeacherRead)
async def archive_task_version(
    version_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> TaskVersionTeacherRead:
    version, item = await _version_and_item(db, context, version_id, lock=True)
    published_use = await db.scalar(
        select(AssessmentItem.id)
        .join(Assessment, Assessment.id == AssessmentItem.assessment_id)
        .where(
            AssessmentItem.task_version_id == version.id,
            Assessment.status == AssessmentStatus.PUBLISHED.value,
        )
        .limit(1)
    )
    if published_use is not None:
        raise _error(
            409,
            "TASK_VERSION_IN_USE",
            "Task version is used by a published assessment and cannot be archived",
        )
    version.status = TaskVersionStatus.ARCHIVED.value
    target_courses = set(
        (
            await db.scalars(
                select(Assessment.course_id)
                .join(AssessmentItem, AssessmentItem.assessment_id == Assessment.id)
                .where(AssessmentItem.task_version_id == version.id)
            )
        ).all()
    )
    if item.course_id is not None:
        target_courses.add(item.course_id)
    for target_course_id in target_courses:
        await _enqueue_task_mirror(
            db,
            item=item,
            version=version,
            course_id=target_course_id,
        )
    await db.commit()
    return _task_version_read(version)


async def _assessment_access(
    db: AsyncSession,
    context: CurrentAuth,
    assessment_id: uuid.UUID,
    *,
    teacher: bool = False,
    lock: bool = False,
) -> tuple[Assessment, MembershipContext]:
    query = select(Assessment).where(Assessment.id == assessment_id)
    if lock:
        query = query.with_for_update()
    assessment = await db.scalar(query)
    if assessment is None:
        raise _error(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found")
    membership = await _course_membership(
        db,
        context,
        assessment.course_id,
        CourseRole.TEACHER if teacher else None,
    )
    return assessment, membership


async def _student_available(
    db: AsyncSession,
    assessment: Assessment,
    membership: MembershipContext,
) -> bool:
    if assessment.status != AssessmentStatus.PUBLISHED.value:
        return False
    try:
        await ensure_assessment_available(db, assessment, membership)
    except DomainError:
        return False
    return True


async def _assessment_student_read(
    db: AsyncSession,
    assessment: Assessment,
    membership: MembershipContext,
) -> AssessmentStudentRead:
    effective = await effective_assessment_policy(db, assessment, membership)
    assessment_policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    requires_live_lms_preparation = (
        assessment_policy.get("moodle_metadata_read_only") is True
    )
    # Only an active attempt is a resumable IDE session.  Returning the most
    # recent terminal/imported attempt here makes the student course links
    # bypass the start endpoint and opens an intentionally read-only
    # workspace.  In particular this prevented Moodle from preparing a new
    # retry after the previous attempt had been submitted.
    attempt = await db.scalar(
        select(Attempt)
        .where(
            Attempt.assessment_id == assessment.id,
            Attempt.principal_id == membership.principal.id,
            Attempt.state == AttemptState.ACTIVE.value,
        )
        .order_by(Attempt.sequence.desc())
        .limit(1)
    )
    return AssessmentStudentRead(
        id=assessment.id,
        course_id=assessment.course_id,
        type=assessment.type,
        title=assessment.title,
        instructions=assessment.instructions,
        opens_at=effective.opens_at,
        closes_at=effective.closes_at,
        duration_seconds=effective.duration_seconds,
        attempt_limit=effective.attempt_limit,
        max_score=assessment.max_score,
        paste_policy=assessment.paste_policy,
        student_ai_enabled=assessment.student_ai_enabled,
        review_required=assessment.review_required,
        autosubmit=assessment.autosubmit,
        multi_file=assessment.multi_file,
        status=assessment.status,
        published_at=assessment.published_at,
        attempt_id=attempt.id if attempt else None,
        progress=0 if attempt else None,
        task=None,
        requires_live_lms_preparation=requires_live_lms_preparation,
    )


async def _assessment_teacher_read(
    db: AsyncSession,
    assessment: Assessment,
) -> AssessmentTeacherRead:
    items = list(
        (
            await db.scalars(
                select(AssessmentItem)
                .where(AssessmentItem.assessment_id == assessment.id)
                .order_by(AssessmentItem.position, AssessmentItem.created_at)
            )
        ).all()
    )
    rules = list(
        (
            await db.scalars(
                select(AvailabilityRule)
                .where(AvailabilityRule.assessment_id == assessment.id)
                .order_by(AvailabilityRule.created_at)
            )
        ).all()
    )
    return AssessmentTeacherRead(
        id=assessment.id,
        course_id=assessment.course_id,
        section_id=assessment.section_id,
        type=assessment.type,
        title=assessment.title,
        instructions=assessment.instructions,
        opens_at=assessment.opens_at,
        closes_at=assessment.closes_at,
        duration_seconds=assessment.duration_seconds,
        attempt_limit=assessment.attempt_limit,
        max_score=assessment.max_score,
        paste_policy=assessment.paste_policy,
        student_ai_enabled=assessment.student_ai_enabled,
        teacher_ai_enabled=assessment.teacher_ai_enabled,
        review_required=assessment.review_required,
        decision_support_enabled=assessment.decision_support_enabled,
        autosubmit=assessment.autosubmit,
        multi_file=assessment.multi_file,
        status=assessment.status,
        policy=assessment.policy,
        published_at=assessment.published_at,
        created_by_id=assessment.created_by_id,
        created_at=assessment.created_at,
        updated_at=assessment.updated_at,
        items=[AssessmentItemTeacherRead.model_validate(item) for item in items],
        availability_rules=[AvailabilityRuleRead.model_validate(rule) for rule in rules],
    )


async def _validate_section(
    db: AsyncSession, section_id: uuid.UUID | None, course_id: uuid.UUID
) -> None:
    if section_id is None:
        return
    section = await db.scalar(
        select(CourseSection.id).where(
            CourseSection.id == section_id,
            CourseSection.course_id == course_id,
        )
    )
    if section is None:
        raise _error(422, "SECTION_COURSE_MISMATCH", "Section does not belong to this course")


@router.get("/courses/{course_id}/assessments")
async def list_course_assessments(
    course_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> list[AssessmentStudentRead | AssessmentTeacherRead]:
    membership = await _course_membership(db, context, course_id)
    rows = list(
        (
            await db.scalars(
                select(Assessment)
                .where(Assessment.course_id == course_id)
                .order_by(Assessment.opens_at, Assessment.created_at)
            )
        ).all()
    )
    # A multi-Essay Moodle Quiz is an activity container, not one programming
    # assignment.  Once history discovery has materialized its Essay questions,
    # expose only those question assessments in course views.
    rows = [
        row
        for row in rows
        if not (
            isinstance(row.policy, dict)
            and row.policy.get("historical_quiz_split_container") is True
        )
    ]
    if membership.membership.role == CourseRole.TEACHER.value:
        return [await _assessment_teacher_read(db, row) for row in rows]
    result: list[AssessmentStudentRead] = []
    for row in rows:
        if await _student_available(db, row, membership):
            result.append(await _assessment_student_read(db, row, membership))
    return result


@router.post(
    "/courses/{course_id}/assessments",
    response_model=AssessmentTeacherRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_assessment(
    course_id: uuid.UUID,
    payload: AssessmentCreateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentTeacherRead:
    await _course_membership(db, context, course_id, CourseRole.TEACHER)
    await _validate_section(db, payload.section_id, course_id)
    _json_size_guard(payload.policy, field="assessment policy", maximum=256 * 1024)
    row = Assessment(
        course_id=course_id,
        section_id=payload.section_id,
        type=payload.type.value,
        title=payload.title,
        instructions=payload.instructions,
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        duration_seconds=payload.duration_seconds,
        attempt_limit=payload.attempt_limit,
        max_score=payload.max_score,
        paste_policy=payload.paste_policy,
        student_ai_enabled=payload.student_ai_enabled,
        teacher_ai_enabled=payload.teacher_ai_enabled,
        review_required=payload.review_required,
        decision_support_enabled=payload.decision_support_enabled,
        autosubmit=payload.autosubmit,
        multi_file=payload.multi_file,
        status=AssessmentStatus.DRAFT.value,
        policy=payload.policy,
        created_by_id=context.principal_id,
    )
    db.add(row)
    await db.flush()
    await _sync_assessment_mapping(db, row)
    result = await _assessment_teacher_read(db, row)
    await db.commit()
    return result


@router.get("/assessments/{assessment_id}")
async def get_assessment(
    assessment_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentStudentRead | AssessmentTeacherRead:
    assessment, membership = await _assessment_access(db, context, assessment_id)
    if membership.membership.role == CourseRole.TEACHER.value:
        return await _assessment_teacher_read(db, assessment)
    if not await _student_available(db, assessment, membership):
        raise _error(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found")
    return await _assessment_student_read(db, assessment, membership)


@router.patch("/assessments/{assessment_id}", response_model=AssessmentTeacherRead)
async def update_assessment(
    assessment_id: uuid.UUID,
    payload: AssessmentUpdateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentTeacherRead:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True, lock=True)
    changes = payload.model_dump(exclude_unset=True)
    managed_mapping = await _managed_moodle_mapping(db, assessment.id)
    moodle_owned_fields = {
        "section_id",
        "type",
        "title",
        "instructions",
        "opens_at",
        "closes_at",
        "duration_seconds",
        "attempt_limit",
        "max_score",
        "policy",
    }
    if managed_mapping is not None and changes.keys() & moodle_owned_fields:
        raise _error(
            409,
            "MOODLE_METADATA_READ_ONLY",
            "Moodle is the source of the work title, condition, schedule, grade and attempts",
            fields=sorted(changes.keys() & moodle_owned_fields),
        )
    runtime_policy_fields = {"decision_support_enabled"}
    if (
        assessment.status != AssessmentStatus.DRAFT.value
        and not changes.keys() <= runtime_policy_fields
    ):
        raise _error(409, "ASSESSMENT_IMMUTABLE", "Only a draft assessment can be edited")
    if "policy" in changes and changes["policy"] is not None:
        _json_size_guard(changes["policy"], field="assessment policy", maximum=256 * 1024)
    if "section_id" in changes:
        await _validate_section(db, changes["section_id"], assessment.course_id)
    prospective_open = changes.get("opens_at", assessment.opens_at)
    prospective_close = changes.get("closes_at", assessment.closes_at)
    if prospective_open is not None and prospective_close is not None:
        if _aware(prospective_close) <= _aware(prospective_open):
            raise _error(422, "INVALID_ASSESSMENT_WINDOW", "closes_at must follow opens_at")
    for name, value in changes.items():
        if name == "type" and value is not None:
            value = value.value
        setattr(assessment, name, value)
    await _sync_assessment_mapping(db, assessment)
    await db.commit()
    return await _assessment_teacher_read(db, assessment)


@router.delete("/assessments/{assessment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_assessment(
    assessment_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> Response:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True, lock=True)
    if assessment.status != AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_IMMUTABLE", "Only a draft assessment can be deleted")
    attempt_count = await db.scalar(
        select(func.count(Attempt.id)).where(Attempt.assessment_id == assessment.id)
    )
    if attempt_count:
        raise _error(409, "ASSESSMENT_HAS_ATTEMPTS", "Assessment with attempts cannot be deleted")
    await db.execute(
        delete(ExternalMapping).where(
            ExternalMapping.local_id == assessment.id,
            ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
        )
    )
    await db.delete(assessment)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/assessments/{assessment_id}/items",
    response_model=AssessmentItemTeacherRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_assessment_item(
    assessment_id: uuid.UUID,
    payload: AssessmentItemCreateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentItemTeacherRead:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True, lock=True)
    if assessment.status != AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_IMMUTABLE", "Items can only be added to a draft")
    version = await db.get(TaskVersion, payload.task_version)
    if version is None or version.status != TaskVersionStatus.PUBLISHED.value:
        raise _error(422, "TASK_VERSION_NOT_PUBLISHED", "Select a published task version")
    item = await db.get(TaskBankItem, version.item_id)
    if item is None or (
        item.scope == TaskScope.COURSE.value and item.course_id != assessment.course_id
    ):
        raise _error(422, "TASK_COURSE_MISMATCH", "Task does not belong to this course")
    # Published SYSTEM tasks may be consumed by course teachers; elevation is required only
    # for authoring or changing those tasks.
    _json_size_guard(payload.assignment_rule, field="assignment rule", maximum=256 * 1024)
    duplicate = await db.scalar(
        select(AssessmentItem.id).where(
            AssessmentItem.assessment_id == assessment.id,
            AssessmentItem.task_version_id == version.id,
        )
    )
    if duplicate is not None:
        raise _error(409, "ASSESSMENT_ITEM_EXISTS", "Task version is already attached")
    row = AssessmentItem(
        assessment_id=assessment.id,
        task_version_id=version.id,
        position=payload.position,
        points=payload.points,
        assignment_rule=payload.assignment_rule,
    )
    db.add(row)
    await db.flush()
    result = AssessmentItemTeacherRead.model_validate(row)
    await db.commit()
    return result


@router.delete("/assessments/{assessment_id}/items/{item_id}", status_code=204)
async def remove_assessment_item(
    assessment_id: uuid.UUID,
    item_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> Response:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True, lock=True)
    if assessment.status != AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_IMMUTABLE", "Items can only be removed from a draft")
    row = await db.scalar(
        select(AssessmentItem).where(
            AssessmentItem.id == item_id,
            AssessmentItem.assessment_id == assessment.id,
        )
    )
    if row is None:
        raise _error(404, "ASSESSMENT_ITEM_NOT_FOUND", "Assessment item was not found")
    await db.delete(row)
    await db.commit()
    return Response(status_code=204)


@router.post(
    "/assessments/{assessment_id}/availability-rules",
    response_model=AvailabilityRuleRead,
    status_code=status.HTTP_201_CREATED,
)
async def add_availability_rule(
    assessment_id: uuid.UUID,
    payload: AvailabilityRuleCreateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> AvailabilityRuleRead:
    assessment, membership = await _assessment_access(
        db, context, assessment_id, teacher=True, lock=True
    )
    if await _managed_moodle_mapping(db, assessment.id) is not None:
        raise _error(
            409,
            "MOODLE_PUBLICATION_GROUPS_REQUIRED",
            "Use assessment publication to set Moodle group visibility atomically",
        )
    if assessment.status != AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_IMMUTABLE", "Rules can only be added to a draft")
    if payload.target_type == AvailabilityTarget.GROUP:
        group = await db.scalar(
            select(CourseGroup.id).where(
                CourseGroup.course_id == assessment.course_id,
                CourseGroup.external_id == payload.target_external_id,
                CourseGroup.active.is_(True),
            )
        )
        if group is None:
            raise _error(422, "GROUP_COURSE_MISMATCH", "Group does not belong to this course")
    elif payload.target_type == AvailabilityTarget.COURSE and payload.target_external_id not in {
        "",
        membership.course.external_id,
        str(membership.course.id),
    }:
        raise _error(422, "COURSE_RULE_MISMATCH", "Course rule targets another course")
    row = AvailabilityRule(
        assessment_id=assessment.id,
        target_type=payload.target_type.value,
        target_external_id=payload.target_external_id,
        allowed=payload.allowed,
        opens_at=payload.opens_at,
        closes_at=payload.closes_at,
        duration_seconds=payload.duration_seconds,
        attempt_limit=payload.attempt_limit,
        authored_by_id=context.principal_id,
    )
    db.add(row)
    await db.flush()
    result = AvailabilityRuleRead.model_validate(row)
    await db.commit()
    return result


@router.delete(
    "/assessments/{assessment_id}/availability-rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_availability_rule(
    assessment_id: uuid.UUID,
    rule_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> Response:
    assessment, _membership = await _assessment_access(
        db,
        context,
        assessment_id,
        teacher=True,
        lock=True,
    )
    if await _managed_moodle_mapping(db, assessment.id) is not None:
        raise _error(
            409,
            "MOODLE_PUBLICATION_GROUPS_REQUIRED",
            "Use assessment publication to update Moodle group visibility atomically",
        )
    rule = await db.scalar(
        select(AvailabilityRule)
        .where(
            AvailabilityRule.id == rule_id,
            AvailabilityRule.assessment_id == assessment.id,
        )
        .with_for_update()
    )
    if rule is None:
        raise _error(404, "AVAILABILITY_RULE_NOT_FOUND", "Availability rule was not found")
    if assessment.status != AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_IMMUTABLE", "Rules can only be removed from a draft")
    await db.delete(rule)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _validate_assessment(
    db: AsyncSession,
    assessment: Assessment,
) -> tuple[list[ValidationIssue], list[ValidationIssue]]:
    errors: list[ValidationIssue] = []
    warnings: list[ValidationIssue] = []
    policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    managed_mapping = await _managed_moodle_mapping(db, assessment.id)
    if policy.get("moodle_metadata_read_only") is True or managed_mapping is not None:
        mapping_issue = _managed_moodle_mapping_issue(
            assessment,
            await db.get(Course, assessment.course_id),
            managed_mapping,
        )
        if mapping_issue is not None:
            errors.append(mapping_issue)
    if policy.get("moodle_metadata_read_only") is True:
        missing_source = missing_moodle_source_confirmations(
            policy.get("moodle_source_confirmation")
        )
        if missing_source:
            warnings.append(
                ValidationIssue(
                    field="policy",
                    code="MOODLE_SOURCE_UNCONFIRMED",
                    message=(
                        "The latest Moodle synchronization did not confirm all "
                        "assessment fields: "
                        + ", ".join(missing_source)
                        + ". The last imported definition will be used for group "
                        "publication; Moodle validates the current student access "
                        "and answer format when the attempt starts"
                    ),
                )
            )
    if policy.get("lms_import_requires_configuration") is True:
        warnings.append(
            ValidationIssue(
                field="policy",
                code="LMS_IMPORT_REQUIRES_CONFIGURATION",
                message=(
                    "Imported Moodle metadata is incomplete. The work can still be "
                    "published locally; the Moodle connector will validate its answer "
                    "transport when an attempt is opened or delivered"
                ),
            )
        )
    if assessment.opens_at and assessment.closes_at:
        if _aware(assessment.closes_at) <= _aware(assessment.opens_at):
            errors.append(
                ValidationIssue(
                    field="closes_at",
                    code="INVALID_WINDOW",
                    message="Closing time must follow opening time",
                )
            )
    if assessment.max_score <= 0:
        errors.append(
            ValidationIssue(
                field="max_score",
                code="POSITIVE_SCORE_REQUIRED",
                message="Maximum score must be positive",
            )
        )
    if assessment.section_id is not None:
        section = await db.scalar(
            select(CourseSection.id).where(
                CourseSection.id == assessment.section_id,
                CourseSection.course_id == assessment.course_id,
            )
        )
        if section is None:
            errors.append(
                ValidationIssue(
                    field="section_id",
                    code="SECTION_COURSE_MISMATCH",
                    message="Section does not belong to this course",
                )
            )
    rows = list(
        (
            await db.execute(
                select(AssessmentItem, TaskVersion, TaskBankItem)
                .join(TaskVersion, TaskVersion.id == AssessmentItem.task_version_id)
                .join(TaskBankItem, TaskBankItem.id == TaskVersion.item_id)
                .where(AssessmentItem.assessment_id == assessment.id)
            )
        ).all()
    )
    if not rows:
        errors.append(
            ValidationIssue(
                field="items",
                code="TASK_REQUIRED",
                message="Attach at least one task version",
            )
        )
    for attached, version, item in rows:
        if version.status == TaskVersionStatus.DRAFT.value:
            version_errors, _version_warnings = _validate_task_version(version)
            for issue in version_errors:
                # Keep the stable machine-readable code, but identify that the
                # problem belongs to the task attached to this assessment.
                # The UI can therefore render one actionable checklist without
                # exposing an internal TaskVersion UUID.
                errors.append(
                    ValidationIssue(
                        field=f"task.{issue.field}" if issue.field else "task",
                        code=issue.code,
                        message=issue.message,
                    )
                )
            if not version_errors:
                warnings.append(
                    ValidationIssue(
                        field="items",
                        code="TASK_WILL_BE_PUBLISHED",
                        message=(
                            "The configured draft task version will be published together "
                            "with the assessment"
                        ),
                    )
                )
        elif version.status != TaskVersionStatus.PUBLISHED.value:
            errors.append(
                ValidationIssue(
                    field="items",
                    code="TASK_VERSION_UNAVAILABLE",
                    message="The attached task version is archived or unavailable",
                )
            )
        if item.scope == TaskScope.COURSE.value and item.course_id != assessment.course_id:
            errors.append(
                ValidationIssue(
                    field="items",
                    code="TASK_COURSE_MISMATCH",
                    message="The attached task belongs to another course",
                )
            )
        if version.multi_file != assessment.multi_file:
            errors.append(
                ValidationIssue(
                    field="multi_file",
                    code="FILE_MODE_MISMATCH",
                    message="The attached task has a different file mode",
                )
            )
        if attached.points <= 0 or attached.points > assessment.max_score:
            errors.append(
                ValidationIssue(
                    field="items.points",
                    code="INVALID_POINTS",
                    message="Each attached task must award between zero and maximum score",
                )
            )
    if assessment.closes_at is None:
        warnings.append(
            ValidationIssue(
                field="closes_at",
                code="NO_CLOSING_TIME",
                message="Assessment has no closing time",
            )
        )
    return errors, warnings


def _require_latest_attempt_quiz_grading_for_publication(
    mapping: ExternalMapping,
) -> None:
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    activity = metadata.get("activity") if isinstance(metadata.get("activity"), dict) else {}
    module = str(metadata.get("module", activity.get("module", ""))).lower().removeprefix("mod_")
    if module != "quiz":
        return
    if moodle_quiz_uses_latest_attempt_grade(metadata.get("activity")):
        return
    raise _error(
        409,
        "MOODLE_LAST_ATTEMPT_GRADING_REQUIRED",
        (
            "Moodle Quiz must use the 'Last attempt' grading method. Moodle may "
            "grant an additional attempt to an individual student later, and the "
            "reviewed work must always remain the latest attempt"
        ),
    )


async def _publication_groups(
    db: AsyncSession,
    context: CurrentAuth,
    assessment: Assessment,
) -> list[CourseGroup]:
    query = select(CourseGroup).where(
        CourseGroup.course_id == assessment.course_id,
        CourseGroup.active.is_(True),
    )
    if not context.has_capability("SYSTEM_SETTINGS"):
        allowed = await publication_group_ids_for_teacher(
            db,
            principal_id=context.principal_id,
            course_id=assessment.course_id,
        )
        query = query.where(CourseGroup.id.in_(allowed))
    return list((await db.scalars(query.order_by(CourseGroup.name))).all())


@router.get(
    "/assessments/{assessment_id}/publication-targets",
    response_model=AssessmentPublicationTargetsRead,
)
async def assessment_publication_targets(
    assessment_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentPublicationTargetsRead:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True)
    mapping = await _managed_moodle_mapping(db, assessment.id)
    if mapping is None:
        raise _error(
            409,
            "MOODLE_MANAGED_ASSESSMENT_REQUIRED",
            "Moodle publication targets are available only for imported work",
        )
    groups = await _publication_groups(db, context, assessment)
    return AssessmentPublicationTargetsRead(
        groups=[AssessmentPublicationGroupRead.model_validate(group) for group in groups],
        principals=[],
        overrides_confirmed=True,
    )


async def _replace_managed_publication_rules(
    db: AsyncSession,
    *,
    assessment: Assessment,
    groups: list[CourseGroup],
    authored_by_id: uuid.UUID,
) -> None:
    await db.execute(
        delete(AvailabilityRule).where(AvailabilityRule.assessment_id == assessment.id)
    )
    for group in groups:
        db.add(
            AvailabilityRule(
                assessment_id=assessment.id,
                target_type=AvailabilityTarget.GROUP.value,
                target_external_id=group.external_id,
                allowed=True,
                authored_by_id=authored_by_id,
            )
        )


@router.post("/assessments/{assessment_id}/validate", response_model=AssessmentValidationRead)
async def validate_assessment(
    assessment_id: uuid.UUID,
    _: AssessmentValidateRequest,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentValidationRead:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True)
    errors, warnings = await _validate_assessment(db, assessment)
    return AssessmentValidationRead(
        assessment_id=assessment.id,
        valid=not errors,
        errors=errors,
        warnings=warnings,
    )


@router.post("/assessments/{assessment_id}/publish", response_model=AssessmentTeacherRead)
async def publish_assessment(
    assessment_id: uuid.UUID,
    payload: AssessmentPublishRequest,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentTeacherRead:
    assessment, membership = await _assessment_access(
        db,
        context,
        assessment_id,
        teacher=True,
        lock=True,
    )
    managed_mapping = await _managed_moodle_mapping(db, assessment.id)
    policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    if policy.get("moodle_metadata_read_only") is True or managed_mapping is not None:
        mapping_issue = _managed_moodle_mapping_issue(
            assessment,
            membership.course,
            managed_mapping,
        )
        if mapping_issue is not None:
            raise _error(
                409,
                "ASSESSMENT_INVALID",
                "Assessment is not ready for publication",
                errors=[mapping_issue.model_dump(mode="json")],
            )
    groups: list[CourseGroup] = []
    if managed_mapping is not None:
        requested_group_ids = set(payload.group_ids or [])
        requested_principal_ids = set(payload.principal_ids or [])
        if requested_principal_ids:
            raise _error(
                422,
                "INDIVIDUAL_PUBLICATION_UNSUPPORTED",
                (
                    "Open Moodle-managed work to a teacher group. Individual "
                    "availability is checked directly in Moodle when a student starts"
                ),
            )
        if not requested_group_ids:
            raise _error(
                422,
                "MOODLE_PUBLICATION_TARGETS_REQUIRED",
                "Select at least one Moodle group before enabling the work",
            )
        groups = list(
            (
                await db.scalars(
                    select(CourseGroup).where(
                        CourseGroup.id.in_(requested_group_ids),
                        CourseGroup.course_id == assessment.course_id,
                        CourseGroup.active.is_(True),
                    )
                )
            ).all()
        )
        if {group.id for group in groups} != requested_group_ids:
            raise _error(
                422,
                "GROUP_COURSE_MISMATCH",
                "One or more groups are not active Moodle groups of this course",
            )
        if not context.has_capability("SYSTEM_SETTINGS"):
            allowed_group_ids = await publication_group_ids_for_teacher(
                db,
                principal_id=context.principal_id,
                course_id=assessment.course_id,
            )
            forbidden = requested_group_ids - allowed_group_ids
            if forbidden:
                raise _error(
                    403,
                    "GROUP_SCOPE_REQUIRED",
                    "A teacher may enable work only for Moodle groups assigned to them",
                )
        _require_latest_attempt_quiz_grading_for_publication(managed_mapping)
    if assessment.status == AssessmentStatus.PUBLISHED.value:
        if managed_mapping is not None:
            await _replace_managed_publication_rules(
                db,
                assessment=assessment,
                groups=groups,
                authored_by_id=context.principal_id,
            )
            await db.commit()
        return await _assessment_teacher_read(db, assessment)
    if assessment.status != AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_NOT_DRAFT", "Only a draft assessment can be published")
    errors, _ = await _validate_assessment(db, assessment)
    if managed_mapping is not None:
        # Moodle is the runtime authority for each student. A group may include
        # a student whose personal Moodle override is valid even when the
        # activity-wide window is already closed (or was imported malformed).
        # Keep all other source/content validation fail-closed.
        errors = [issue for issue in errors if issue.code != "INVALID_WINDOW"]
    if errors:
        raise _error(
            409,
            "ASSESSMENT_INVALID",
            "Assessment is not ready for publication",
            errors=[item.model_dump(mode="json") for item in errors],
        )
    if managed_mapping is not None:
        await _replace_managed_publication_rules(
            db,
            assessment=assessment,
            groups=groups,
            authored_by_id=context.principal_id,
        )
    versions = list(
        (
            await db.scalars(
                select(TaskVersion)
                .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
                .where(AssessmentItem.assessment_id == assessment.id)
                .with_for_update()
            )
        ).all()
    )
    # Publishing a work is the single explicit user action that freezes both
    # the assessment and any valid attached draft versions.  This removes the
    # surprising two-step "publish task, then publish work" workflow. Legacy
    # transport-configuration markers are warnings; structural task errors
    # remain blocking.
    for version in versions:
        if version.status == TaskVersionStatus.DRAFT.value:
            version.status = TaskVersionStatus.PUBLISHED.value
            version.published_at = utcnow()
    assessment.status = AssessmentStatus.PUBLISHED.value
    assessment.published_at = utcnow()
    await _sync_assessment_mapping(db, assessment)
    mirror_rows = list(
        (
            await db.execute(
                select(TaskBankItem, TaskVersion)
                .join(TaskVersion, TaskVersion.item_id == TaskBankItem.id)
                .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
                .where(AssessmentItem.assessment_id == assessment.id)
            )
        ).all()
    )
    for item, version in mirror_rows:
        await _enqueue_task_mirror(
            db,
            item=item,
            version=version,
            course_id=assessment.course_id,
        )
    await db.commit()
    return await _assessment_teacher_read(db, assessment)


@router.post("/assessments/{assessment_id}/close", response_model=AssessmentTeacherRead)
async def close_assessment(
    assessment_id: uuid.UUID,
    context: CurrentAuth,
    db: DBSession,
) -> AssessmentTeacherRead:
    assessment, _ = await _assessment_access(db, context, assessment_id, teacher=True, lock=True)
    if assessment.status == AssessmentStatus.DRAFT.value:
        raise _error(409, "ASSESSMENT_NOT_PUBLISHED", "Draft assessment cannot be closed")
    assessment.status = AssessmentStatus.CLOSED.value
    await db.commit()
    return await _assessment_teacher_read(db, assessment)
