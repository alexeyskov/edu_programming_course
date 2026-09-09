from __future__ import annotations

import base64
import binascii
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
PositiveId = Annotated[str, StringConstraints(pattern=r"^[1-9][0-9]{0,19}$")]
AnswerTransport = Literal[
    "ESSAY_ONLINE_TEXT",
    "ESSAY_ATTACHMENT",
    "ASSIGN_ONLINE_TEXT",
    "ASSIGN_FILE",
]
QuizGradingMethod = Literal["HIGHEST", "AVERAGE", "FIRST", "LAST"]
ManagedSubmissionFilename = Literal[
    "solution.c",
    "solution.cpp",
    "main.c",
    "main.cpp",
    "submission.zip",
]
_ARTIFACT_BASE64_MAX_CHARS = ((4 * 1024 * 1024 + 2) // 3) * 4
_SAFE_ARTIFACT_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BrowserCookie(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    value: Annotated[str, StringConstraints(max_length=16_384)]
    domain: Annotated[str, StringConstraints(min_length=1, max_length=253)]
    path: Annotated[str, StringConstraints(min_length=1, max_length=2_048)] = "/"
    expires: float = -1
    httpOnly: bool = False
    secure: bool = True
    sameSite: Literal["Strict", "Lax", "None"] = "Lax"


class LocalStorageEntry(StrictModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=1_024)]
    value: Annotated[str, StringConstraints(max_length=32_768)]


class BrowserOriginState(StrictModel):
    origin: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    localStorage: list[LocalStorageEntry] = Field(default_factory=list, max_length=128)


class BrowserStorageState(StrictModel):
    cookies: list[BrowserCookie] = Field(default_factory=list, max_length=256)
    origins: list[BrowserOriginState] = Field(default_factory=list, max_length=4)


class CourseMembership(StrictModel):
    external_id: PositiveId
    title: ShortText
    short_name: Annotated[str, StringConstraints(max_length=120)] = ""
    role: Literal["STUDENT", "TEACHER", "UNKNOWN"] = "UNKNOWN"


class MoodleIdentity(StrictModel):
    external_subject: PositiveId
    display_name: ShortText
    email: Annotated[str, StringConstraints(max_length=254)] = ""
    locale: Annotated[str, StringConstraints(max_length=35)] = ""
    courses: list[CourseMembership] = Field(default_factory=list, max_length=512)


class LoginRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    username: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=320)
    ]
    password: SecretStr
    allowed_course_ids: list[PositiveId] = Field(default_factory=list, max_length=512)

    @field_validator("password")
    @classmethod
    def password_length(cls, value: SecretStr) -> SecretStr:
        length = len(value.get_secret_value())
        if not 1 <= length <= 4_096:
            raise ValueError("password length is outside the supported range")
        return value

    @field_validator("allowed_course_ids")
    @classmethod
    def unique_allowed_course_ids(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("allowed_course_ids must not contain duplicates")
        return value


class LoginResponse(StrictModel):
    identity: MoodleIdentity
    storage_state: BrowserStorageState


class CourseDiscoverRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    external_id: PositiveId
    actor_external_subject: PositiveId
    storage_state: BrowserStorageState
    # Signed internal hint: a user-triggered refresh must not wait behind the
    # historical-import queue. Background workers leave the default disabled.
    interactive: bool = False


class GroupSnapshot(StrictModel):
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    name: ShortText


class MemberSnapshot(StrictModel):
    user_id: PositiveId
    display_name: ShortText
    email: Annotated[str, StringConstraints(max_length=254)] = ""
    suspended: bool = False
    role: Literal["STUDENT", "TEACHER"]
    roles: list[Literal["STUDENT", "TEACHER"]] = Field(min_length=1, max_length=2)
    groups: list[GroupSnapshot] = Field(default_factory=list, max_length=128)


class MembershipSnapshot(StrictModel):
    complete: bool
    members: list[MemberSnapshot] = Field(default_factory=list, max_length=10_000)


class ActivityUserOverride(StrictModel):
    override_id: int = Field(gt=0)
    user_id: PositiveId
    display_name: ShortText
    opens_at: int = Field(default=0, ge=0)
    due_at: int = Field(default=0, ge=0)
    cutoff_at: int = Field(default=0, ge=0)
    opens_at_overridden: bool = False
    due_at_overridden: bool = False
    cutoff_at_overridden: bool = False
    duration_seconds: int | None = Field(default=None, gt=0, le=31_536_000)
    duration_overridden: bool = False
    attempt_limit: int | None = Field(default=None, gt=0, le=100)
    attempt_limit_unlimited: bool = False
    attempt_limit_overridden: bool = False
    confirmed: bool = False


class ActivitySnapshot(StrictModel):
    cmid: int = Field(gt=0)
    instance_id: int = Field(default=0, ge=0)
    module: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]
    name: ShortText
    visible: bool = True
    uservisible: bool = True
    url: Annotated[str, StringConstraints(max_length=2_000)] = ""
    opens_at: int = Field(default=0, ge=0)
    due_at: int = Field(default=0, ge=0)
    cutoff_at: int = Field(default=0, ge=0)
    description: Annotated[str, StringConstraints(max_length=50_000)] = ""
    grade_max: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    duration_seconds: int | None = Field(default=None, gt=0, le=31_536_000)
    attempt_limit: int | None = Field(default=None, gt=0, le=100)
    attempt_limit_unlimited: bool = False
    quiz_grading_method: QuizGradingMethod | None = None
    quiz_grading_method_confirmed: bool = False
    answer_transport: AnswerTransport | None = None
    available_answer_transports: list[AnswerTransport] = Field(
        default_factory=list,
        max_length=4,
    )
    submission_drafts: bool | None = None
    requires_submission_statement: bool | None = None
    max_submission_files: int | None = Field(default=None, gt=0, le=128)
    max_submission_bytes: int | None = Field(default=None, gt=0, le=4 * 1024 * 1024 * 1024)
    max_submission_bytes_inherited: bool = False
    accepted_file_types: Annotated[str, StringConstraints(max_length=2_000)] = ""
    file_types_confirmed: bool = False
    team_submission: bool | None = None
    question_count: int | None = Field(default=None, ge=0, le=10_000)
    essay_question_count: int | None = Field(default=None, ge=0, le=10_000)
    random_question_count: int = Field(default=0, ge=0, le=10_000)
    random_essay_confirmed: bool = False
    statement_deferred: bool = False
    import_supported: bool = False
    title_confirmed: bool = False
    settings_confirmed: bool = False
    statement_confirmed: bool = False
    schedule_confirmed: bool = False
    duration_confirmed: bool = False
    grade_confirmed: bool = False
    attempt_policy_confirmed: bool = False
    user_overrides: list[ActivityUserOverride] = Field(default_factory=list, max_length=256)
    user_overrides_confirmed: bool = False


