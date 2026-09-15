from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models.attempts import Attempt, Submission
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
)
from app.models.enums import AttemptState, AvailabilityTarget, CourseRole, TaskVersionStatus
from app.models.identity import ExternalPrincipal, LMSConnection
from app.models.tasks import Assessment, AssessmentItem, AvailabilityRule, TaskVersion
from app.services.common import DomainError
from app.services.teacher_tokens import teacher_membership_is_authorized


@dataclass(frozen=True, slots=True)
class MembershipContext:
    membership: CourseMembership
    principal: ExternalPrincipal
    course: Course
    group_external_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class EffectiveAssessmentPolicy:
    opens_at: datetime | None
    closes_at: datetime | None
    duration_seconds: int | None
    attempt_limit: int | None


@dataclass(frozen=True, slots=True)
class SubmissionReviewAccess:
    """Resolved, server-side scope for reading or changing a submitted work."""

    submission: Submission
    attempt: Attempt
    assessment: Assessment
    course: Course
    via_system_settings: bool
    assignment_source: str


_NAME_WORD_RE = re.compile(r"[0-9a-zа-я]+", re.IGNORECASE)
_SUBGROUP_RE = re.compile(r"\bподгрупп[\w-]*\s+(.+)", re.IGNORECASE)


def _name_words(value: str) -> list[str]:
    return [word.replace("ё", "е").casefold() for word in _NAME_WORD_RE.findall(value)]


def _without_redundant_compact_initials(words: list[str]) -> list[str]:
    """Drop UI-style leading initials only when the remaining name proves them."""

    if len(words) < 3 or not (1 <= len(words[0]) <= 3):
        return words
    remaining_initials = "".join(word[0] for word in words[1:] if word)
    return words[1:] if words[0] == remaining_initials[: len(words[0])] else words


def legacy_subgroup_is_assigned_to_teacher(group_name: str, teacher_display_name: str) -> bool:
    """Match MMCS ``подгруппа Фамилия И.О.`` labels without substrings.

    A surname must be an exact normalized word and the label must contain at
    least one matching initial or a complete given-name word.  Consequently a
    short/partial surname can never grant access.
    """

    match = _SUBGROUP_RE.search(group_name.replace("ё", "е"))
    if match is None:
        return False
    # Moodle group labels sometimes append unrelated metadata after a delimiter.
    assigned_label = re.split(r"[;,|/]", match.group(1), maxsplit=1)[0]
    assigned = _name_words(assigned_label)
    teacher = _without_redundant_compact_initials(_name_words(teacher_display_name))
    if len(assigned) < 2 or len(teacher) < 2:
        return False
    surname, qualifiers = assigned[0], assigned[1:]
    if len(surname) < 3 or surname not in teacher:
        return False

    surname_index = teacher.index(surname)
    other_names = [word for index, word in enumerate(teacher) if index != surname_index]
    if not other_names:
        return False
    if all(len(value) == 1 for value in qualifiers):
        initials = [word[0] for word in other_names]
        if qualifiers == initials:
            return True
        # The MMCS public profile may omit the patronymic (``Коваленко
        # Алексей``), while the course group keeps both initials
        # (``подгруппа Коваленко А.С.``).  In that one incomplete-profile
        # shape the surname and the known given-name initial are the strongest
        # identity evidence Moodle exposes.  A different first initial still
        # fails closed; complete three-part names continue to require an exact
        # initial sequence.
        return len(other_names) == 1 and len(qualifiers) > 1 and qualifiers[0] == initials[0]
    # A fully written label is accepted only as exact words, in either common
    # Russian display order (surname first or surname last).
    return qualifiers == other_names or assigned == teacher


def _active_membership_predicates(now: datetime) -> tuple:
    return (
        CourseMembership.active.is_(True),
        or_(CourseMembership.valid_until.is_(None), CourseMembership.valid_until > now),
    )


