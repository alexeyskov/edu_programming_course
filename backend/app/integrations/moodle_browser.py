from __future__ import annotations

import base64
import hashlib
import hmac
import math
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from app.core.config import Settings

from ._http import canonical_json, request_json_limited, secret_value, sha256_hex
from .errors import (
    IntegrationAssessmentUnavailable,
    IntegrationAttemptFinalized,
    IntegrationBusy,
    IntegrationConfigurationError,
    IntegrationProtocolError,
    IntegrationUnavailable,
)
from .moodle import CourseDiscovery
from .moodle_standard import MoodleAuthenticationError, MoodleCourseMembership, TokenIdentity

_ENDPOINTS = {
    "login": "/internal/v1/moodle/login",
    "discover_course": "/internal/v1/moodle/course/discover",
    "push_grade": "/internal/v1/moodle/assignment/grade",
    "sync_assignment_submission": "/internal/v1/moodle/assignment/submission/sync",
    "prepare_assignment_submission": "/internal/v1/moodle/assignment/submission/prepare",
    "prepare_quiz_essay": "/internal/v1/moodle/quiz/essay/prepare",
    "sync_quiz_essay": "/internal/v1/moodle/quiz/essay/sync",
    "sync_quiz_answers": "/internal/v1/moodle/quiz/answers/sync",
    "discover_historical_submissions": "/internal/v1/moodle/activity/submissions/discover",
}
_MAX_COURSES = 512
_MAX_SECTIONS = 2_000
_MAX_ACTIVITIES = 5_000
_MAX_PARTICIPANTS = 20_000
_MAX_GROUPS_PER_PARTICIPANT = 128
_MAX_COOKIES = 256
_MAX_ORIGINS = 4
_MAX_LOCAL_STORAGE_ENTRIES = 128
_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{8,200}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9._-]{16,128}$")
_ARTIFACT_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_MAX_QUIZ_ARTIFACT_BYTES = 4 * 1024 * 1024
_MAX_QUIZ_RECEIPT_BYTES = 4 * 1024
_MANAGED_SUBMISSION_FILENAMES = frozenset(
    {"solution.c", "solution.cpp", "main.c", "main.cpp", "submission.zip"}
)


@dataclass(frozen=True, slots=True)
class MoodleBrowserLoginResult:
    identity: TokenIdentity
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserDiscoveryResult:
    discovery: CourseDiscovery
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserGradeResult:
    status: str
    receipt: dict[str, Any]
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizEssayArtifact:
    """One in-memory source artifact for a Moodle essay response.

    The public API deliberately accepts raw bytes rather than caller-provided
    base64 or a digest.  This makes the adapter responsible for canonical
    encoding and prevents a digest/content mismatch from crossing the trust
    boundary to the browser service.
    """

    filename: str
    content: bytes