class SectionSnapshot(StrictModel):
    external_id: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    title: ShortText
    position: int = Field(ge=0)
    visible: bool = True
    activities: list[ActivitySnapshot] = Field(default_factory=list, max_length=4_096)


class CoursePreview(StrictModel):
    external_id: PositiveId
    title: ShortText
    short_name: Annotated[str, StringConstraints(max_length=120)] = ""
    external_revision: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    membership_revision: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    starts_at_epoch: int = Field(default=0, ge=0)
    ends_at_epoch: int = Field(default=0, ge=0)
    sections: list[SectionSnapshot] = Field(default_factory=list, max_length=512)
    groups: list[GroupSnapshot] = Field(default_factory=list, max_length=10_000)
    membership_snapshot: MembershipSnapshot


class DiscoveryCapabilities(StrictModel):
    roster: bool
    groups: bool
    grades: bool
    comments: bool
    checkpoints: bool = False
    task_bank_mirror: bool = False
    native_question_bank_write: bool = False


class CourseDiscovery(StrictModel):
    external_id: PositiveId
    actor_role: Literal["TEACHER"]
    preview: CoursePreview
    capabilities: DiscoveryCapabilities


class CourseDiscoverResponse(StrictModel):
    discovery: CourseDiscovery
    storage_state: BrowserStorageState