async def _ordinary_review_assignments(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    assessment_id: uuid.UUID | None = None,
) -> dict[uuid.UUID, set[uuid.UUID]]:
    """Return student principals visible to a token-authorized teacher per course."""

    if not await teacher_membership_is_authorized(db, principal_id):
        return {}
    teacher = await db.get(ExternalPrincipal, principal_id)
    if teacher is None or not teacher.active:
        return {}
    now = utcnow()
    teacher_statement = (
        select(CourseMembership)
        .join(Course, Course.id == CourseMembership.course_id)
        .join(LMSConnection, LMSConnection.id == Course.connection_id)
        .where(
            CourseMembership.principal_id == principal_id,
            CourseMembership.role == CourseRole.TEACHER.value,
            *_active_membership_predicates(now),
            Course.catalog_enabled.is_(True),
            Course.archived_at.is_(None),
            LMSConnection.enabled.is_(True),
        )
    )
    teacher_memberships = list((await db.scalars(teacher_statement)).all())
    if assessment_id is not None:
        assessment = await db.get(Assessment, assessment_id)
        if assessment is None:
            return {}
        teacher_memberships = [
            membership
            for membership in teacher_memberships
            if membership.course_id == assessment.course_id
        ]
    teacher_courses = {
        course.id: course
        for course in (
            await db.scalars(
                select(Course).where(
                    Course.id.in_([membership.course_id for membership in teacher_memberships])
                )
            )
        ).all()
    }
    teacher_memberships = [
        membership
        for membership in teacher_memberships
        if teacher_courses[membership.course_id].connection_id == teacher.connection_id
    ]
    if not teacher_memberships:
        return {}

    membership_ids = [membership.id for membership in teacher_memberships]
    teacher_group_rows = (
        await db.execute(
            select(
                CourseMembershipGroup.coursemembership_id,
                CourseMembershipGroup.coursegroup_id,
            )
            .join(
                CourseMembership,
                CourseMembership.id == CourseMembershipGroup.coursemembership_id,
            )
            .join(CourseGroup, CourseGroup.id == CourseMembershipGroup.coursegroup_id)
            .where(
                CourseMembershipGroup.coursemembership_id.in_(membership_ids),
                CourseGroup.active.is_(True),
                CourseGroup.course_id == CourseMembership.course_id,
            )
        )
    ).all()
    teacher_groups: dict[uuid.UUID, set[uuid.UUID]] = {
        membership.id: set() for membership in teacher_memberships
    }
    for membership_id, group_id in teacher_group_rows:
        teacher_groups.setdefault(membership_id, set()).add(group_id)

    course_ids = [membership.course_id for membership in teacher_memberships]
    student_group_rows = (
        await db.execute(
            select(CourseMembership, CourseGroup)
            .join(
                CourseMembershipGroup,
                CourseMembershipGroup.coursemembership_id == CourseMembership.id,
            )
            .join(CourseGroup, CourseGroup.id == CourseMembershipGroup.coursegroup_id)
            .join(ExternalPrincipal, ExternalPrincipal.id == CourseMembership.principal_id)
            .where(
                CourseMembership.course_id.in_(course_ids),
                CourseMembership.role == CourseRole.STUDENT.value,
                *_active_membership_predicates(now),
                CourseGroup.active.is_(True),
                CourseGroup.course_id == CourseMembership.course_id,
                ExternalPrincipal.active.is_(True),
                ExternalPrincipal.connection_id == teacher.connection_id,
            )
        )
    ).all()
    membership_by_course = {membership.course_id: membership for membership in teacher_memberships}
    allowed: dict[uuid.UUID, set[uuid.UUID]] = {
        membership.course_id: set() for membership in teacher_memberships
    }
    for student_membership, group in student_group_rows:
        teacher_membership = membership_by_course.get(student_membership.course_id)
        if teacher_membership is None:
            continue
        shared_group = group.id in teacher_groups.get(teacher_membership.id, set())
        legacy_group = legacy_subgroup_is_assigned_to_teacher(group.name, teacher.display_name)
        if shared_group or legacy_group:
            allowed.setdefault(student_membership.course_id, set()).add(
                student_membership.principal_id
            )
    return allowed


