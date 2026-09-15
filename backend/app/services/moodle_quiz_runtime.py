from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.integrations.moodle_modes import moodle_auth_mode, moodle_pluginless_transport
from app.integrations.moodle_transport import normalize_moodle_essay_answer_transport
from app.models.attempts import Attempt
from app.models.courses import Course
from app.models.enums import LMSProvider, TaskVersionStatus
from app.models.identity import LMSConnection
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment, TaskBankItem, TaskVersion
from app.services.build_profile import effective_workspace_build_profile
from app.services.common import DomainError, canonical_hash, sha256_text
from app.services.moodle_source import moodle_statement_is_deferred

_RUNTIME_TRANSPORTS = frozenset({"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"})
_MAX_QUESTION_TEXT = 50_000


@dataclass(frozen=True, slots=True)
class DeferredMoodleQuizContext:
    assessment: Assessment
    course: Course
    connection: LMSConnection
    mapping: ExternalMapping
    cmid: int


@dataclass(frozen=True, slots=True)
class MoodleAssignmentContext:
    assessment: Assessment
    course: Course
    connection: LMSConnection
    mapping: ExternalMapping
    cmid: int


@dataclass(frozen=True, slots=True)
class PreparedMoodleAssignment:
    course_external_id: str
    cmid: int
    answer_transport: str
    available_answer_transports: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PreparedMoodleQuizAttempt:
    course_external_id: str
    cmid: int
    external_attempt_id: str
    question_slot: str
    question_text: str
    answer_transport: str
    available_answer_transports: tuple[str, ...]
    remaining_seconds: int | None = None
    question_max_mark: Decimal | None = None
    questions: tuple[PreparedMoodleQuizAttempt, ...] = ()


def apply_prepared_quiz_timer(
    attempt: Attempt,
    *,
    remaining_seconds: int | None,
    reserve_seconds: int,
    now: datetime,
) -> None:
    """Reserve upload time against the live, per-user timer, never course dates.

    The configured reserve is pinned once per attempt. A subsequent successful
    preparation can refresh Moodle's remaining time (including an extension),
    but never subtracts the reserve from an already reduced local deadline.
    A missing timer cannot erase a previously confirmed deadline.
    """
    policy = dict(attempt.integrity_policy or {})
    pinned = policy.get("moodle_sync_timeout_seconds")
    has_pinned_reserve = type(pinned) is int and 0 <= pinned <= 86_400
    if remaining_seconds is None:
        if not has_pinned_reserve:
            # Repair legacy deadlines derived from global dates, which do not
            # reflect student/group overrides. Unlimited work stays untimed.
            attempt.expected_end_at = None
            attempt.deadline_at = None
        return
    reserve = pinned if has_pinned_reserve else reserve_seconds
    attempt.expected_end_at = now + timedelta(seconds=remaining_seconds)
    attempt.deadline_at = now + timedelta(seconds=max(0, remaining_seconds - reserve))
    attempt.integrity_policy = {**policy, "moodle_sync_timeout_seconds": reserve}


def prepared_moodle_assignment(
    *,
    course_external_id: object,
    cmid: object,
    answer_transport: object,
    available_answer_transports: object,
) -> PreparedMoodleAssignment:
    course_id = _positive_decimal_id(course_external_id, "Moodle course id")
    normalized_cmid = int(_positive_decimal_id(cmid, "Moodle activity id"))
    transport = normalize_moodle_essay_answer_transport(answer_transport)
    supported = frozenset({"ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"})
    if transport not in supported or not isinstance(available_answer_transports, list | tuple):
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle Assignment answer format is invalid",
        )
    available = tuple(
        value
        for raw in available_answer_transports
        if (value := normalize_moodle_essay_answer_transport(raw)) in supported
    )
    if (
        not available
        or len(available) != len(available_answer_transports)
        or len(available) != len(set(available))
        or transport not in available
    ):
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle Assignment answer formats are inconsistent",
        )
    return PreparedMoodleAssignment(
        course_external_id=course_id,
        cmid=normalized_cmid,
        answer_transport=transport,
        available_answer_transports=available,
    )