class GradePayload(StrictModel):
    module: Literal["assign", "quiz"] = "assign"
    course_id: PositiveId
    cmid: int = Field(gt=0)
    user_id: PositiveId
    grade: float = Field(ge=0, le=1_000_000, allow_inf_nan=False)
    grade_scale_max: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    quiz_overall_grade_max: float | None = Field(
        default=None, gt=0, le=1_000_000, allow_inf_nan=False
    )
    comment: Annotated[str, StringConstraints(max_length=20_000)] = ""
    attempt_number: int | None = Field(default=None, ge=0, le=1_000_000)
    attempt_id: PositiveId | None = None
    question_slot: int | None = Field(default=None, gt=0, le=10_000)

    @model_validator(mode="after")
    def target_is_unambiguous(self) -> GradePayload:
        if self.module == "quiz":
            if self.attempt_id is None or self.question_slot is None:
                raise ValueError("quiz grading requires an attempt and question slot")
            if self.attempt_number is not None:
                raise ValueError("quiz grading cannot use an assignment attempt number")
            if (
                self.grade_scale_max is None
                or self.quiz_overall_grade_max is None
                or self.grade > self.grade_scale_max
            ):
                raise ValueError("quiz grading requires a valid local grade scale")
        elif self.attempt_id is not None or self.question_slot is not None:
            raise ValueError("assignment grading cannot use quiz identifiers")
        elif self.grade_scale_max is not None or self.quiz_overall_grade_max is not None:
            raise ValueError("assignment grading cannot use a quiz grade scale")
        return self


class GradeRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    storage_state: BrowserStorageState
    payload: GradePayload
    idempotency_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{8,200}$")]


class GradeResponse(StrictModel):
    status: Literal["DELIVERED"]
    receipt: dict[str, str | int | float | bool | None]
    storage_state: BrowserStorageState


class QuizArtifact(StrictModel):
    filename: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    content_base64: Annotated[str, StringConstraints(max_length=_ARTIFACT_BASE64_MAX_CHARS)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        if (
            not _SAFE_ARTIFACT_FILENAME.fullmatch(value)
            or value in {".", ".."}
            or value.endswith(".")
        ):
            raise ValueError("artifact filename is unsafe")
        return value

    @field_validator("content_base64")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("artifact content is not canonical base64") from exc
        if len(value) % 4 or base64.b64encode(decoded).decode("ascii") != value:
            raise ValueError("artifact content is not canonical base64")
        return value


class QuizEssaySyncRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    course_id: PositiveId
    cmid: int = Field(gt=0, le=2**63 - 1)
    answer_transport: Literal["ESSAY_ONLINE_TEXT", "ESSAY_ATTACHMENT"] = "ESSAY_ATTACHMENT"
    artifact: QuizArtifact
    previous_managed_filename: ManagedSubmissionFilename | None = None
    previous_managed_sha256: (
        Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None
    ) = None
    expected_attempt_id: PositiveId | None = None
    expected_question_slot: PositiveId | None = None
    finalize: bool = False
    idempotency_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{8,200}$")]
    storage_state: BrowserStorageState

    @model_validator(mode="after")
    def managed_receipt_is_complete(self) -> QuizEssaySyncRequest:
        if (self.previous_managed_filename is None) != (self.previous_managed_sha256 is None):
            raise ValueError("previous managed artifact receipt is incomplete")
        if (self.expected_attempt_id is None) != (self.expected_question_slot is None):
            raise ValueError("expected Moodle Quiz attempt identity is incomplete")
        return self


class QuizEssayPrepareRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    course_id: PositiveId
    cmid: int = Field(gt=0, le=2**63 - 1)
    expected_attempt_id: PositiveId | None = None
    expected_question_slot: PositiveId | None = None
    storage_state: BrowserStorageState

    @model_validator(mode="after")
    def expected_identity_is_complete(self) -> QuizEssayPrepareRequest:
        if (self.expected_attempt_id is None) != (self.expected_question_slot is None):
            raise ValueError("expected Moodle Quiz attempt identity is incomplete")
        return self


class QuizEssayPreparation(StrictModel):
    course_id: PositiveId
    cmid: int = Field(gt=0)
    attempt_id: PositiveId
    question_slot: PositiveId
    question_text: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=50_000),
    ]
    answer_transport: Literal["ESSAY_ONLINE_TEXT", "ESSAY_ATTACHMENT"]
    available_answer_transports: list[Literal["ESSAY_ONLINE_TEXT", "ESSAY_ATTACHMENT"]] = Field(
        min_length=1, max_length=2
    )
    remaining_seconds: int | None = Field(default=None, ge=0, le=315_360_000)