MoodleBrowserAssignmentArtifact = MoodleBrowserQuizEssayArtifact


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizEssayReceipt:
    course_id: str
    cmid: int
    attempt_id: str
    question_slot: str
    filename: str
    sha256: str
    size_bytes: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizEssayResult:
    status: Literal["DRAFT_SAVED", "FINALIZED"]
    receipt: MoodleBrowserQuizEssayReceipt
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizAnswer:
    question_slot: str
    artifact: MoodleBrowserQuizEssayArtifact
    answer_transport: Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"]
    previous_managed_filename: str | None = None
    previous_managed_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizAnswersResult:
    status: Literal["DRAFT_SAVED", "FINALIZED"]
    receipts: tuple[MoodleBrowserQuizEssayReceipt, ...]
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizQuestionPreparation:
    question_slot: str
    question_text: str
    answer_transport: Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"]
    available_answer_transports: tuple[Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"], ...]
    page: int = 0
    question_max_mark: float | None = None


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizEssayPreparation:
    course_id: str
    cmid: int
    attempt_id: str
    question_slot: str
    question_text: str
    answer_transport: Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"]
    available_answer_transports: tuple[Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"], ...]
    remaining_seconds: int | None = None
    questions: tuple[MoodleBrowserQuizQuestionPreparation, ...] = ()


@dataclass(frozen=True, slots=True)
class MoodleBrowserQuizEssayPrepareResult:
    preparation: MoodleBrowserQuizEssayPreparation
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserAssignmentReceipt:
    course_id: str
    cmid: int
    filename: str
    sha256: str
    size_bytes: int
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class MoodleBrowserAssignmentResult:
    status: Literal["DRAFT_SAVED", "FINALIZED"]
    receipt: MoodleBrowserAssignmentReceipt
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserAssignmentPreparation:
    course_id: str
    cmid: int
    answer_transport: Literal["ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"]
    available_answer_transports: tuple[Literal["ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"], ...]


@dataclass(frozen=True, slots=True)
class MoodleBrowserAssignmentPrepareResult:
    preparation: MoodleBrowserAssignmentPreparation
    storage_state: dict[str, Any]


@dataclass(frozen=True, slots=True)
class MoodleBrowserHistoricalSubmissionsResult:
    course_id: str
    module: Literal["quiz", "assign"]
    cmid: int
    items: tuple[dict[str, Any], ...]
    next_cursor: str | None
    complete: bool
    warnings: tuple[str, ...]
    storage_state: dict[str, Any]


class MoodleBrowserClient:
    """Authenticated client for the fixed, internal Playwright Moodle service.

    The browser service owns all selectors and navigation.  This adapter never
    accepts a service URL or a Moodle URL from an operation payload: both origins
    come from administrator-controlled configuration.  Credentials and browser
    state are sent only in size-bounded, HMAC-authenticated JSON requests.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        base_url: str | None = None,
        storage_state: Mapping[str, Any] | None = None,
        service_url: str | None = None,
        shared_secret: str | None = None,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.base_url = self._normalise_moodle_url(base_url or settings.moodle_base_url)
        self.service_url = self._normalise_service_url(
            service_url or str(getattr(settings, "moodle_browser_service_url", ""))
        )
        configured_secret = getattr(settings, "moodle_browser_shared_secret", "")
        self.shared_secret = (
            shared_secret if shared_secret is not None else secret_value(configured_secret)
        ).strip()
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        self.timeout_seconds = float(
            getattr(
                settings,
                "moodle_browser_http_timeout_seconds",
                max(30.0, float(getattr(settings, "moodle_http_timeout_seconds", 15)) * 5),
            )
        )
        self.request_limit = int(
            getattr(
                settings,
                "moodle_browser_request_body_max_bytes",
                getattr(settings, "moodle_browser_max_request_bytes", 1024 * 1024),
            )
        )
        self.response_limit = int(
            getattr(settings, "moodle_browser_max_response_bytes", 4 * 1024 * 1024)
        )
        self.storage_state_limit = int(
            getattr(settings, "moodle_browser_storage_state_max_bytes", 256 * 1024)
        )
        if self.request_limit < 1024 or self.response_limit < 1024:
            raise IntegrationConfigurationError("Moodle browser transport size limits are invalid")
        if not 16 * 1024 <= self.storage_state_limit <= min(self.request_limit, 1024 * 1024):
            raise IntegrationConfigurationError("Moodle browser state size limit is invalid")
        if bool(getattr(client, "follow_redirects", False)):
            raise IntegrationConfigurationError(
                "Moodle browser HTTP client must not follow redirects"
            )
        self._storage_state = (
            self._normalise_storage_state(storage_state) if storage_state is not None else None
        )

    @property
    def storage_state(self) -> dict[str, Any] | None:
        """Return a detached copy suitable for encrypted persistence."""

        if self._storage_state is None:
            return None
        return self._normalise_storage_state(self._storage_state)

    def recognize_course_url(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
            expected = urlsplit(self.base_url)
        except (TypeError, ValueError) as exc:
            raise IntegrationProtocolError("Course URL is invalid") from exc
        if parsed.username or parsed.password or parsed.fragment:
            raise IntegrationProtocolError("Course URL contains unsupported components")
        if self._origin(parsed) != self._origin(expected):
            raise IntegrationProtocolError("Course URL origin is not configured")
        course_path = f"{expected.path.rstrip('/')}/course/view.php" or "/course/view.php"
        if parsed.path.rstrip("/") != course_path:
            raise IntegrationProtocolError("Only a Moodle course URL is accepted")
        values = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if len(values) != 1:
            raise IntegrationProtocolError("Moodle course id is missing")
        return self._positive_id(values[0], "Moodle course id")

    async def authenticate(
        self,
        username: str,
        password: str,
        *,
        allowed_course_ids: tuple[str, ...] = (),
    ) -> MoodleBrowserLoginResult:
        normalized_username = self._required_text(username, 320, "Moodle username")
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise MoodleAuthenticationError("Moodle credentials were not accepted")
        if len(allowed_course_ids) > _MAX_COURSES:
            raise IntegrationProtocolError("Too many configured Moodle courses")
        normalized_course_ids = [
            self._positive_id(value, "Moodle course id") for value in allowed_course_ids
        ]
        if len(set(normalized_course_ids)) != len(normalized_course_ids):
            raise IntegrationProtocolError("Configured Moodle courses contain duplicates")
        result = await self._call(
            "login",
            {
                "schema_version": "1.0",
                "base_url": self.base_url,
                "username": normalized_username,
                "password": password,
                "allowed_course_ids": normalized_course_ids,
            },
        )
        identity = self._normalise_identity(result.get("identity"), normalized_username)
        state = self._response_storage_state(result)
        self._storage_state = state
        return MoodleBrowserLoginResult(identity=identity, storage_state=self.storage_state or {})

    async def discover_course(
        self,
        external_id: str,
        actor_external_subject: str,
        *,
        interactive: bool = False,
    ) -> MoodleBrowserDiscoveryResult:
        course_id = self._positive_id(external_id, "Moodle course id")
        actor_id = self._positive_id(actor_external_subject, "Moodle actor external subject")
        state = self._required_storage_state()
        result = await self._call(
            "discover_course",
            {
                "schema_version": "1.0",
                "base_url": self.base_url,
                "external_id": course_id,
                "actor_external_subject": actor_id,
                "storage_state": state,
                "interactive": interactive,
            },
        )
        raw_discovery = result.get("discovery")
        discovery = self._normalise_discovery(raw_discovery, course_id, actor_id)
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserDiscoveryResult(
            discovery=discovery,
            storage_state=self.storage_state or {},
        )

    async def push_grade(
        self,
        payload: Mapping[str, Any],
        idempotency_key: str,
    ) -> MoodleBrowserGradeResult:
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(
            idempotency_key
        ):
            raise IntegrationProtocolError("Invalid Moodle idempotency key")
        normalized_payload = self._normalise_grade_payload(payload)
        state = self._required_storage_state()
        result = await self._call(
            "push_grade",
            {
                "schema_version": "1.0",
                "base_url": self.base_url,
                "payload": normalized_payload,
                "idempotency_key": idempotency_key,
                "storage_state": state,
            },
        )
        status = self._required_text(result.get("status"), 32, "Moodle grade status").upper()
        if status not in {"DELIVERED", "UNCHANGED"}:
            raise IntegrationProtocolError("Moodle browser returned an invalid grade status")
        receipt = result.get("receipt", {})
        if not isinstance(receipt, dict):
            raise IntegrationProtocolError("Moodle browser grade receipt has an invalid shape")
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserGradeResult(
            status=status,
            receipt=dict(receipt),
            storage_state=self.storage_state or {},
        )

    async def sync_quiz_essay(
        self,
        course_id: str,
        cmid: int,
        artifact: MoodleBrowserQuizEssayArtifact,
        *,
        answer_transport: Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"],
        finalize: bool,
        idempotency_key: str,
        question_slot: int | None = None,
        expected_attempt_id: str | None = None,
        expected_question_slot: str | None = None,
        previous_managed_filename: str | None = None,
        previous_managed_sha256: str | None = None,
    ) -> MoodleBrowserQuizEssayResult:
        """Synchronize one C/C++ artifact through its proven Moodle Essay transport.

        The current browser service intentionally supports only quizzes with
        exactly one essay question and discovers that slot from Moodle markup.
        ``question_slot`` is reserved in the public API for future multi-essay
        support and therefore fails closed when supplied today.
        """

        normalized_course_id = self._positive_id(course_id, "Moodle course id")
        normalized_cmid = self._positive_int(cmid, "Moodle activity id")
        if answer_transport not in {"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"}:
            raise IntegrationProtocolError("Moodle Essay answer transport is invalid")
        if not isinstance(finalize, bool):
            raise IntegrationProtocolError("Moodle quiz finalize flag is invalid")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(
            idempotency_key
        ):
            raise IntegrationProtocolError("Invalid Moodle idempotency key")
        if question_slot is not None:
            raise IntegrationConfigurationError(
                "Moodle browser multi-essay question selection is not implemented"
            )
        if (expected_attempt_id is None) != (expected_question_slot is None):
            raise IntegrationProtocolError("Expected Moodle quiz attempt identity is incomplete")
        normalized_expected_attempt = (
            self._positive_id(expected_attempt_id, "Expected Moodle quiz attempt id")
            if expected_attempt_id is not None
            else None
        )
        normalized_expected_slot = (
            self._positive_id(expected_question_slot, "Expected Moodle quiz question slot")
            if expected_question_slot is not None
            else None
        )
        if (
            previous_managed_filename is not None
            and previous_managed_filename not in _MANAGED_SUBMISSION_FILENAMES
        ):
            raise IntegrationProtocolError("Moodle previous managed filename is invalid")
        if (previous_managed_filename is None) != (previous_managed_sha256 is None):
            raise IntegrationProtocolError("Moodle previous managed receipt is incomplete")
        if previous_managed_sha256 is not None and not _SHA256_RE.fullmatch(
            previous_managed_sha256
        ):
            raise IntegrationProtocolError("Moodle previous managed digest is invalid")
        filename, content = self._normalise_quiz_artifact(artifact)
        digest = hashlib.sha256(content).hexdigest()
        state = self._required_storage_state()
        request_payload: dict[str, Any] = {
            "schema_version": "1.0",
            "base_url": self.base_url,
            "course_id": normalized_course_id,
            "cmid": normalized_cmid,
            "answer_transport": answer_transport,
            "artifact": {
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "sha256": digest,
            },
            "finalize": finalize,
            "idempotency_key": idempotency_key,
            "storage_state": state,
        }
        if previous_managed_filename is not None:
            request_payload["previous_managed_filename"] = previous_managed_filename
            request_payload["previous_managed_sha256"] = previous_managed_sha256
        if normalized_expected_attempt is not None and normalized_expected_slot is not None:
            request_payload["expected_attempt_id"] = normalized_expected_attempt
            request_payload["expected_question_slot"] = normalized_expected_slot
        result = await self._call("sync_quiz_essay", request_payload)
        status = self._required_text(result.get("status"), 32, "Moodle quiz synchronization status")
        if status not in {"DRAFT_SAVED", "FINALIZED"}:
            raise IntegrationProtocolError(
                "Moodle browser returned an invalid quiz synchronization status"
            )
        expected_status = "FINALIZED" if finalize else "DRAFT_SAVED"
        if status != expected_status:
            raise IntegrationProtocolError(
                "Moodle browser returned an inconsistent quiz synchronization status"
            )
        receipt = self._normalise_quiz_receipt(
            result.get("receipt"),
            course_id=normalized_course_id,
            cmid=normalized_cmid,
            filename=filename,
            digest=digest,
            size_bytes=len(content),
            idempotency_key=idempotency_key,
        )
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserQuizEssayResult(
            status=cast(Literal["DRAFT_SAVED", "FINALIZED"], status),
            receipt=receipt,
            storage_state=self.storage_state or {},
        )

    async def sync_quiz_answers(
        self,
        course_id: str,
        cmid: int,
        answers: tuple[MoodleBrowserQuizAnswer, ...],
        *,
        expected_attempt_id: str,
        finalize: bool,
        idempotency_key: str,
    ) -> MoodleBrowserQuizAnswersResult:
        """Save independent responses and verify every receipt before accepting completion."""

        course_id = self._positive_id(course_id, "Moodle course id")
        cmid = self._positive_int(cmid, "Moodle activity id")
        attempt_id = self._positive_id(expected_attempt_id, "Moodle attempt id")
        if (
            not isinstance(finalize, bool)
            or not isinstance(idempotency_key, str)
            or not _IDEMPOTENCY_KEY_RE.fullmatch(idempotency_key)
        ):
            raise IntegrationProtocolError("Moodle quiz batch flags are invalid")
        if not isinstance(answers, tuple | list) or not 1 <= len(answers) <= 32:
            raise IntegrationProtocolError("Moodle quiz answer count is invalid")
        request_answers: list[dict[str, Any]] = []
        expected: dict[str, tuple[str, str, int]] = {}
        for answer in answers:
            if not isinstance(answer, MoodleBrowserQuizAnswer):
                raise IntegrationProtocolError("Moodle quiz answer is invalid")
            slot = self._positive_id(answer.question_slot, "Moodle question slot")
            if (
                slot in expected
                or not isinstance(answer.answer_transport, str)
                or answer.answer_transport
                not in {
                    "ESSAY_ATTACHMENT",
                    "ESSAY_ONLINE_TEXT",
                }
            ):
                raise IntegrationProtocolError("Moodle quiz answer slots or transports are invalid")
            filename, content = self._normalise_quiz_artifact(answer.artifact)
            digest = hashlib.sha256(content).hexdigest()
            if (answer.previous_managed_filename is None) != (
                answer.previous_managed_sha256 is None
            ):
                raise IntegrationProtocolError("Moodle previous managed receipt is incomplete")
            if answer.previous_managed_filename is not None and (
                answer.previous_managed_filename not in _MANAGED_SUBMISSION_FILENAMES
                or not _SHA256_RE.fullmatch(answer.previous_managed_sha256 or "")
            ):
                raise IntegrationProtocolError("Moodle previous managed receipt is invalid")
            expected[slot] = (filename, digest, len(content))
            request_answers.append(
                {
                    "question_slot": slot,
                    "answer_transport": answer.answer_transport,
                    "artifact": {
                        "filename": filename,
                        "content_base64": base64.b64encode(content).decode("ascii"),
                        "sha256": digest,
                    },
                    "previous_managed_filename": answer.previous_managed_filename,
                    "previous_managed_sha256": answer.previous_managed_sha256,
                }
            )
        result = await self._call(
            "sync_quiz_answers",
            {
                "schema_version": "1.0",
                "base_url": self.base_url,
                "course_id": course_id,
                "cmid": cmid,
                "expected_attempt_id": attempt_id,
                "answers": request_answers,
                "finalize": finalize,
                "idempotency_key": idempotency_key,
                "storage_state": self._required_storage_state(),
            },
        )
        status = "FINALIZED" if finalize else "DRAFT_SAVED"
        raw_receipts = result.get("receipts")
        if (
            result.get("status") != status
            or not isinstance(raw_receipts, list)
            or len(raw_receipts) != len(expected)
        ):
            raise IntegrationProtocolError("Moodle quiz batch confirmation is incomplete")
        receipts: dict[str, MoodleBrowserQuizEssayReceipt] = {}
        for raw in raw_receipts:
            if not isinstance(raw, dict):
                raise IntegrationProtocolError("Moodle quiz batch receipt is invalid")
            slot = self._positive_id(raw.get("question_slot"), "Moodle question slot")
            if slot not in expected or slot in receipts:
                raise IntegrationProtocolError("Moodle quiz batch returned different answer slots")
            filename, digest, size = expected[slot]
            receipt = self._normalise_quiz_receipt(
                raw,
                course_id=course_id,
                cmid=cmid,
                filename=filename,
                digest=digest,
                size_bytes=size,
                idempotency_key=idempotency_key,
            )
            if receipt.attempt_id != attempt_id:
                raise IntegrationProtocolError("Moodle quiz batch returned another attempt")
            receipts[slot] = receipt
        self._storage_state = self._response_storage_state(result)
        return MoodleBrowserQuizAnswersResult(
            status=cast(Literal["DRAFT_SAVED", "FINALIZED"], status),
            receipts=tuple(receipts[slot] for slot in expected),
            storage_state=self.storage_state or {},
        )

    def _normalise_prepared_quiz_questions(
        self,
        raw: Any,
    ) -> tuple[MoodleBrowserQuizQuestionPreparation, ...]:
        if raw is None:
            return ()  # Older connector: the scalar one-question contract remains valid.
        if not isinstance(raw, list) or not 1 <= len(raw) <= 32:
            raise IntegrationProtocolError("Moodle prepared question count is invalid")
        questions = []
        slots: set[str] = set()
        supported = {"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"}
        for value in raw:
            if not isinstance(value, dict):
                raise IntegrationProtocolError("Moodle prepared question is invalid")
            slot = self._positive_id(value.get("question_slot"), "Moodle question slot")
            statement = self._required_text(
                value.get("question_text"), 50_000, "Moodle question text"
            )
            transport = value.get("answer_transport")
            available = value.get("available_answer_transports")
            page = value.get("page", 0)
            mark = value.get("question_max_mark")
            if (
                slot in slots
                or not isinstance(transport, str)
                or transport not in supported
                or not isinstance(available, list)
                or not 1 <= len(available) <= 2
                or any(not isinstance(item, str) or item not in supported for item in available)
                or len(set(available)) != len(available)
                or transport not in available
                or isinstance(page, bool)
                or not isinstance(page, int)
                or not 0 <= page < 64
                or (mark is None and len(raw) > 1)
                or (
                    mark is not None
                    and (
                        isinstance(mark, bool)
                        or not isinstance(mark, int | float)
                        or not math.isfinite(mark)
                        or not 0 < mark <= 999_999.99
                    )
                )
            ):
                raise IntegrationProtocolError(
                    "Moodle prepared question configuration is inconsistent"
                )
            slots.add(slot)
            questions.append(
                MoodleBrowserQuizQuestionPreparation(
                    question_slot=slot,
                    question_text=statement,
                    answer_transport=cast(
                        Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"], transport
                    ),
                    available_answer_transports=tuple(available),
                    page=page,
                    question_max_mark=mark,
                )
            )
        return tuple(questions)

    async def prepare_quiz_essay(
        self,
        course_id: str,
        cmid: int,
        *,
        expected_attempt_id: str | None = None,
        expected_question_slot: str | None = None,
    ) -> MoodleBrowserQuizEssayPrepareResult:
        """Open/resume one real Essay attempt and return its concrete binding."""

        normalized_course_id = self._positive_id(course_id, "Moodle course id")
        normalized_cmid = self._positive_int(cmid, "Moodle activity id")
        if (expected_attempt_id is None) != (expected_question_slot is None):
            raise IntegrationProtocolError("Expected Moodle quiz attempt identity is incomplete")
        normalized_expected_attempt = (
            self._positive_id(expected_attempt_id, "Expected Moodle quiz attempt id")
            if expected_attempt_id is not None
            else None
        )
        normalized_expected_slot = (
            self._positive_id(expected_question_slot, "Expected Moodle quiz question slot")
            if expected_question_slot is not None
            else None
        )
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "base_url": self.base_url,
            "course_id": normalized_course_id,
            "cmid": normalized_cmid,
            "storage_state": self._required_storage_state(),
        }
        if normalized_expected_attempt is not None and normalized_expected_slot is not None:
            payload["expected_attempt_id"] = normalized_expected_attempt
            payload["expected_question_slot"] = normalized_expected_slot
        result = await self._call("prepare_quiz_essay", payload)
        if (
            self._required_text(result.get("status"), 32, "Moodle quiz preparation status").upper()
            != "READY"
        ):
            raise IntegrationProtocolError(
                "Moodle browser returned an invalid quiz preparation status"
            )
        raw = result.get("preparation")
        if not isinstance(raw, dict):
            raise IntegrationProtocolError("Moodle browser quiz preparation has an invalid shape")
        prepared_course = self._positive_id(raw.get("course_id"), "Prepared Moodle course id")
        prepared_cmid = self._positive_int(raw.get("cmid"), "Prepared Moodle activity id")
        if prepared_course != normalized_course_id or prepared_cmid != normalized_cmid:
            raise IntegrationProtocolError("Moodle browser prepared a different quiz activity")
        attempt_id = self._positive_id(raw.get("attempt_id"), "Prepared Moodle quiz attempt id")
        question_slot = self._positive_id(
            raw.get("question_slot"), "Prepared Moodle quiz question slot"
        )
        if normalized_expected_attempt is not None and (
            attempt_id != normalized_expected_attempt or question_slot != normalized_expected_slot
        ):
            raise IntegrationProtocolError("Moodle browser changed the bound quiz attempt identity")
        question_text = self._required_text(
            raw.get("question_text"), 50_000, "Prepared Moodle quiz question text"
        )
        answer_transport = self._required_text(
            raw.get("answer_transport"), 32, "Prepared Moodle quiz answer transport"
        ).upper()
        supported = {"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"}
        if answer_transport not in supported:
            raise IntegrationProtocolError(
                "Moodle browser returned an invalid quiz answer transport"
            )
        raw_available = raw.get("available_answer_transports")
        if not isinstance(raw_available, list) or not 1 <= len(raw_available) <= 2:
            raise IntegrationProtocolError(
                "Moodle browser returned invalid available quiz transports"
            )
        available = tuple(
            self._required_text(value, 32, "Available Moodle quiz answer transport").upper()
            for value in raw_available
        )
        if (
            any(value not in supported for value in available)
            or len(set(available)) != len(available)
            or answer_transport not in available
        ):
            raise IntegrationProtocolError(
                "Moodle browser returned inconsistent available quiz transports"
            )
        remaining_seconds = raw.get("remaining_seconds")
        if remaining_seconds is not None and (
            isinstance(remaining_seconds, bool)
            or not isinstance(remaining_seconds, int)
            or not 0 <= remaining_seconds <= 315_360_000
        ):
            raise IntegrationProtocolError("Moodle browser returned an invalid quiz remaining time")
        questions = self._normalise_prepared_quiz_questions(raw.get("questions"))
        if questions and (
            questions[0].question_slot != question_slot
            or questions[0].question_text != question_text
            or questions[0].answer_transport != answer_transport
            or questions[0].available_answer_transports != available
        ):
            raise IntegrationProtocolError("Moodle preparation changed the first question binding")
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserQuizEssayPrepareResult(
            preparation=MoodleBrowserQuizEssayPreparation(
                course_id=prepared_course,
                cmid=prepared_cmid,
                attempt_id=attempt_id,
                question_slot=question_slot,
                question_text=question_text,
                answer_transport=cast(
                    Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"],
                    answer_transport,
                ),
                available_answer_transports=cast(
                    tuple[Literal["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"], ...],
                    available,
                ),
                remaining_seconds=remaining_seconds,
                questions=questions,
            ),
            storage_state=self.storage_state or {},
        )

    async def prepare_assignment_submission(
        self,
        course_id: str,
        cmid: int,
    ) -> MoodleBrowserAssignmentPrepareResult:
        """Open the exact student's writable Assignment form without changing it."""

        normalized_course_id = self._positive_id(course_id, "Moodle course id")
        normalized_cmid = self._positive_int(cmid, "Moodle activity id")
        result = await self._call(
            "prepare_assignment_submission",
            {
                "schema_version": "1.0",
                "base_url": self.base_url,
                "course_id": normalized_course_id,
                "cmid": normalized_cmid,
                "storage_state": self._required_storage_state(),
            },
        )
        if (
            self._required_text(
                result.get("status"), 32, "Moodle assignment preparation status"
            ).upper()
            != "READY"
        ):
            raise IntegrationProtocolError(
                "Moodle browser returned an invalid assignment preparation status"
            )
        raw = result.get("preparation")
        if not isinstance(raw, dict):
            raise IntegrationProtocolError(
                "Moodle browser assignment preparation has an invalid shape"
            )
        prepared_course = self._positive_id(raw.get("course_id"), "Prepared Moodle course id")
        prepared_cmid = self._positive_int(raw.get("cmid"), "Prepared Moodle activity id")
        if prepared_course != normalized_course_id or prepared_cmid != normalized_cmid:
            raise IntegrationProtocolError(
                "Moodle browser prepared a different assignment activity"
            )
        answer_transport = self._required_text(
            raw.get("answer_transport"), 32, "Prepared Moodle assignment answer transport"
        ).upper()
        supported = {"ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"}
        if answer_transport not in supported:
            raise IntegrationProtocolError(
                "Moodle browser returned an invalid assignment answer transport"
            )
        raw_available = raw.get("available_answer_transports")
        if not isinstance(raw_available, list) or not 1 <= len(raw_available) <= 2:
            raise IntegrationProtocolError(
                "Moodle browser returned invalid available assignment transports"
            )
        available = tuple(
            self._required_text(value, 32, "Available Moodle assignment answer transport").upper()
            for value in raw_available
        )
        if (
            any(value not in supported for value in available)
            or len(set(available)) != len(available)
            or answer_transport not in available
        ):
            raise IntegrationProtocolError(
                "Moodle browser returned inconsistent available assignment transports"
            )
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserAssignmentPrepareResult(
            preparation=MoodleBrowserAssignmentPreparation(
                course_id=prepared_course,
                cmid=prepared_cmid,
                answer_transport=cast(
                    Literal["ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"], answer_transport
                ),
                available_answer_transports=cast(
                    tuple[Literal["ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"], ...], available
                ),
            ),
            storage_state=self.storage_state or {},
        )

    async def sync_assignment_submission(
        self,
        course_id: str,
        cmid: int,
        artifact: MoodleBrowserAssignmentArtifact,
        *,
        answer_transport: Literal["ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"],
        finalize: bool,
        requires_submission_statement: bool = False,
        submission_drafts: bool | None = None,
        max_submission_bytes_inherited: bool = False,
        previous_managed_filename: str | None = None,
        previous_managed_sha256: str | None = None,
        idempotency_key: str,
    ) -> MoodleBrowserAssignmentResult:
        """Synchronize one artifact through a proven student Assignment form."""

        normalized_course_id = self._positive_id(course_id, "Moodle course id")
        normalized_cmid = self._positive_int(cmid, "Moodle activity id")
        if answer_transport not in {"ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"}:
            raise IntegrationProtocolError("Moodle Assignment answer transport is invalid")
        if not isinstance(finalize, bool):
            raise IntegrationProtocolError("Moodle assignment finalize flag is invalid")
        if not isinstance(requires_submission_statement, bool):
            raise IntegrationProtocolError("Moodle assignment submission statement flag is invalid")
        if submission_drafts is not None and not isinstance(submission_drafts, bool):
            raise IntegrationProtocolError("Moodle assignment drafts flag is invalid")
        if not isinstance(max_submission_bytes_inherited, bool):
            raise IntegrationProtocolError("Moodle assignment inherited file limit flag is invalid")
        if (
            previous_managed_filename is not None
            and previous_managed_filename not in _MANAGED_SUBMISSION_FILENAMES
        ):
            raise IntegrationProtocolError("Moodle previous managed filename is invalid")
        if (previous_managed_filename is None) != (previous_managed_sha256 is None):
            raise IntegrationProtocolError("Moodle previous managed receipt is incomplete")
        if previous_managed_sha256 is not None and not _SHA256_RE.fullmatch(
            previous_managed_sha256
        ):
            raise IntegrationProtocolError("Moodle previous managed digest is invalid")
        if not isinstance(idempotency_key, str) or not _IDEMPOTENCY_KEY_RE.fullmatch(
            idempotency_key
        ):
            raise IntegrationProtocolError("Invalid Moodle idempotency key")
        filename, content = self._normalise_quiz_artifact(artifact)
        digest = hashlib.sha256(content).hexdigest()
        state = self._required_storage_state()
        request_payload: dict[str, Any] = {
            "schema_version": "1.0",
            "base_url": self.base_url,
            "course_id": normalized_course_id,
            "cmid": normalized_cmid,
            "answer_transport": answer_transport,
            "artifact": {
                "filename": filename,
                "content_base64": base64.b64encode(content).decode("ascii"),
                "sha256": digest,
            },
            "finalize": finalize,
            "requires_submission_statement": requires_submission_statement,
            "submission_drafts": submission_drafts,
            "max_submission_bytes_inherited": max_submission_bytes_inherited,
            "idempotency_key": idempotency_key,
            "storage_state": state,
        }
        if previous_managed_filename is not None:
            request_payload["previous_managed_filename"] = previous_managed_filename
            request_payload["previous_managed_sha256"] = previous_managed_sha256
        result = await self._call("sync_assignment_submission", request_payload)
        status = self._required_text(
            result.get("status"),
            32,
            "Moodle assignment synchronization status",
        )
        if status not in {"DRAFT_SAVED", "FINALIZED"}:
            raise IntegrationProtocolError(
                "Moodle browser returned an invalid assignment synchronization status"
            )
        # With submission drafts disabled, Moodle makes Save changes submitted
        # immediately. That is a successful content save, not a retryable
        # mismatch. Explicit finalization must still end in FINALIZED.
        if finalize and status != "FINALIZED":
            raise IntegrationProtocolError(
                "Moodle browser did not finalize the assignment submission"
            )
        if not finalize and status == "FINALIZED" and submission_drafts is not False:
            raise IntegrationProtocolError(
                "Moodle browser finalized a non-final assignment save unexpectedly"
            )
        receipt = self._normalise_assignment_receipt(
            result.get("receipt"),
            course_id=normalized_course_id,
            cmid=normalized_cmid,
            filename=filename,
            digest=digest,
            size_bytes=len(content),
            idempotency_key=idempotency_key,
        )
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserAssignmentResult(
            status=cast(Literal["DRAFT_SAVED", "FINALIZED"], status),
            receipt=receipt,
            storage_state=self.storage_state or {},
        )

    async def discover_historical_submissions(
        self,
        *,
        course_id: str,
        actor_external_subject: str,
        module: Literal["quiz", "assign"] | str,
        cmid: int,
        cursor: str = "0:0",
        limit: int = 10,
        priority_only: bool = False,
    ) -> MoodleBrowserHistoricalSubmissionsResult:
        normalized_course = self._positive_id(course_id, "Moodle course id")
        actor_id = self._positive_id(actor_external_subject, "Moodle actor external subject")
        normalized_module = str(module).lower().removeprefix("mod_")
        if normalized_module not in {"quiz", "assign"}:
            raise IntegrationProtocolError("Moodle historical activity type is invalid")
        normalized_cmid = self._positive_int(cmid, "Moodle activity id")
        if not isinstance(cursor, str) or not cursor or len(cursor) > 128 or "\x00" in cursor:
            raise IntegrationProtocolError("Moodle historical cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 25:
            raise IntegrationProtocolError("Moodle historical page size is invalid")
        if not isinstance(priority_only, bool):
            raise IntegrationProtocolError("Moodle historical priority flag is invalid")
        result = await self._call(
            "discover_historical_submissions",
            {
                "schema_version": "1.0",
                "base_url": self.base_url,
                "course_id": normalized_course,
                "actor_external_subject": actor_id,
                "activity": {"module": normalized_module, "cmid": normalized_cmid},
                "cursor": cursor,
                "limit": limit,
                "priority_only": priority_only,
                "storage_state": self._required_storage_state(),
            },
        )
        if self._positive_id(result.get("course_id"), "Moodle course id") != normalized_course:
            raise IntegrationProtocolError("Moodle historical response identifies another course")
        activity = result.get("activity")
        if not isinstance(activity, dict):
            raise IntegrationProtocolError("Moodle historical activity is invalid")
        response_module = self._required_text(
            activity.get("module"), 16, "Moodle activity module"
        ).lower()
        response_cmid = self._positive_int(activity.get("cmid"), "Moodle activity id")
        if response_module != normalized_module or response_cmid != normalized_cmid:
            raise IntegrationProtocolError("Moodle historical response identifies another activity")
        raw_items = result.get("items")
        if not isinstance(raw_items, list) or len(raw_items) > limit:
            raise IntegrationProtocolError("Moodle historical submissions have an invalid shape")
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise IntegrationProtocolError("Moodle historical submission is invalid")
            external_id = self._required_text(
                raw.get("external_id"), 255, "Moodle historical submission id"
            )
            revision = self._sha256(raw.get("external_revision"), "historical submission revision")
            user_id = self._positive_id(raw.get("user_id"), "Moodle student id")
            item_module = self._required_text(raw.get("module"), 16, "Moodle module").lower()
            item_cmid = self._positive_int(raw.get("cmid"), "Moodle activity id")
            responses = raw.get("responses")
            if (
                item_module != normalized_module
                or item_cmid != normalized_cmid
                or not isinstance(responses, list)
                or len(responses) > 128
            ):
                raise IntegrationProtocolError("Moodle historical submission context is invalid")
            # Detach the response while preserving only JSON data that crossed
            # the already size-bounded internal transport.
            items.append(
                {
                    **raw,
                    "external_id": external_id,
                    "external_revision": revision,
                    "user_id": user_id,
                }
            )
        next_cursor_raw = result.get("next_cursor")
        next_cursor = None
        if next_cursor_raw is not None:
            next_cursor = self._required_text(next_cursor_raw, 128, "Moodle historical next cursor")
        complete = self._bool(result.get("complete"), default=next_cursor is None)
        if complete == (next_cursor is not None):
            raise IntegrationProtocolError("Moodle historical pagination is inconsistent")
        raw_warnings = result.get("warnings", [])
        if not isinstance(raw_warnings, list) or len(raw_warnings) > 100:
            raise IntegrationProtocolError("Moodle historical warnings are invalid")
        warnings = tuple(
            self._required_text(value, 500, "Moodle historical warning") for value in raw_warnings
        )
        refreshed_state = self._response_storage_state(result)
        self._storage_state = refreshed_state
        return MoodleBrowserHistoricalSubmissionsResult(
            course_id=normalized_course,
            module=cast(Literal["quiz", "assign"], normalized_module),
            cmid=normalized_cmid,
            items=tuple(items),
            next_cursor=next_cursor,
            complete=complete,
            warnings=warnings,
            storage_state=self.storage_state or {},
        )

    async def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        path = _ENDPOINTS.get(operation)
        if path is None:
            raise IntegrationProtocolError("Unsupported Moodle browser operation")
        if not self.service_url or not self.shared_secret:
            raise IntegrationConfigurationError("Moodle browser service is not configured")
        if len(self.shared_secret.encode("utf-8")) < 32:
            raise IntegrationConfigurationError(
                "Moodle browser shared secret must contain at least 32 bytes"
            )
        try:
            body = canonical_json(payload)
        except (RecursionError, TypeError, ValueError) as exc:
            raise IntegrationProtocolError("Moodle browser request is not valid JSON") from exc
        if len(body) > self.request_limit:
            raise IntegrationProtocolError(
                "Moodle browser request exceeds the configured size limit"
            )
        timestamp = str(int(self.clock()))
        nonce = str(self.nonce_factory())
        if not _NONCE_RE.fullmatch(nonce):
            raise IntegrationConfigurationError("Moodle browser nonce factory is invalid")
        canonical = f"{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode("ascii")
        signature = hmac.new(
            self.shared_secret.encode("utf-8"), canonical, hashlib.sha256
        ).hexdigest()
        try:
            result = await request_json_limited(
                self.client,
                "POST",
                f"{self.service_url}{path}",
                timeout_seconds=self.timeout_seconds,
                response_limit=self.response_limit,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Moodle-Timestamp": timestamp,
                    "X-Moodle-Nonce": nonce,
                    "X-Moodle-Signature": f"v1={signature}",
                },
                content=body,
            )
        except IntegrationUnavailable as exc:
            cause = exc.__cause__
            status_code = (
                cause.response.status_code if isinstance(cause, httpx.HTTPStatusError) else None
            )
            if operation == "login" and status_code in {401, 403}:
                raise MoodleAuthenticationError("Moodle credentials were not accepted") from exc
            if status_code == 401:
                raise MoodleAuthenticationError("Moodle browser session expired") from exc
            if operation == "discover_course" and status_code == 403:
                raise IntegrationProtocolError("Moodle did not confirm teacher membership") from exc
            if (
                operation
                in {
                    "prepare_quiz_essay",
                    "prepare_assignment_submission",
                }
                and status_code == 403
            ):
                raise IntegrationAssessmentUnavailable(
                    "Moodle did not allow this student to open the assessment"
                ) from exc
            if status_code == 429:
                raise IntegrationBusy("Moodle browser connector is busy") from exc
            if status_code == 503 and isinstance(cause, httpx.HTTPStatusError):
                diagnostic = cause.response.headers.get("X-Moodle-Error-Code")
                if diagnostic in {
                    "MOODLE_RESPONSE_TIMEOUT", "MOODLE_DOCUMENT_TIMEOUT",
                    "MOODLE_DNS_ERROR", "MOODLE_CONNECTION_ERROR", "MOODLE_TLS_ERROR",
                    "MOODLE_HTTP_ERROR", "MOODLE_NAVIGATION_ERROR",
                }:
                    # Keep the cause in durable outbox diagnostics while
                    # preserving retries. Never relay arbitrary headers/bodies.
                    raise IntegrationUnavailable(
                        f"{diagnostic}: Moodle page could not be opened"
                    ) from exc
            if (
                operation
                in {
                    "prepare_quiz_essay",
                    "prepare_assignment_submission",
                    "sync_quiz_essay",
                    "sync_quiz_answers",
                    "sync_assignment_submission",
                }
                and status_code == 423
            ):
                raise IntegrationAttemptFinalized(
                    "Moodle attempt was already finalized outside the application"
                ) from exc
            if operation == "push_grade" and status_code == 501:
                raise IntegrationConfigurationError(
                    "Moodle browser grade writing is not implemented"
                ) from exc
            if operation in {"sync_quiz_essay", "sync_quiz_answers"} and status_code == 501:
                raise IntegrationConfigurationError(
                    "Moodle browser quiz synchronization is not implemented"
                ) from exc
            if operation == "prepare_quiz_essay" and status_code == 501:
                raise IntegrationConfigurationError(
                    "Moodle browser quiz preparation is not implemented"
                ) from exc
            if operation == "prepare_assignment_submission" and status_code == 501:
                raise IntegrationConfigurationError(
                    "Moodle browser assignment preparation is not implemented"
                ) from exc
            if operation == "sync_assignment_submission" and status_code == 501:
                raise IntegrationConfigurationError(
                    "Moodle browser assignment synchronization is not implemented"
                ) from exc
            if operation == "sync_assignment_submission" and status_code == 409:
                raise IntegrationProtocolError(
                    "Moodle assignment idempotency key conflicts with another artifact"
                ) from exc
            if operation == "sync_quiz_essay" and status_code == 409:
                raise IntegrationProtocolError(
                    "Moodle quiz idempotency key conflicts with another artifact"
                ) from exc
            if status_code == 502 and isinstance(cause, httpx.HTTPStatusError):
                # Error bodies are deliberately not read by the bounded HTTP
                # transport. Accept only known codes from the internal service.
                diagnostic = cause.response.headers.get("X-Moodle-Error-Code")
                if operation == "discover_historical_submissions" and diagnostic in (
                    "ASSIGN_TABLE_NOT_FOUND", "QUIZ_TABLE_NOT_FOUND"
                ):
                    raise IntegrationProtocolError(
                        f"{diagnostic}: Moodle submissions table was not recognized"
                    ) from exc
                if diagnostic in {
                    "UPLOAD_INVALID_FILE", "UPLOAD_INVALID_TYPE", "UPLOAD_TOO_LARGE",
                    "UPLOAD_REJECTED",
                }:
                    raise IntegrationProtocolError(
                        f"{diagnostic}: Moodle rejected the uploaded file"
                    ) from exc
            if status_code in {400, 403, 409, 413, 422, 502}:
                raise IntegrationProtocolError(
                    f"Moodle browser rejected the operation (HTTP {status_code})"
                ) from exc
            raise
        if not isinstance(result, dict):
            raise IntegrationProtocolError("Moodle browser returned an invalid response")
        return result

    def _normalise_identity(self, value: Any, fallback_name: str) -> TokenIdentity:
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Moodle browser identity has an invalid shape")
        external_subject = self._positive_id(
            value.get("external_subject", value.get("user_id")),
            "Moodle user id",
        )
        display_name = self._optional_text(value.get("display_name"), 255) or fallback_name
        email = self._optional_text(value.get("email"), 320)
        locale = self._optional_text(value.get("locale"), 32) or "ru"
        raw_courses = value.get("courses", [])
        if not isinstance(raw_courses, list) or len(raw_courses) > _MAX_COURSES:
            raise IntegrationProtocolError("Moodle browser course list has an invalid shape")
        memberships: list[MoodleCourseMembership] = []
        seen: set[str] = set()
        for raw in raw_courses:
            if not isinstance(raw, dict):
                raise IntegrationProtocolError("Moodle browser course list has an invalid entry")
            course_id = self._required_text(
                raw.get("external_id", raw.get("id")), 255, "Moodle course id"
            )
            if course_id in seen:
                raise IntegrationProtocolError("Moodle browser course list has duplicate ids")
            seen.add(course_id)
            role = self._optional_text(raw.get("role"), 32).upper() or "UNKNOWN"
            if role not in {"STUDENT", "TEACHER", "UNKNOWN"}:
                raise IntegrationProtocolError("Moodle browser course role is invalid")
            memberships.append(
                MoodleCourseMembership(
                    external_id=course_id,
                    title=self._optional_text(raw.get("title"), 255),
                    short_name=self._optional_text(
                        raw.get("short_name", raw.get("shortname")), 120
                    ),
                    role=role,
                )
            )
        # A browser session is intentionally not represented as a reusable Moodle
        # token.  The remaining fields match TokenIdentity so identity projection
        # can be shared without ever persisting a password.
        return TokenIdentity(
            token="",
            external_subject=external_subject,
            display_name=display_name,
            email=email,
            locale=locale,
            courses=tuple(memberships),
            functions=frozenset(),
            upload_files=False,
        )

    def _normalise_discovery(
        self,
        value: Any,
        expected_course_id: str,
        actor_external_subject: str,
    ) -> CourseDiscovery:
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Moodle browser discovery has an invalid shape")
        returned_id = self._required_text(value.get("external_id"), 255, "Moodle course id")
        if returned_id != expected_course_id:
            raise IntegrationProtocolError("Moodle browser returned a different course")
        if isinstance(value.get("preview"), dict):
            return self._normalise_projected_discovery(
                value,
                returned_id=returned_id,
                actor_external_subject=actor_external_subject,
            )
        title = self._required_text(value.get("title"), 255, "Moodle course title")
        sections = self._normalise_sections(value.get("sections", []))
        members = self._normalise_participants(value.get("participants", []))
        actor_role = self._optional_text(value.get("actor_role"), 32).upper()
        actor = next(
            (member for member in members if member["user_id"] == actor_external_subject),
            None,
        )
        if actor is not None:
            actor_role = str(actor["role"])
        if actor_role != "TEACHER":
            raise IntegrationProtocolError("Moodle did not confirm teacher membership")
        if actor is None:
            members.append(
                {
                    "user_id": actor_external_subject,
                    "display_name": "",
                    "email": "",
                    "suspended": False,
                    "role": "TEACHER",
                    "roles": ["TEACHER"],
                    "groups": [],
                }
            )
        groups = sorted(
            {
                (str(group["id"]), str(group.get("name", "")))
                for member in members
                for group in member.get("groups", [])
                if isinstance(group, dict) and str(group.get("id", ""))
            }
        )
        membership_snapshot = {
            "complete": bool(value.get("participants_complete", True)),
            "members": members,
        }
        course_projection = {
            "external_id": returned_id,
            "title": title,
            "short_name": self._optional_text(value.get("short_name", value.get("shortname")), 120),
            "summary": self._optional_text(value.get("summary"), 20_000),
            "course_url": self._safe_course_url(value.get("course_url"), returned_id),
            "starts_at_epoch": self._nonnegative_int(value.get("starts_at_epoch")),
            "ends_at_epoch": self._nonnegative_int(value.get("ends_at_epoch")),
            "sections": sections,
            "groups": [{"external_id": group_id, "name": name} for group_id, name in groups],
        }
        capabilities = self._normalise_capabilities(value.get("capabilities", {}))
        return CourseDiscovery(
            external_id=returned_id,
            preview={
                **course_projection,
                "external_revision": sha256_hex(canonical_json(course_projection)),
                "membership_revision": sha256_hex(canonical_json(membership_snapshot)),
                "membership_snapshot": membership_snapshot,
            },
            capabilities=capabilities,
        )

    def _normalise_projected_discovery(
        self,
        value: dict[str, Any],
        *,
        returned_id: str,
        actor_external_subject: str,
    ) -> CourseDiscovery:
        preview = value["preview"]
        if not isinstance(preview, dict):  # pragma: no cover - narrowed by caller
            raise IntegrationProtocolError("Moodle browser preview has an invalid shape")
        preview_id = self._required_text(
            preview.get("external_id"), 255, "Moodle course preview id"
        )
        if preview_id != returned_id:
            raise IntegrationProtocolError("Moodle browser preview identifies another course")
        actor_role = self._optional_text(value.get("actor_role"), 32).upper()
        if actor_role != "TEACHER":
            raise IntegrationProtocolError("Moodle did not confirm teacher membership")
        sections = self._normalise_sections(preview.get("sections", []))
        membership = preview.get("membership_snapshot")
        if not isinstance(membership, dict):
            raise IntegrationProtocolError(
                "Moodle browser membership snapshot has an invalid shape"
            )
        members = self._normalise_participants(membership.get("members", []))
        actor = next(
            (member for member in members if member["user_id"] == actor_external_subject),
            None,
        )
        if actor is not None and actor["role"] != "TEACHER":
            raise IntegrationProtocolError("Moodle actor role conflicts with membership snapshot")
        if actor is None:
            members.append(
                {
                    "user_id": actor_external_subject,
                    "display_name": "",
                    "email": "",
                    "suspended": False,
                    "role": "TEACHER",
                    "roles": ["TEACHER"],
                    "groups": [],
                }
            )
        raw_groups = preview.get("groups", [])
        if not isinstance(raw_groups, list) or len(raw_groups) > 10_000:
            raise IntegrationProtocolError("Moodle browser groups have an invalid shape")
        groups: list[dict[str, str]] = []
        seen_groups: set[str] = set()
        for raw in raw_groups:
            if not isinstance(raw, dict):
                raise IntegrationProtocolError("Moodle browser group has an invalid shape")
            group_id = self._required_text(
                raw.get("external_id", raw.get("id")), 255, "Moodle group id"
            )
            if group_id in seen_groups:
                raise IntegrationProtocolError("Moodle browser groups have duplicate ids")
            seen_groups.add(group_id)
            groups.append(
                {
                    "external_id": group_id,
                    "name": self._required_text(raw.get("name"), 255, "Moodle group name"),
                }
            )
        normalized_membership = {
            "complete": self._bool(membership.get("complete"), default=False),
            "members": members,
        }
        external_revision = self._sha256(preview.get("external_revision"), "course revision")
        membership_revision = self._sha256(
            preview.get("membership_revision"), "membership revision"
        )
        normalized_preview = {
            "external_id": returned_id,
            "title": self._required_text(preview.get("title"), 255, "Moodle course title"),
            "short_name": self._optional_text(
                preview.get("short_name", preview.get("shortname")), 120
            ),
            "external_revision": external_revision,
            "membership_revision": membership_revision,
            "starts_at_epoch": self._nonnegative_int(preview.get("starts_at_epoch")),
            "ends_at_epoch": self._nonnegative_int(preview.get("ends_at_epoch")),
            "sections": sections,
            "groups": groups,
            "membership_snapshot": normalized_membership,
        }
        return CourseDiscovery(
            external_id=returned_id,
            preview=normalized_preview,
            capabilities=self._normalise_capabilities(value.get("capabilities", {})),
        )

    def _normalise_sections(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > _MAX_SECTIONS:
            raise IntegrationProtocolError("Moodle browser sections have an invalid shape")
        sections: list[dict[str, Any]] = []
        activity_count = 0
        seen: set[str] = set()
        for index, raw in enumerate(value):
            if not isinstance(raw, dict):
                raise IntegrationProtocolError("Moodle browser section has an invalid shape")
            section_id = self._required_text(
                raw.get("external_id", raw.get("id", str(index))),
                255,
                "Moodle section id",
            )
            if section_id in seen:
                raise IntegrationProtocolError("Moodle browser sections have duplicate ids")
            seen.add(section_id)
            raw_activities = raw.get("activities", [])
            if not isinstance(raw_activities, list):
                raise IntegrationProtocolError("Moodle browser activities have an invalid shape")
            activity_count += len(raw_activities)
            if activity_count > _MAX_ACTIVITIES:
                raise IntegrationProtocolError("Moodle browser returned too many activities")
            activities: list[dict[str, Any]] = []
            for activity in raw_activities:
                if not isinstance(activity, dict):
                    raise IntegrationProtocolError("Moodle browser activity has an invalid shape")
                # Preserve the service's narrow activity projection, but detach it
                # from the decoded response and prove that it is finite JSON.
                try:
                    detached = dict(activity)
                    canonical_json(detached)
                except (TypeError, ValueError) as exc:
                    raise IntegrationProtocolError(
                        "Moodle browser activity is not valid JSON"
                    ) from exc
                activities.append(detached)
            position = self._nonnegative_int(raw.get("position"), default=index)
            sections.append(
                {
                    "external_id": section_id,
                    "title": self._optional_text(raw.get("title", raw.get("name")), 255),
                    "position": position,
                    "visible": self._bool(raw.get("visible"), default=True),
                    "activities": activities,
                }
            )
        return sections

    def _normalise_participants(self, value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list) or len(value) > _MAX_PARTICIPANTS:
            raise IntegrationProtocolError("Moodle browser participants have an invalid shape")
        members: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in value:
            if not isinstance(raw, dict):
                raise IntegrationProtocolError("Moodle browser participant is invalid")
            user_id = self._required_text(
                raw.get("user_id", raw.get("external_subject", raw.get("id"))),
                255,
                "Moodle participant id",
            )
            if user_id in seen:
                raise IntegrationProtocolError("Moodle browser participants have duplicate ids")
            seen.add(user_id)
            role = self._optional_text(raw.get("role"), 32).upper()
            if role not in {"STUDENT", "TEACHER"}:
                raise IntegrationProtocolError("Moodle browser participant role is invalid")
            raw_groups = raw.get("groups", [])
            if not isinstance(raw_groups, list) or len(raw_groups) > _MAX_GROUPS_PER_PARTICIPANT:
                raise IntegrationProtocolError("Moodle browser participant groups are invalid")
            groups: list[dict[str, str]] = []
            for group in raw_groups:
                if not isinstance(group, dict):
                    raise IntegrationProtocolError("Moodle browser group is invalid")
                group_id = self._required_text(
                    group.get("id", group.get("external_id")), 255, "Moodle group id"
                )
                groups.append({"id": group_id, "name": self._optional_text(group.get("name"), 255)})
            members.append(
                {
                    "user_id": user_id,
                    "display_name": self._optional_text(
                        raw.get("display_name", raw.get("fullname")), 255
                    ),
                    "email": self._optional_text(raw.get("email"), 320),
                    "suspended": self._bool(raw.get("suspended"), default=False),
                    "role": role,
                    "roles": [role],
                    "groups": groups,
                }
            )
        return members

    @staticmethod
    def _normalise_capabilities(value: Any) -> dict[str, bool]:
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Moodle browser capabilities have an invalid shape")
        keys = (
            "roster",
            "groups",
            "grades",
            "comments",
            "checkpoints",
            "task_bank_mirror",
            "native_question_bank_write",
        )
        return {key: value.get(key) is True for key in keys}

    def _normalise_grade_payload(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise IntegrationProtocolError("Moodle grade payload has an invalid shape")
        course_id = self._positive_id(
            payload.get("course_id", payload.get("courseid")), "Moodle course id"
        )
        user_id = self._positive_id(payload.get("user_id", payload.get("userid")), "Moodle user id")
        cmid = self._positive_int(payload.get("cmid"), "Moodle activity id")
        raw_grade = payload.get("grade")
        if isinstance(raw_grade, bool):
            raise IntegrationProtocolError("Moodle grade is invalid")
        try:
            grade = Decimal(str(raw_grade))
        except (InvalidOperation, ValueError) as exc:
            raise IntegrationProtocolError("Moodle grade is invalid") from exc
        if not grade.is_finite() or grade < 0 or grade > Decimal("1000000"):
            raise IntegrationProtocolError("Moodle grade is invalid")
        normalized_grade = format(grade, "f")
        decimal_places = len(normalized_grade.partition(".")[2].rstrip("0"))
        if decimal_places > 5 or len(normalized_grade.replace(".", "").lstrip("0")) > 12:
            raise IntegrationProtocolError("Moodle grade precision is invalid")
        comment = payload.get("comment", "")
        if not isinstance(comment, str) or len(comment) > 20_000:
            raise IntegrationProtocolError("Moodle grade comment is invalid")
        raw_module = payload.get("module", "assign")
        if raw_module not in {"assign", "quiz"}:
            raise IntegrationProtocolError("Moodle grade module is invalid")
        normalized: dict[str, Any] = {
            "module": raw_module,
            "course_id": course_id,
            "cmid": cmid,
            "user_id": user_id,
            "grade": float(grade),
            "comment": comment,
        }
        if raw_module == "quiz":
            normalized["attempt_id"] = self._positive_id(
                payload.get("attempt_id"), "Moodle quiz attempt id"
            )
            normalized["question_slot"] = self._positive_int(
                payload.get("question_slot"), "Moodle quiz question slot"
            )
            raw_scale_max = payload.get("grade_scale_max")
            if isinstance(raw_scale_max, bool):
                raise IntegrationProtocolError("Moodle quiz grade scale is invalid")
            try:
                grade_scale_max = Decimal(str(raw_scale_max))
            except (InvalidOperation, ValueError) as exc:
                raise IntegrationProtocolError("Moodle quiz grade scale is invalid") from exc
            if (
                not grade_scale_max.is_finite()
                or grade_scale_max <= 0
                or grade_scale_max > Decimal("1000000")
                or grade > grade_scale_max
            ):
                raise IntegrationProtocolError("Moodle quiz grade scale is invalid")
            normalized_scale = format(grade_scale_max, "f")
            scale_decimal_places = len(normalized_scale.partition(".")[2].rstrip("0"))
            if scale_decimal_places > 5 or len(normalized_scale.replace(".", "").lstrip("0")) > 12:
                raise IntegrationProtocolError("Moodle quiz grade scale precision is invalid")
            normalized["grade_scale_max"] = float(grade_scale_max)
            raw_overall_scale_max = payload.get("quiz_overall_grade_max")
            if isinstance(raw_overall_scale_max, bool):
                raise IntegrationProtocolError("Moodle Quiz overall grade scale is invalid")
            try:
                overall_scale_max = Decimal(str(raw_overall_scale_max))
            except (InvalidOperation, ValueError) as exc:
                raise IntegrationProtocolError(
                    "Moodle Quiz overall grade scale is invalid"
                ) from exc
            if (
                not overall_scale_max.is_finite()
                or overall_scale_max <= 0
                or overall_scale_max > Decimal("1000000")
            ):
                raise IntegrationProtocolError("Moodle Quiz overall grade scale is invalid")
            normalized_overall_scale = format(overall_scale_max, "f")
            overall_decimal_places = len(normalized_overall_scale.partition(".")[2].rstrip("0"))
            if (
                overall_decimal_places > 5
                or len(normalized_overall_scale.replace(".", "").lstrip("0")) > 12
            ):
                raise IntegrationProtocolError(
                    "Moodle Quiz overall grade scale precision is invalid"
                )
            normalized["quiz_overall_grade_max"] = float(overall_scale_max)
        else:
            if (
                payload.get("grade_scale_max") is not None
                or payload.get("quiz_overall_grade_max") is not None
            ):
                raise IntegrationProtocolError(
                    "Moodle assignment grade payload cannot contain a Quiz scale"
                )
            if payload.get("attempt_number") is not None:
                raw_attempt_number = payload.get("attempt_number")
                if (
                    isinstance(raw_attempt_number, bool)
                    or not isinstance(raw_attempt_number, int)
                    or not 0 <= raw_attempt_number <= 1_000_000
                ):
                    raise IntegrationProtocolError("Moodle assignment attempt number is invalid")
                normalized["attempt_number"] = raw_attempt_number
        return normalized

    def _normalise_quiz_artifact(
        self,
        artifact: MoodleBrowserQuizEssayArtifact,
    ) -> tuple[str, bytes]:
        if not isinstance(artifact, MoodleBrowserQuizEssayArtifact):
            raise IntegrationProtocolError("Moodle quiz artifact has an invalid shape")
        filename = artifact.filename
        if (
            not isinstance(filename, str)
            or not _ARTIFACT_FILENAME_RE.fullmatch(filename)
            or filename.endswith(".")
        ):
            raise IntegrationProtocolError("Moodle quiz artifact filename is unsafe")
        content = artifact.content
        if not isinstance(content, bytes):
            raise IntegrationProtocolError("Moodle quiz artifact content must be raw bytes")
        if len(content) > _MAX_QUIZ_ARTIFACT_BYTES:
            raise IntegrationProtocolError("Moodle quiz artifact size is outside the limit")
        return filename, content

    def _normalise_quiz_receipt(
        self,
        value: Any,
        *,
        course_id: str,
        cmid: int,
        filename: str,
        digest: str,
        size_bytes: int,
        idempotency_key: str,
    ) -> MoodleBrowserQuizEssayReceipt:
        if not isinstance(value, Mapping):
            raise IntegrationProtocolError("Moodle browser quiz receipt has an invalid shape")
        expected_keys = {
            "course_id",
            "cmid",
            "attempt_id",
            "question_slot",
            "filename",
            "sha256",
            "size_bytes",
            "idempotency_key",
        }
        if set(value) != expected_keys:
            raise IntegrationProtocolError("Moodle browser quiz receipt has an invalid shape")
        try:
            encoded = canonical_json(dict(value))
        except (RecursionError, TypeError, ValueError) as exc:
            raise IntegrationProtocolError("Moodle browser quiz receipt is not valid JSON") from exc
        if len(encoded) > _MAX_QUIZ_RECEIPT_BYTES:
            raise IntegrationProtocolError("Moodle browser quiz receipt is too large")

        returned_course_id = self._positive_id(value.get("course_id"), "Moodle course id")
        returned_cmid = self._positive_int(value.get("cmid"), "Moodle activity id")
        attempt_id = self._positive_id(value.get("attempt_id"), "Moodle quiz attempt id")
        question_slot = self._positive_id(value.get("question_slot"), "Moodle quiz question slot")
        returned_filename = self._required_text(
            value.get("filename"), 128, "Moodle quiz artifact filename"
        )
        returned_digest = self._required_text(
            value.get("sha256"), 64, "Moodle quiz artifact digest"
        )
        returned_size = self._nonnegative_int(value.get("size_bytes"))
        returned_key = self._required_text(
            value.get("idempotency_key"), 200, "Moodle idempotency key"
        )
        if (
            returned_course_id != course_id
            or returned_cmid != cmid
            or returned_filename != filename
            or not _SHA256_RE.fullmatch(returned_digest)
            or returned_digest != digest
            or returned_size != size_bytes
            or returned_key != idempotency_key
        ):
            raise IntegrationProtocolError("Moodle browser quiz receipt does not match the request")
        return MoodleBrowserQuizEssayReceipt(
            course_id=returned_course_id,
            cmid=returned_cmid,
            attempt_id=attempt_id,
            question_slot=question_slot,
            filename=returned_filename,
            sha256=returned_digest,
            size_bytes=returned_size,
            idempotency_key=returned_key,
        )

    def _normalise_assignment_receipt(
        self,
        value: Any,
        *,
        course_id: str,
        cmid: int,
        filename: str,
        digest: str,
        size_bytes: int,
        idempotency_key: str,
    ) -> MoodleBrowserAssignmentReceipt:
        if not isinstance(value, Mapping):
            raise IntegrationProtocolError("Moodle browser assignment receipt is invalid")
        expected_keys = {
            "course_id",
            "cmid",
            "filename",
            "sha256",
            "size_bytes",
            "idempotency_key",
        }
        if set(value) != expected_keys:
            raise IntegrationProtocolError("Moodle browser assignment receipt is invalid")
        try:
            encoded = canonical_json(dict(value))
        except (RecursionError, TypeError, ValueError) as exc:
            raise IntegrationProtocolError(
                "Moodle browser assignment receipt is not valid JSON"
            ) from exc
        if len(encoded) > _MAX_QUIZ_RECEIPT_BYTES:
            raise IntegrationProtocolError("Moodle browser assignment receipt is too large")
        returned_course_id = self._positive_id(value.get("course_id"), "Moodle course id")
        returned_cmid = self._positive_int(value.get("cmid"), "Moodle activity id")
        returned_filename = self._required_text(
            value.get("filename"), 128, "Moodle assignment artifact filename"
        )
        returned_digest = self._required_text(
            value.get("sha256"), 64, "Moodle assignment artifact digest"
        )
        returned_size = self._nonnegative_int(value.get("size_bytes"))
        returned_key = self._required_text(
            value.get("idempotency_key"), 200, "Moodle idempotency key"
        )
        if (
            returned_course_id != course_id
            or returned_cmid != cmid
            or returned_filename != filename
            or not _SHA256_RE.fullmatch(returned_digest)
            or returned_digest != digest
            or returned_size != size_bytes
            or returned_key != idempotency_key
        ):
            raise IntegrationProtocolError(
                "Moodle browser assignment receipt does not match the request"
            )
        return MoodleBrowserAssignmentReceipt(
            course_id=returned_course_id,
            cmid=returned_cmid,
            filename=returned_filename,
            sha256=returned_digest,
            size_bytes=returned_size,
            idempotency_key=returned_key,
        )

    def _response_storage_state(self, result: Mapping[str, Any]) -> dict[str, Any]:
        if "storage_state" not in result:
            raise IntegrationProtocolError("Moodle browser response has no refreshed state")
        return self._normalise_storage_state(result["storage_state"])

    def _required_storage_state(self) -> dict[str, Any]:
        if self._storage_state is None:
            raise IntegrationConfigurationError("Moodle browser session is not configured")
        return self._normalise_storage_state(self._storage_state)

    def _normalise_storage_state(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise IntegrationProtocolError("Moodle browser state has an invalid shape")
        cookies = value.get("cookies", [])
        origins = value.get("origins", [])
        if not isinstance(cookies, list) or len(cookies) > _MAX_COOKIES:
            raise IntegrationProtocolError("Moodle browser cookies have an invalid shape")
        if not isinstance(origins, list) or len(origins) > _MAX_ORIGINS:
            raise IntegrationProtocolError("Moodle browser origins have an invalid shape")
        expected = urlsplit(self.base_url)
        expected_origin = self._origin(expected)
        normalized_cookies: list[dict[str, Any]] = []
        for cookie in cookies:
            if not isinstance(cookie, Mapping):
                raise IntegrationProtocolError("Moodle browser cookie is invalid")
            name = self._required_text(cookie.get("name"), 256, "Moodle cookie name")
            cookie_value = self._optional_text(cookie.get("value"), 16_384)
            domain = self._required_text(cookie.get("domain"), 253, "Moodle cookie domain")
            normalized_domain = domain.lower().lstrip(".")
            host = expected.hostname.lower() if expected.hostname else ""
            if normalized_domain != host:
                raise IntegrationProtocolError("Moodle browser cookie domain is not configured")
            path = self._required_text(cookie.get("path", "/"), 2_000, "Moodle cookie path")
            if not path.startswith("/"):
                raise IntegrationProtocolError("Moodle browser cookie path is invalid")
            if "\r" in path or "\n" in path:
                raise IntegrationProtocolError("Moodle browser cookie path is invalid")
            expires = cookie.get("expires", -1)
            if (
                isinstance(expires, bool)
                or not isinstance(expires, int | float)
                or not math.isfinite(float(expires))
                or float(expires) < -1
                or float(expires) > 4_102_444_800
            ):
                raise IntegrationProtocolError("Moodle browser cookie expiry is invalid")
            same_site = self._optional_text(cookie.get("sameSite"), 16) or "Lax"
            if same_site not in {"Strict", "Lax", "None"}:
                raise IntegrationProtocolError("Moodle browser cookie SameSite is invalid")
            normalized_cookies.append(
                {
                    "name": name,
                    "value": cookie_value,
                    "domain": domain,
                    "path": path,
                    "expires": expires,
                    "httpOnly": self._bool(cookie.get("httpOnly"), default=False),
                    "secure": self._bool(cookie.get("secure"), default=False),
                    "sameSite": same_site,
                }
            )
        normalized_origins: list[dict[str, Any]] = []
        for raw_origin in origins:
            if not isinstance(raw_origin, Mapping):
                raise IntegrationProtocolError("Moodle browser origin is invalid")
            origin_text = self._required_text(
                raw_origin.get("origin"), 2_000, "Moodle browser origin"
            )
            try:
                parsed_origin = urlsplit(origin_text)
            except ValueError as exc:
                raise IntegrationProtocolError("Moodle browser origin is invalid") from exc
            if (
                self._origin(parsed_origin) != expected_origin
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
                or parsed_origin.username
                or parsed_origin.password
            ):
                raise IntegrationProtocolError("Moodle browser origin is not configured")
            entries = raw_origin.get("localStorage", [])
            if not isinstance(entries, list) or len(entries) > _MAX_LOCAL_STORAGE_ENTRIES:
                raise IntegrationProtocolError("Moodle browser local storage is invalid")
            normalized_entries: list[dict[str, str]] = []
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise IntegrationProtocolError("Moodle browser local storage is invalid")
                normalized_entries.append(
                    {
                        "name": self._required_text(
                            entry.get("name"), 1_024, "Moodle local-storage key"
                        ),
                        "value": self._optional_text(entry.get("value"), 32_768),
                    }
                )
            normalized_origins.append(
                {"origin": origin_text.rstrip("/"), "localStorage": normalized_entries}
            )
        normalized = {"cookies": normalized_cookies, "origins": normalized_origins}
        try:
            encoded = canonical_json(normalized)
        except (TypeError, ValueError) as exc:
            raise IntegrationProtocolError("Moodle browser state is not valid JSON") from exc
        if len(encoded) > self.storage_state_limit:
            raise IntegrationProtocolError("Moodle browser state exceeds the size limit")
        return normalized

    def _safe_course_url(self, value: Any, course_id: str) -> str:
        expected = f"{self.base_url}/course/view.php?id={course_id}"
        if value in {None, ""}:
            return expected
        if not isinstance(value, str) or len(value) > 2_000:
            raise IntegrationProtocolError("Moodle browser course URL is invalid")
        if self.recognize_course_url(value) != course_id:
            raise IntegrationProtocolError("Moodle browser course URL does not match")
        return value

    def _normalise_moodle_url(self, value: str) -> str:
        try:
            parsed = urlsplit(str(value).strip())
            port = parsed.port
        except ValueError as exc:
            raise IntegrationConfigurationError("Moodle base URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
            or (parsed.scheme != "https" and not self.settings.debug)
        ):
            raise IntegrationConfigurationError("Moodle base URL is invalid")
        hostname = parsed.hostname.lower()
        hostname_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme == "https" else 80
        netloc = (
            hostname_for_netloc if port in {None, default_port} else f"{hostname_for_netloc}:{port}"
        )
        return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))

    @staticmethod
    def _normalise_service_url(value: str) -> str:
        if not value:
            return ""
        try:
            parsed = urlsplit(value.strip())
            port = parsed.port
        except ValueError as exc:
            raise IntegrationConfigurationError("Moodle browser service URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise IntegrationConfigurationError("Moodle browser service URL is invalid")
        hostname = parsed.hostname.lower()
        hostname_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme == "https" else 80
        netloc = (
            hostname_for_netloc if port in {None, default_port} else f"{hostname_for_netloc}:{port}"
        )
        return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))

    @staticmethod
    def _origin(value: Any) -> tuple[str, str | None, int | None]:
        try:
            port = value.port
        except ValueError as exc:
            raise IntegrationProtocolError("URL port is invalid") from exc
        if port is None:
            port = 443 if value.scheme == "https" else 80 if value.scheme == "http" else None
        return value.scheme.lower(), value.hostname.lower() if value.hostname else None, port

    @staticmethod
    def _required_text(value: Any, maximum: int, label: str) -> str:
        if not isinstance(value, str):
            value = str(value) if isinstance(value, int) and not isinstance(value, bool) else ""
        result = value.strip()
        if not result or len(result) > maximum or "\x00" in result:
            raise IntegrationProtocolError(f"{label} is invalid")
        return result

    @staticmethod
    def _optional_text(value: Any, maximum: int) -> str:
        if value is None:
            return ""
        if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
            raise IntegrationProtocolError("Moodle browser text field is invalid")
        return value.strip()

    @staticmethod
    def _positive_id(value: Any, label: str) -> str:
        if isinstance(value, bool):
            raise IntegrationProtocolError(f"{label} is invalid")
        result = str(value or "")
        if not result.isdigit() or int(result) <= 0 or len(result) > 20:
            raise IntegrationProtocolError(f"{label} is invalid")
        return result

    @staticmethod
    def _positive_int(value: Any, label: str) -> int:
        normalized = MoodleBrowserClient._positive_id(value, label)
        return int(normalized)

    @staticmethod
    def _nonnegative_int(value: Any, *, default: int = 0) -> int:
        if value is None:
            return default
        if isinstance(value, bool):
            raise IntegrationProtocolError("Moodle browser numeric field is invalid")
        if isinstance(value, int) and 0 <= value < 2**63:
            return value
        if isinstance(value, str) and value.isdigit() and int(value) < 2**63:
            return int(value)
        raise IntegrationProtocolError("Moodle browser numeric field is invalid")

    @staticmethod
    def _sha256(value: Any, label: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(char not in "0123456789abcdef" for char in value)
        ):
            raise IntegrationProtocolError(f"Moodle browser {label} is invalid")
        return value

    @staticmethod
    def _bool(value: Any, *, default: bool) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        raise IntegrationProtocolError("Moodle browser boolean field is invalid")


__all__ = [
    "MoodleBrowserClient",
    "MoodleBrowserAssignmentPreparation",
    "MoodleBrowserAssignmentPrepareResult",
    "MoodleBrowserDiscoveryResult",
    "MoodleBrowserGradeResult",
    "MoodleBrowserHistoricalSubmissionsResult",
    "MoodleBrowserLoginResult",
    "MoodleBrowserQuizEssayArtifact",
    "MoodleBrowserQuizEssayReceipt",
    "MoodleBrowserQuizEssayResult",
]