def _positive_decimal_id(value: object, label: str, *, maximum: int = 2_147_483_647) -> str:
    normalized = str(value).strip()
    if not normalized.isdigit() or not 0 < int(normalized) <= maximum:
        raise DomainError(502, "INVALID_MOODLE_PREPARATION", f"{label} is invalid")
    return str(int(normalized))


def moodle_question_local_max_score(mark: Decimal) -> Decimal:
    """Use the app's two-decimal score scale without losing Moodle's raw mark.

    The exact mark remains in the immutable question binding. Grade delivery
    converts the relative local grade back to that Moodle scale.
    """

    rounded = mark.quantize(Decimal("0.01"))
    return rounded if rounded > 0 else Decimal("1.00")


def prepared_moodle_quiz_attempt(
    *,
    course_external_id: object,
    cmid: object,
    external_attempt_id: object,
    question_slot: object,
    question_text: object,
    answer_transport: object,
    available_answer_transports: object,
    remaining_seconds: object = None,
    question_max_mark: object = None,
    questions: object = None,
) -> PreparedMoodleQuizAttempt:
    course_id = _positive_decimal_id(course_external_id, "Moodle course id")
    normalized_cmid = int(_positive_decimal_id(cmid, "Moodle activity id"))
    attempt_id = _positive_decimal_id(
        external_attempt_id,
        "Moodle attempt id",
        maximum=9_223_372_036_854_775_807,
    )
    slot = _positive_decimal_id(question_slot, "Moodle question slot")
    if not isinstance(question_text, str):
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle question text is invalid",
        )
    statement = question_text.strip()
    if not statement or len(statement) > _MAX_QUESTION_TEXT:
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle question text is empty or too large",
        )
    transport = normalize_moodle_essay_answer_transport(answer_transport)
    if transport not in _RUNTIME_TRANSPORTS:
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle Essay answer format is invalid",
        )
    if not isinstance(available_answer_transports, list | tuple):
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle Essay answer formats are invalid",
        )
    available = tuple(
        value
        for raw in available_answer_transports
        if (value := normalize_moodle_essay_answer_transport(raw)) in _RUNTIME_TRANSPORTS
    )
    if (
        not available
        or len(available) != len(available_answer_transports)
        or len(available) != len(set(available))
        or transport not in available
    ):
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle Essay answer formats are inconsistent",
        )
    if remaining_seconds is not None and (
        isinstance(remaining_seconds, bool)
        or not isinstance(remaining_seconds, int)
        or not 0 <= remaining_seconds <= 315_360_000
    ):
        raise DomainError(
            502,
            "INVALID_MOODLE_PREPARATION",
            "Moodle remaining attempt time is invalid",
        )
    mark = None
    if question_max_mark is not None:
        try:
            mark = Decimal(str(question_max_mark))
        except (InvalidOperation, ValueError) as exc:
            raise DomainError(
                502, "INVALID_MOODLE_PREPARATION", "Moodle question mark is invalid"
            ) from exc
        if (
            not mark.is_finite()
            or not 0 < mark <= Decimal("999999.99")
            or mark != mark.quantize(Decimal("0.0000001"))
        ):
            raise DomainError(
                502, "INVALID_MOODLE_PREPARATION", "Moodle question mark is invalid"
            )
    prepared_questions: tuple[PreparedMoodleQuizAttempt, ...] = ()
    if questions is not None:
        if not isinstance(questions, list | tuple) or not 1 <= len(questions) <= 32:
            raise DomainError(
                502, "INVALID_MOODLE_PREPARATION", "Moodle question list is invalid"
            )
        parsed_questions = []
        for raw_question in questions:
            if not isinstance(raw_question, dict):
                raise DomainError(
                    502, "INVALID_MOODLE_PREPARATION", "Moodle question list is invalid"
                )
            question = prepared_moodle_quiz_attempt(
                course_external_id=course_id,
                cmid=normalized_cmid,
                external_attempt_id=attempt_id,
                question_slot=raw_question.get("question_slot"),
                question_text=raw_question.get("question_text"),
                answer_transport=raw_question.get("answer_transport"),
                available_answer_transports=raw_question.get("available_answer_transports"),
                remaining_seconds=remaining_seconds,
                question_max_mark=raw_question.get("question_max_mark"),
            )
            if len(questions) > 1 and question.question_max_mark is None:
                raise DomainError(
                    502, "INVALID_MOODLE_PREPARATION", "Moodle question mark is unconfirmed"
                )
            parsed_questions.append(question)
        first = parsed_questions[0]
        if (
            len({question.question_slot for question in parsed_questions}) != len(parsed_questions)
            or first.question_slot != slot
            or first.question_text != statement
            or first.answer_transport != transport
            or first.available_answer_transports != available
        ):
            raise DomainError(
                502, "INVALID_MOODLE_PREPARATION", "Moodle question list is inconsistent"
            )
        prepared_questions = tuple(parsed_questions)
        mark = first.question_max_mark
    return PreparedMoodleQuizAttempt(
        course_external_id=course_id,
        cmid=normalized_cmid,
        external_attempt_id=attempt_id,
        question_slot=slot,
        question_text=statement,
        answer_transport=transport,
        available_answer_transports=available,
        remaining_seconds=remaining_seconds,
        question_max_mark=mark,
        questions=prepared_questions,
    )