class QuizEssayPrepareResponse(StrictModel):
    status: Literal["READY"]
    preparation: QuizEssayPreparation
    storage_state: BrowserStorageState


class QuizEssayReceipt(StrictModel):
    course_id: PositiveId
    cmid: int = Field(gt=0)
    attempt_id: PositiveId
    question_slot: PositiveId
    filename: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    size_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    idempotency_key: Annotated[str, StringConstraints(min_length=8, max_length=200)]


class QuizEssaySyncResponse(StrictModel):
    status: Literal["DRAFT_SAVED", "FINALIZED"]
    receipt: QuizEssayReceipt
    storage_state: BrowserStorageState


class AssignmentSubmissionPrepareRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    course_id: PositiveId
    cmid: int = Field(gt=0, le=2**63 - 1)
    storage_state: BrowserStorageState


class AssignmentSubmissionPreparation(StrictModel):
    course_id: PositiveId
    cmid: int = Field(gt=0, le=2**63 - 1)
    answer_transport: Literal["ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"]
    available_answer_transports: list[Literal["ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"]] = Field(
        min_length=1, max_length=2
    )


class AssignmentSubmissionPrepareResponse(StrictModel):
    status: Literal["READY"]
    preparation: AssignmentSubmissionPreparation
    storage_state: BrowserStorageState


class AssignmentSubmissionSyncRequest(StrictModel):
    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    course_id: PositiveId
    cmid: int = Field(gt=0, le=2**63 - 1)
    answer_transport: Literal["ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"] = "ASSIGN_FILE"
    artifact: QuizArtifact
    previous_managed_filename: ManagedSubmissionFilename | None = None
    previous_managed_sha256: (
        Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")] | None
    ) = None
    finalize: bool = False
    requires_submission_statement: bool = False
    submission_drafts: bool | None = None
    max_submission_bytes_inherited: bool = False
    idempotency_key: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._:-]{8,200}$")]
    storage_state: BrowserStorageState

    @model_validator(mode="after")
    def managed_receipt_is_complete(self) -> AssignmentSubmissionSyncRequest:
        if (self.previous_managed_filename is None) != (self.previous_managed_sha256 is None):
            raise ValueError("previous managed artifact receipt is incomplete")
        return self


class AssignmentSubmissionReceipt(StrictModel):
    course_id: PositiveId
    cmid: int = Field(gt=0)
    filename: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    sha256: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    size_bytes: int = Field(ge=0, le=4 * 1024 * 1024)
    idempotency_key: Annotated[str, StringConstraints(min_length=8, max_length=200)]


class AssignmentSubmissionSyncResponse(StrictModel):
    status: Literal["DRAFT_SAVED", "FINALIZED"]
    receipt: AssignmentSubmissionReceipt
    storage_state: BrowserStorageState


class HistoricalActivityRef(StrictModel):
    """One Moodle activity whose teacher-visible submissions are read."""

    module: Literal["assign", "quiz"]
    cmid: int = Field(gt=0, le=2**63 - 1)


class HistoricalSubmissionsRequest(StrictModel):
    """Bounded cursor request for historical Moodle submissions.

    ``cursor`` is opaque to the core service.  The browser connector currently
    encodes it as ``<Moodle report page>:<row offset>`` so a large Moodle page
    can be consumed in small chunks without silently dropping rows.
    """

    schema_version: Literal["1.0"]
    base_url: Annotated[str, StringConstraints(min_length=8, max_length=2_048)]
    course_id: PositiveId
    actor_external_subject: PositiveId
    activity: HistoricalActivityRef
    cursor: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,6}:[0-9]{1,6}$")] = "0:0"
    limit: int = Field(default=10, ge=1, le=25)
    # A lightweight first pass imports only active markers and finished
    # attempts which still require manual grading.  The exhaustive historical
    # crawl is queued separately after this pass completes.
    priority_only: bool = False
    storage_state: BrowserStorageState


