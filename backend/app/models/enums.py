from enum import StrEnum


class LMSProvider(StrEnum):
    MOODLE = "MOODLE"
    MOCK = "MOCK"


class CourseRole(StrEnum):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"


class CourseImportState(StrEnum):
    DISCOVERED = "DISCOVERED"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class TaskScope(StrEnum):
    SYSTEM = "SYSTEM"
    COURSE = "COURSE"


class TaskVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    REVIEW = "REVIEW"
    PUBLISHED = "PUBLISHED"
    ARCHIVED = "ARCHIVED"


class AssessmentType(StrEnum):
    LAB = "LAB"
    INDEPENDENT = "INDEPENDENT"
    CONTROL = "CONTROL"
    EXAM = "EXAM"


class AssessmentStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    CLOSED = "CLOSED"


class AvailabilityTarget(StrEnum):
    COURSE = "COURSE"
    GROUP = "GROUP"
    PRINCIPAL = "PRINCIPAL"


class AttemptState(StrEnum):
    ACTIVE = "ACTIVE"
    FINISHING = "FINISHING"
    SUBMITTED = "SUBMITTED"
    AUTO_SUBMITTED = "AUTO_SUBMITTED"
    LOCKED = "LOCKED"
    VOID = "VOID"


class AnalysisState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class AuthorshipAnalysisState(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INVALID = "INVALID"


class PlagiarismCaseState(StrEnum):
    SUSPECTED = "SUSPECTED"
    REVIEWING = "REVIEWING"
    CONFIRMED = "CONFIRMED"
    DISMISSED = "DISMISSED"
    INCONCLUSIVE = "INCONCLUSIVE"


class RunOrigin(StrEnum):
    STUDENT_ATTEMPT = "STUDENT_ATTEMPT"
    IMMUTABLE_SUBMISSION = "IMMUTABLE_SUBMISSION"
    TEACHER_EXPERIMENT = "TEACHER_EXPERIMENT"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ReviewClaimState(StrEnum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"
    OVERRIDDEN = "OVERRIDDEN"


class ChatMode(StrEnum):
    STUDENT = "STUDENT"
    TEACHER = "TEACHER"


class SyncOutboxState(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    RETRY = "RETRY"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