async def publication_group_ids_for_teacher(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    course_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Return Moodle groups that the current teacher may publish work to.

    Moodle installations do not expose teacher-to-subgroup assignments in one
    uniform shape.  Prefer explicit membership-group links and retain the
    narrowly validated legacy ``подгруппа Фамилия И.О.`` convention already
    used by review authorization.  The result contains local ``CourseGroup``
    identifiers; callers must still verify that submitted IDs belong to the
    requested course.
    """

    if not await teacher_membership_is_authorized(db, principal_id):
        return set()
    teacher = await db.get(ExternalPrincipal, principal_id)
    if teacher is None or not teacher.active:
        return set()
    now = utcnow()
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course_id,
            CourseMembership.principal_id == principal_id,
            CourseMembership.role == CourseRole.TEACHER.value,
            *_active_membership_predicates(now),
        )
    )
    if membership is None:
        return set()
    explicit = set(
        (
            await db.scalars(
                select(CourseMembershipGroup.coursegroup_id)
                .join(
                    CourseGroup,
                    CourseGroup.id == CourseMembershipGroup.coursegroup_id,
                )
                .where(
                    CourseMembershipGroup.coursemembership_id == membership.id,
                    CourseGroup.course_id == course_id,
                    CourseGroup.active.is_(True),
                )
            )
        ).all()
    )
    candidates = list(
        (
            await db.scalars(
                select(CourseGroup).where(
                    CourseGroup.course_id == course_id,
                    CourseGroup.active.is_(True),
                )
            )
        ).all()
    )
    return explicit | {
        group.id
        for group in candidates
        if legacy_subgroup_is_assigned_to_teacher(group.name, teacher.display_name)
    }


async def publication_student_ids_for_teacher(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    course_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Return students inside the current teacher's review/publication scope."""

    return (await _ordinary_review_assignments(db, principal_id=principal_id)).get(
        course_id,
        set(),
    )


async def assessment_review_scope_ids(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> set[uuid.UUID]:
    """Include an activity's managed Quiz questions, never unrelated courses.

    A request for an individual child remains scoped to that child. Parent
    activity queues, however, need both native and imported question responses
    before latest-attempt selection and pagination can group them correctly.
    """

    assessment = await db.get(Assessment, assessment_id)
    if assessment is None:
        return set()
    children = await db.scalars(
        select(Assessment.id).where(
            Assessment.course_id == assessment.course_id,
            Assessment.policy["moodle_quiz_question_split"].as_boolean().is_(True),
            Assessment.policy["moodle_parent_assessment_id"].as_string() == str(assessment_id),
        )
    )
    return {assessment_id, *children.all()}


async def visible_submission_ids_for_review(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    assessment_id: uuid.UUID | None = None,
    allow_system_settings_read: bool = False,
) -> set[uuid.UUID]:
    """Resolve the complete server-side read queue before pagination is applied."""

    base = (
        select(Submission.id)
        .join(Attempt, Attempt.id == Submission.attempt_id)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .join(Course, Course.id == Assessment.course_id)
        .where(
            Attempt.state != AttemptState.VOID.value,
            Course.catalog_enabled.is_(True),
            Course.archived_at.is_(None),
        )
    )
    if assessment_id is not None:
        base = base.where(Assessment.id.in_(await assessment_review_scope_ids(db, assessment_id)))
    if allow_system_settings_read:
        return set((await db.scalars(base)).all())

    assignments = await _ordinary_review_assignments(
        db,
        principal_id=principal_id,
        assessment_id=assessment_id,
    )
    predicates = [
        (Assessment.course_id == course_id) & (Attempt.principal_id.in_(student_ids))
        for course_id, student_ids in assignments.items()
        if student_ids
    ]
    if not predicates:
        return set()
    return set((await db.scalars(base.where(or_(*predicates)))).all())


async def visible_submission_ids_for_assessment(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    assessment_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> set[uuid.UUID]:
    return await visible_submission_ids_for_review(
        db,
        principal_id=principal_id,
        assessment_id=assessment_id,
        allow_system_settings_read=allow_system_settings_read,
    )


async def submission_review_access(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    submission_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> SubmissionReviewAccess | None:
    submission = await db.get(Submission, submission_id)
    attempt = await db.get(Attempt, submission.attempt_id) if submission else None
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    course = await db.get(Course, assessment.course_id) if assessment else None
    if submission is None or attempt is None or assessment is None or course is None:
        return None
    if attempt.state == AttemptState.VOID.value:
        return None
    if not course.catalog_enabled or course.archived_at is not None:
        return None
    if allow_system_settings_read:
        return SubmissionReviewAccess(
            submission, attempt, assessment, course, True, "SYSTEM_SETTINGS"
        )
    visible = await visible_submission_ids_for_assessment(
        db,
        principal_id=principal_id,
        assessment_id=assessment.id,
    )
    if submission.id not in visible:
        return None
    return SubmissionReviewAccess(submission, attempt, assessment, course, False, "COURSE_GROUP")


async def require_submission_review_access(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    submission_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> SubmissionReviewAccess:
    submission = await db.get(Submission, submission_id)
    if submission is None:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
    access = await submission_review_access(
        db,
        principal_id=principal_id,
        submission_id=submission_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    if access is None:
        raise DomainError(
            403,
            "SUBMISSION_REVIEW_SCOPE_REQUIRED",
            "The submission is outside this teacher's assigned student groups",
        )
    return access


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def require_membership(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    course_id: uuid.UUID,
    role: CourseRole | str | None = None,
) -> MembershipContext:
    now = utcnow()
    memberships = list(
        (
            await db.scalars(
                select(CourseMembership)
                .join(Course, Course.id == CourseMembership.course_id)
                .join(LMSConnection, LMSConnection.id == Course.connection_id)
                .where(
                    CourseMembership.principal_id == principal_id,
                    CourseMembership.course_id == course_id,
                    CourseMembership.active.is_(True),
                    Course.catalog_enabled.is_(True),
                    Course.archived_at.is_(None),
                    LMSConnection.enabled.is_(True),
                    or_(
                        CourseMembership.valid_until.is_(None),
                        CourseMembership.valid_until > now,
                    ),
                )
            )
        ).all()
    )
    if not await teacher_membership_is_authorized(db, principal_id):
        memberships = [row for row in memberships if row.role != CourseRole.TEACHER.value]
    roles = {row.role for row in memberships}
    if len(roles) > 1:
        raise DomainError(
            403,
            "AMBIGUOUS_COURSE_ROLE",
            "The LMS projected more than one active role for this course",
        )
    expected = str(role.value if isinstance(role, CourseRole) else role) if role else None
    membership = next((row for row in memberships if not expected or row.role == expected), None)
    if membership is None:
        raise DomainError(403, "COURSE_MEMBERSHIP_REQUIRED", "Active course membership is required")
    principal = await db.get(ExternalPrincipal, principal_id)
    course = await db.get(Course, course_id)
    if principal is None or course is None:
        raise DomainError(404, "COURSE_NOT_FOUND", "Course was not found")
    if not principal.active or principal.connection_id != course.connection_id:
        raise DomainError(403, "COURSE_MEMBERSHIP_REQUIRED", "Active course membership is required")
    groups = frozenset(
        (
            await db.scalars(
                select(CourseGroup.external_id)
                .join(
                    CourseMembershipGroup,
                    CourseMembershipGroup.coursegroup_id == CourseGroup.id,
                )
                .where(
                    CourseMembershipGroup.coursemembership_id == membership.id,
                    CourseGroup.active.is_(True),
                )
            )
        ).all()
    )
    return MembershipContext(membership, principal, course, groups)


def _matching_availability_rules(
    rules: list[AvailabilityRule],
    membership: MembershipContext,
) -> list[AvailabilityRule]:
    matches: list[tuple[int, AvailabilityRule]] = []
    for rule in rules:
        specificity = -1
        if rule.target_type == AvailabilityTarget.PRINCIPAL.value and rule.target_external_id in {
            membership.principal.external_subject,
            str(membership.principal.id),
        }:
            specificity = 3
        elif (
            rule.target_type == AvailabilityTarget.GROUP.value
            and rule.target_external_id in membership.group_external_ids
        ):
            specificity = 2
        elif rule.target_type == AvailabilityTarget.COURSE.value and rule.target_external_id in {
            "",
            membership.course.external_id,
            str(membership.course.id),
        }:
            specificity = 1
        if specificity >= 0:
            matches.append((specificity, rule))
    if not matches:
        return []
    strongest = max(item[0] for item in matches)
    return [rule for specificity, rule in matches if specificity == strongest]


async def effective_assessment_policy(
    db: AsyncSession,
    assessment: Assessment,
    membership: MembershipContext,
) -> EffectiveAssessmentPolicy:
    """Resolve the strongest local publication target and display limits.

    Imported Moodle work is assigned only through group rules. Individual
    Moodle dates, limits and overrides are deliberately not copied here: the
    student's live Moodle form is authoritative when an attempt is opened.
    """

    rules = list(
        (
            await db.scalars(
                select(AvailabilityRule).where(AvailabilityRule.assessment_id == assessment.id)
            )
        ).all()
    )
    policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    managed_by_moodle = policy.get("moodle_metadata_read_only") is True
    if managed_by_moodle:
        # Ignore PRINCIPAL/COURSE rules left by an older deployment. A teacher
        # now opens imported work to groups, while Moodle itself decides
        # whether each member can start or continue at that moment.
        rules = [rule for rule in rules if rule.target_type == AvailabilityTarget.GROUP.value]
    if not rules:
        if managed_by_moodle:
            raise DomainError(
                403,
                "ASSESSMENT_NOT_ASSIGNED",
                "Imported Moodle assessment must be explicitly assigned",
            )
        return EffectiveAssessmentPolicy(
            opens_at=assessment.opens_at,
            closes_at=assessment.closes_at,
            duration_seconds=assessment.duration_seconds,
            attempt_limit=assessment.attempt_limit,
        )

    selected = _matching_availability_rules(rules, membership)
    if not selected:
        raise DomainError(
            403, "ASSESSMENT_NOT_ASSIGNED", "Assessment is not assigned to this student"
        )
    if not all(rule.allowed for rule in selected):
        raise DomainError(
            403, "ASSESSMENT_NOT_AVAILABLE", "Assessment is unavailable for this student"
        )

    open_values = [
        value
        for rule in selected
        if (value := rule.opens_at if rule.opens_at is not None else assessment.opens_at)
        is not None
    ]
    close_values = [
        value
        for rule in selected
        if (value := rule.closes_at if rule.closes_at is not None else assessment.closes_at)
        is not None
    ]
    explicit_attempts = [rule.attempt_limit for rule in selected if rule.attempt_limit is not None]
    explicit_durations = [
        rule.duration_seconds for rule in selected if rule.duration_seconds is not None
    ]
    if explicit_attempts:
        positive_attempts = [value for value in explicit_attempts if value > 0]
        attempt_limit = min(positive_attempts) if positive_attempts else None
    else:
        attempt_limit = assessment.attempt_limit
    return EffectiveAssessmentPolicy(
        opens_at=max(open_values, key=_as_utc) if open_values else None,
        closes_at=min(close_values, key=_as_utc) if close_values else None,
        duration_seconds=(
            min(explicit_durations) if explicit_durations else assessment.duration_seconds
        ),
        attempt_limit=attempt_limit,
    )


async def ensure_assessment_available(
    db: AsyncSession,
    assessment: Assessment,
    membership: MembershipContext,
) -> EffectiveAssessmentPolicy:
    now = utcnow()
    if assessment.status != "PUBLISHED":
        raise DomainError(409, "ASSESSMENT_NOT_PUBLISHED", "Assessment is not published")
    policy = assessment.policy if isinstance(assessment.policy, dict) else {}
    effective = await effective_assessment_policy(db, assessment, membership)
    if policy.get("moodle_metadata_read_only") is True:
        # Synced dates are useful to display, but group members can have Moodle
        # overrides which extend them. The mandatory live LMS preparation at
        # attempt start is the only safe source of individual availability.
        return effective
    if effective.opens_at and now < _as_utc(effective.opens_at):
        raise DomainError(409, "ASSESSMENT_NOT_OPEN", "Assessment is not open yet")
    if effective.closes_at and now >= _as_utc(effective.closes_at):
        raise DomainError(409, "ASSESSMENT_CLOSED", "Assessment is closed")
    return effective


def require_review_required(assessment: Assessment) -> None:
    if not assessment.review_required:
        raise DomainError(
            409,
            "REVIEW_NOT_REQUIRED",
            "This assessment does not require a teacher review",
        )


def require_decision_support(assessment: Assessment) -> None:
    if not assessment.decision_support_enabled:
        raise DomainError(
            403,
            "DECISION_SUPPORT_DISABLED",
            "Decision support is disabled for this assessment",
        )


def _assignment_rule_matches(rule: dict, membership: MembershipContext) -> bool:
    if not rule:
        return True
    principals = {
        str(value) for value in rule.get("principal_external_ids", rule.get("principals", []))
    }
    groups = {str(value) for value in rule.get("group_external_ids", rule.get("groups", []))}
    if principals and membership.principal.external_subject not in principals:
        return False
    if groups and groups.isdisjoint(membership.group_external_ids):
        return False
    return bool(rule.get("enabled", True))


async def resolve_assigned_task_version(
    db: AsyncSession,
    assessment: Assessment,
    membership: MembershipContext,
) -> TaskVersion:
    rows = list(
        (
            await db.execute(
                select(AssessmentItem, TaskVersion)
                .join(TaskVersion, TaskVersion.id == AssessmentItem.task_version_id)
                .where(
                    AssessmentItem.assessment_id == assessment.id,
                    TaskVersion.status == TaskVersionStatus.PUBLISHED.value,
                )
                .order_by(AssessmentItem.position, AssessmentItem.created_at)
            )
        ).all()
    )
    candidates = [
        (item, version)
        for item, version in rows
        if _assignment_rule_matches(item.assignment_rule, membership)
    ]
    if not candidates:
        raise DomainError(
            409, "NO_ASSIGNED_TASK", "Assessment has no published task assigned to this student"
        )
    digest = hashlib.sha256(f"{assessment.id}:{membership.principal.id}".encode()).digest()
    return candidates[int.from_bytes(digest[:8], "big") % len(candidates)][1]