class HistoricalArtifact(StrictModel):
    """A bounded same-origin Moodle attachment.

    Files that exceed the response budget are still represented, but have
    ``downloaded=false`` and contain no content.  This avoids treating a
    truncated source file as a valid student answer.
    """

    external_id: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
    filename: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    mime_type: Annotated[str, StringConstraints(max_length=255)] = ""
    size_bytes: int = Field(default=0, ge=0, le=4 * 1024 * 1024)
    sha256: Annotated[str, StringConstraints(pattern=r"^(?:[a-f0-9]{64})?$")] = ""
    content_base64: Annotated[str, StringConstraints(max_length=_ARTIFACT_BASE64_MAX_CHARS)] = ""
    downloaded: bool = True
    omission_reason: Annotated[str, StringConstraints(max_length=255)] = ""

    @field_validator("filename")
    @classmethod
    def bounded_filename(cls, value: str) -> str:
        if (
            value in {".", ".."}
            or any(character in value for character in ("/", "\\", "\0"))
            or any(ord(character) < 32 for character in value)
        ):
            raise ValueError("artifact filename is unsafe")
        return value

    @model_validator(mode="after")
    def content_contract(self) -> HistoricalArtifact:
        if not self.downloaded:
            if self.content_base64 or self.sha256 or not self.omission_reason:
                raise ValueError("omitted artifact has an invalid representation")
            return self
        try:
            decoded = base64.b64decode(self.content_base64, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("artifact content is not canonical base64") from exc
        if (
            len(self.content_base64) % 4
            or base64.b64encode(decoded).decode("ascii") != self.content_base64
            or len(decoded) != self.size_bytes
        ):
            raise ValueError("artifact content is not canonical base64")
        import hashlib

        if hashlib.sha256(decoded).hexdigest() != self.sha256:
            raise ValueError("artifact digest does not match content")
        return self


class HistoricalResponsePart(StrictModel):
    response_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    question_text: Annotated[str, StringConstraints(max_length=50_000)] = ""
    answer_text: Annotated[str, StringConstraints(max_length=1_000_000)] = ""
    answer_complete: bool = True
    answer_omission_reason: Annotated[str, StringConstraints(max_length=255)] = ""
    grade: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    grade_max: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    comment: Annotated[str, StringConstraints(max_length=20_000)] = ""
    reviewer_name: Annotated[str, StringConstraints(max_length=255)] = ""
    artifacts: list[HistoricalArtifact] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def answer_contract(self) -> HistoricalResponsePart:
        if self.answer_complete and self.answer_omission_reason:
            raise ValueError("complete answer must not have an omission reason")
        if not self.answer_complete and not self.answer_omission_reason:
            raise ValueError("omitted answer must explain why it is unavailable")
        return self


class HistoricalSubmission(StrictModel):
    external_id: Annotated[
        str,
        StringConstraints(pattern=r"^(?:quiz|assign):[1-9][0-9]{0,19}:[A-Za-z0-9:_-]{1,160}$"),
    ]
    module: Literal["assign", "quiz"]
    cmid: int = Field(gt=0, le=2**63 - 1)
    attempt_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9:_-]{1,160}$")]
    user_id: PositiveId
    display_name: ShortText
    state: Literal["IN_PROGRESS", "SUBMITTED", "GRADED", "UNKNOWN"]
    submitted_at_epoch: int = Field(default=0, ge=0)
    grade: float | None = Field(default=None, ge=0, le=1_000_000, allow_inf_nan=False)
    grade_max: float | None = Field(default=None, gt=0, le=1_000_000, allow_inf_nan=False)
    comment: Annotated[str, StringConstraints(max_length=20_000)] = ""
    # True only when every question page of this attempt was collected.  Core
    # importers may retire stale per-question children only under this flag.
    responses_complete: bool = True
    responses: list[HistoricalResponsePart] = Field(default_factory=list, max_length=32)
    external_revision: Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]


class HistoricalSubmissionsResponse(StrictModel):
    course_id: PositiveId
    activity: HistoricalActivityRef
    items: list[HistoricalSubmission] = Field(default_factory=list, max_length=25)
    next_cursor: Annotated[str, StringConstraints(pattern=r"^[0-9]{1,6}:[0-9]{1,6}$")] | None = (
        None
    )
    complete: bool
    warnings: list[Annotated[str, StringConstraints(max_length=255)]] = Field(
        default_factory=list, max_length=32
    )
    storage_state: BrowserStorageState


class HealthResponse(StrictModel):
    status: Literal["ok", "unavailable"]
    version: str
    browser: Literal["connected", "disconnected", "not_started"]
    ready: bool