def _mapping_module(mapping: ExternalMapping, metadata: dict[str, Any]) -> str:
    external_type = mapping.external_type.lower().replace("-", "_")
    module = str(metadata.get("module", external_type)).lower().replace("-", "_")
    return module.removeprefix("mod_")


async def _resolve_moodle_activity_context(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> DeferredMoodleQuizContext | MoodleAssignmentContext | None:
    row = (
        await db.execute(
            select(Assessment, Course, LMSConnection)
            .join(Course, Course.id == Assessment.course_id)
            .join(LMSConnection, LMSConnection.id == Course.connection_id)
            .where(Assessment.id == assessment_id)
        )
    ).one_or_none()
    if row is None:
        return None
    assessment, course, connection = row
    assessment_policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    managed_by_moodle = assessment_policy.get("moodle_metadata_read_only") is True
    mappings = list(
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
    if len(mappings) != 1:
        if managed_by_moodle:
            raise DomainError(
                409,
                "MOODLE_RUNTIME_MAPPING_UNAVAILABLE",
                "The published Moodle assessment has no unambiguous activity mapping",
            )
        return None
    mapping = mappings[0]
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    activity = metadata.get("activity")
    activity = activity if isinstance(activity, dict) else {}
    module = _mapping_module(mapping, metadata)
    if module not in {"assign", "quiz"}:
        if managed_by_moodle:
            raise DomainError(
                409,
                "MOODLE_RUNTIME_MAPPING_UNAVAILABLE",
                "The published assessment is not mapped to a supported Moodle activity",
            )
        return None
    if not managed_by_moodle and not (module == "quiz" and moodle_statement_is_deferred(activity)):
        # Locally authored assessments may also have an optional LMS delivery
        # mapping. They retain the provider-neutral start contract; only an
        # imported Moodle work requires the live student-form admission gate.
        return None
    if connection.provider != LMSProvider.MOODLE.value or not connection.enabled:
        raise DomainError(
            409,
            "MOODLE_RUNTIME_PREPARATION_UNAVAILABLE",
            "Moodle connection is unavailable for this assessment",
        )
    if (
        moodle_auth_mode(connection) != "PLUGINLESS"
        or moodle_pluginless_transport(connection) != "PLAYWRIGHT"
    ):
        raise DomainError(
            409,
            "MOODLE_RUNTIME_PREPARATION_UNAVAILABLE",
            "This Moodle connection cannot verify a student attempt",
        )
    # Course discovery and admission are deliberately independent. A failed,
    # stale or incomplete background synchronization must not hide an already
    # published work, nor prevent the connector from checking the exact
    # student's live Moodle form. Stable course/activity identities are still
    # validated below; the prepare endpoint is authoritative for availability
    # and the current answer transport.
    external_cmid = _positive_decimal_id(mapping.external_id, "Moodle activity id")
    metadata_cmid = _positive_decimal_id(
        metadata.get("cmid", external_cmid),
        "Moodle activity id",
    )
    if external_cmid != metadata_cmid:
        raise DomainError(
            409,
            "MOODLE_RUNTIME_PREPARATION_STALE",
            "Moodle activity identity is inconsistent",
        )
    course_external_id = _positive_decimal_id(course.external_id, "Moodle course id")
    # Preserve the normalized value for callers without mutating LMS-owned rows.
    if course_external_id != str(course.external_id).strip():
        raise DomainError(
            409,
            "MOODLE_RUNTIME_PREPARATION_STALE",
            "Moodle course identity is inconsistent",
        )
    context_type = DeferredMoodleQuizContext if module == "quiz" else MoodleAssignmentContext
    return context_type(
        assessment=assessment,
        course=course,
        connection=connection,
        mapping=mapping,
        cmid=int(external_cmid),
    )


async def resolve_moodle_quiz_context(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> DeferredMoodleQuizContext | None:
    context = await _resolve_moodle_activity_context(db, assessment_id)
    return context if isinstance(context, DeferredMoodleQuizContext) else None


async def resolve_deferred_moodle_quiz_context(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> DeferredMoodleQuizContext | None:
    context = await resolve_moodle_quiz_context(db, assessment_id)
    if context is None:
        return None
    metadata = (
        context.mapping.metadata_json if isinstance(context.mapping.metadata_json, dict) else {}
    )
    activity = metadata.get("activity")
    return (
        context if isinstance(activity, dict) and moodle_statement_is_deferred(activity) else None
    )


async def resolve_moodle_assignment_context(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> MoodleAssignmentContext | None:
    context = await _resolve_moodle_activity_context(db, assessment_id)
    return context if isinstance(context, MoodleAssignmentContext) else None


def prepared_binding_matches_attempt(
    integrity_policy: object,
    prepared: PreparedMoodleQuizAttempt,
) -> bool:
    raw = integrity_policy if isinstance(integrity_policy, dict) else {}
    return all(
        str(raw.get(key, "")) == expected
        for key, expected in (
            ("moodle_course_id", prepared.course_external_id),
            ("moodle_cmid", str(prepared.cmid)),
            ("moodle_attempt_id", prepared.external_attempt_id),
            ("moodle_question_slot", prepared.question_slot),
            ("moodle_answer_transport", prepared.answer_transport),
        )
    )


def pinned_moodle_quiz_binding(
    integrity_policy: object,
    *,
    course_external_id: object,
    cmid: object,
) -> tuple[str, str, str] | None:
    raw = integrity_policy if isinstance(integrity_policy, dict) else {}
    try:
        expected_course = _positive_decimal_id(course_external_id, "Moodle course id")
        expected_cmid = _positive_decimal_id(cmid, "Moodle activity id")
        pinned_course = _positive_decimal_id(raw.get("moodle_course_id"), "Moodle course id")
        pinned_cmid = _positive_decimal_id(raw.get("moodle_cmid"), "Moodle activity id")
        attempt_id = _positive_decimal_id(
            raw.get("moodle_attempt_id"),
            "Moodle attempt id",
            maximum=9_223_372_036_854_775_807,
        )
        slot = _positive_decimal_id(raw.get("moodle_question_slot"), "Moodle question slot")
    except DomainError:
        return None
    transport = normalize_moodle_essay_answer_transport(raw.get("moodle_answer_transport"))
    if (
        pinned_course != expected_course
        or pinned_cmid != expected_cmid
        or transport not in _RUNTIME_TRANSPORTS
    ):
        return None
    return attempt_id, slot, transport


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
        "max_score": format(version.max_score, "f"),
        "difficulty": version.difficulty,
        "ai_policy": version.ai_policy,
    }


async def materialize_prepared_task_version(
    db: AsyncSession,
    *,
    base_version: TaskVersion,
    prepared: PreparedMoodleQuizAttempt,
) -> TaskVersion:
    item = await db.scalar(
        select(TaskBankItem).where(TaskBankItem.id == base_version.item_id).with_for_update()
    )
    if item is None:
        raise DomainError(500, "TASK_ITEM_MISSING", "Assigned task item is missing")
    versions = list(
        (
            await db.scalars(
                select(TaskVersion)
                .where(TaskVersion.item_id == base_version.item_id)
                .order_by(TaskVersion.number.desc())
                .with_for_update()
            )
        ).all()
    )
    for candidate in versions:
        policy = candidate.ai_policy if isinstance(candidate.ai_policy, dict) else {}
        if (
            policy.get("source") == "MOODLE_RUNTIME_ESSAY"
            and str(policy.get("moodle_course_id", "")) == prepared.course_external_id
            and str(policy.get("moodle_cmid", "")) == str(prepared.cmid)
            and str(policy.get("moodle_attempt_id", "")) == prepared.external_attempt_id
            and str(policy.get("moodle_question_slot", "")) == prepared.question_slot
        ):
            if (
                candidate.statement != prepared.question_text
                or policy.get("answer_transport") != prepared.answer_transport
                or (
                    policy.get("moodle_question_max_mark") is not None
                    and str(policy["moodle_question_max_mark"])
                    != str(prepared.question_max_mark)
                )
            ):
                raise DomainError(
                    409,
                    "MOODLE_ATTEMPT_BINDING_CONFLICT",
                    "Moodle returned different data for an already prepared attempt",
                )
            return candidate

    multi_file = prepared.answer_transport == "ESSAY_ATTACHMENT"
    ai_policy = {
        **dict(base_version.ai_policy or {}),
        "source": "MOODLE_RUNTIME_ESSAY",
        "statement_deferred": False,
        "moodle_course_id": prepared.course_external_id,
        "moodle_cmid": prepared.cmid,
        "moodle_attempt_id": prepared.external_attempt_id,
        "moodle_question_slot": prepared.question_slot,
        "answer_transport": prepared.answer_transport,
        "available_answer_transports": list(prepared.available_answer_transports),
        "question_sha256": sha256_text(prepared.question_text),
        "base_task_version_id": str(base_version.id),
    }
    question_scale = len(prepared.questions) > 1 or (
        prepared.question_max_mark is not None
        and dict(base_version.ai_policy or {}).get("historical_import_only") is True
    )
    if question_scale:
        ai_policy["moodle_question_max_mark"] = str(prepared.question_max_mark)
    version = TaskVersion(
        item_id=base_version.item_id,
        number=(versions[0].number if versions else base_version.number) + 1,
        title=base_version.title,
        statement=prepared.question_text,
        language=base_version.language,
        language_standard=base_version.language_standard,
        multi_file=multi_file,
        starter_files=(
            list(base_version.starter_files)
            if isinstance(base_version.starter_files, list) and base_version.starter_files
            else [{"path": "main.c" if base_version.language == "C" else "main.cpp", "content": ""}]
        ),
        build_profile=effective_workspace_build_profile(
            base_version.build_profile,
            multi_file=multi_file,
        ),
        public_examples=list(base_version.public_examples or []),
        hidden_test_manifest=dict(base_version.hidden_test_manifest or {}),
        max_score=(
            moodle_question_local_max_score(prepared.question_max_mark)
            if question_scale and prepared.question_max_mark is not None
            else base_version.max_score
        ),
        difficulty=base_version.difficulty,
        ai_policy=ai_policy,
        content_hash="",
        status=TaskVersionStatus.PUBLISHED.value,
        authored_by_id=base_version.authored_by_id,
        published_at=utcnow(),
    )
    version.content_hash = canonical_hash(_task_content(version))
    db.add(version)
    await db.flush()
    return version


__all__ = [
    "DeferredMoodleQuizContext",
    "PreparedMoodleQuizAttempt",
    "materialize_prepared_task_version",
    "moodle_question_local_max_score",
    "pinned_moodle_quiz_binding",
    "prepared_binding_matches_attempt",
    "prepared_moodle_quiz_attempt",
    "resolve_deferred_moodle_quiz_context",
]
