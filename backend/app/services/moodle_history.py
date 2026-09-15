from __future__ import annotations

import base64
import binascii
import hashlib
import io
import re
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, DecimalException, InvalidOperation
from pathlib import PurePosixPath
from typing import Any

try:  # Kept optional so an old image reports the missing capability explicitly.
    import py7zr as _py7zr
    from py7zr.io import Py7zIO as _Py7zIO
    from py7zr.io import WriterFactory as _WriterFactory
except ImportError:  # pragma: no cover - exercised by the explicit fallback test
    _py7zr = None
    _Py7zIO = object  # type: ignore[assignment,misc]
    _WriterFactory = object  # type: ignore[assignment,misc]

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.credential_crypto import BROWSER_STATE_CREDENTIAL_KIND
from app.db.base import utcnow
from app.models.attempts import Attempt, Snapshot, Submission, Workspace, WorkspaceFile
from app.models.courses import Course, CourseMembership
from app.models.enums import (
    AssessmentStatus,
    AttemptState,
    CourseRole,
    SyncOutboxState,
    TaskScope,
    TaskVersionStatus,
)
from app.models.identity import ExternalPrincipal, MoodleCredential
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ReviewDecision
from app.models.tasks import Assessment, AssessmentItem, TaskBankItem, TaskVersion
from app.services.common import canonical_hash, language_for_path, validate_source_path
from app.services.moodle_attempt_selection import observe_moodle_attempt
from app.services.submission_origin import historical_response_observations
from app.services.teacher_tokens import teacher_membership_is_authorized
from app.services.workspace import (
    MAX_SOURCE_FILE_BYTES,
    MAX_WORKSPACE_BYTES,
    MAX_WORKSPACE_FILES,
)

_SOURCE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inc",
    ".txt",
}
_MAX_FILES = MAX_WORKSPACE_FILES
_MAX_FILE_BYTES = MAX_SOURCE_FILE_BYTES
_MAX_TOTAL_BYTES = MAX_WORKSPACE_BYTES
_MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
_IMPORT_EVENT_STATES = {
    SyncOutboxState.PENDING.value,
    SyncOutboxState.PROCESSING.value,
    SyncOutboxState.RETRY.value,
}
_MAX_IMPORT_ACTORS = 128

# Version 5 retries source materialization after attachment downloads moved
# from page-level JavaScript fetches to the authenticated browser HTTP context.
# Existing ``moodle-import.txt`` placeholders are therefore replaced on the
# first successful synchronization even when Moodle's own attempt is unchanged.
HISTORICAL_SOURCE_MATERIALIZATION_VERSION = 5


def _origin_receipt_fields(item: dict[str, Any]) -> dict[str, Any]:
    responses = item.get("responses")
    sole_response = (
        responses[0]
        if isinstance(responses, list) and len(responses) == 1 and isinstance(responses[0], dict)
        else {}
    )
    return {
        "lms_response_observations": historical_response_observations(item),
        "lms_response_observations_complete": (
            item.get("responses_complete") is True and not _source_omissions(item)
        ),
        "lms_module": str(item.get("module", "")).removeprefix("mod_").lower()[:16],
        "lms_cmid": str(item.get("cmid", ""))[:64],
        "moodle_parent_attempt_id": str(
            item.get("moodle_parent_attempt_id") or item.get("attempt_id") or ""
        )[:160],
        "moodle_response_id": str(
            item.get("moodle_response_id") or sole_response.get("response_id") or ""
        )[:255],
    }


def _historical_source_materialization_is_current(mapping: ExternalMapping) -> bool:
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    value = metadata.get("historical_source_materialization_version")
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= HISTORICAL_SOURCE_MATERIALIZATION_VERSION
    )


@dataclass(frozen=True, slots=True)
class MoodleHistoryImportStats:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0

    def add(self, outcome: str) -> MoodleHistoryImportStats:
        values = {
            "created": self.created,
            "updated": self.updated,
            "unchanged": self.unchanged,
            "skipped": self.skipped,
        }
        values[outcome] += 1
        return MoodleHistoryImportStats(**values)

    def merge(self, other: MoodleHistoryImportStats) -> MoodleHistoryImportStats:
        return MoodleHistoryImportStats(
            created=self.created + other.created,
            updated=self.updated + other.updated,
            unchanged=self.unchanged + other.unchanged,
            skipped=self.skipped + other.skipped,
        )


def _safe_filename(value: object, fallback: str) -> str:
    raw = PurePosixPath(str(value or "").replace("\\", "/")).name.strip()
    raw = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", raw)[:180]
    return raw if raw and raw not in {".", ".."} else fallback


def _unique_path(candidate: str, occupied: set[str]) -> str:
    path = validate_source_path(candidate)
    if path not in occupied:
        occupied.add(path)
        return path
    source = PurePosixPath(path)
    for index in range(2, 10_000):
        candidate = str(source.with_name(f"{source.stem}-{index}{source.suffix}"))
        if candidate not in occupied:
            occupied.add(candidate)
            return candidate
    raise ValueError("too many duplicate Moodle source filenames")


def _decode_artifact(raw: dict[str, Any]) -> bytes | None:
    if raw.get("downloaded") is False:
        return None
    encoded = raw.get("content_base64")
    if not isinstance(encoded, str) or not encoded:
        return None
    try:
        content = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    if len(content) > _MAX_ARCHIVE_BYTES:
        return None
    expected_size = raw.get("size_bytes")
    if isinstance(expected_size, int) and not isinstance(expected_size, bool):
        if expected_size != len(content):
            return None
    expected_hash = raw.get("sha256")
    if isinstance(expected_hash, str) and expected_hash:
        if not re.fullmatch(r"[a-f0-9]{64}", expected_hash.lower()):
            return None
        if hashlib.sha256(content).hexdigest() != expected_hash.lower():
            return None
    return content


def _decode_text(content: bytes) -> str | None:
    if len(content) > _MAX_FILE_BYTES or b"\x00" in content:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return content.decode("cp1251")
        except UnicodeDecodeError:
            return None


def _archive_sources(content: bytes, occupied: set[str]) -> list[tuple[str, str]]:
    if len(content) > _MAX_ARCHIVE_BYTES:
        return []
    try:
        archive = zipfile.ZipFile(io.BytesIO(content))
    except (OSError, zipfile.BadZipFile):
        return []
    result: list[tuple[str, str]] = []
    reserved = set(occupied)
    total = 0
    with archive:
        members = [item for item in archive.infolist() if not item.is_dir()]
        # Keep archive traversal bounded even when most entries are unrelated
        # binaries.  Only supported text members consume workspace file slots.
        if len(members) > _MAX_FILES * 8:
            return []
        for member in members:
            source = PurePosixPath(member.filename.replace("\\", "/"))
            unix_mode = member.external_attr >> 16
            if (
                source.is_absolute()
                or any(part in {"", ".", ".."} for part in source.parts)
                or source.suffix.lower() not in _SOURCE_SUFFIXES
                or (unix_mode & 0o170000) == 0o120000
                or member.file_size < 0
                or member.file_size > _MAX_FILE_BYTES
            ):
                continue
            if len(result) >= _MAX_FILES - len(occupied):
                return []
            total += member.file_size
            if total > _MAX_TOTAL_BYTES:
                return []
            try:
                data = archive.read(member)
            except (OSError, RuntimeError, zipfile.BadZipFile):
                continue
            text = _decode_text(data)
            if text is None:
                continue
            try:
                # Preserve the student's project layout while validating that
                # every member remains a relative path inside the workspace.
                path = _unique_path(str(source), reserved)
            except Exception:
                continue
            result.append((path, text))
    occupied.update(reserved)
    return result


def _safe_archive_source(
    raw_name: object,
    raw_size: object,
) -> tuple[PurePosixPath, int] | None:
    """Validate an archive member before any extraction touches the filesystem."""

    source = PurePosixPath(str(raw_name or "").replace("\\", "/"))
    try:
        size = int(raw_size)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        source.is_absolute()
        or not source.parts
        or any(part in {"", ".", ".."} for part in source.parts)
        or source.suffix.lower() not in _SOURCE_SUFFIXES
        or size < 0
        or size > _MAX_FILE_BYTES
    ):
        return None
    try:
        validate_source_path(str(source))
    except Exception:
        return None
    return source, size


class _Bounded7zBuffer(_Py7zIO):  # type: ignore[misc,valid-type]
    """In-memory py7zr target that refuses writes beyond the declared member."""

    def __init__(self, maximum_bytes: int) -> None:
        self._maximum_bytes = maximum_bytes
        self._buffer = io.BytesIO()

    def write(self, data: bytes | bytearray) -> int:
        final_size = max(len(self._buffer.getbuffer()), self._buffer.tell() + len(data))
        if final_size > self._maximum_bytes:
            raise ValueError("7z member exceeded its declared bounded size")
        return self._buffer.write(data)

    def read(self, size: int | None = None) -> bytes:
        return self._buffer.read(-1 if size is None else size)

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._buffer.seek(offset, whence)

    def flush(self) -> None:
        return None

    def size(self) -> int:
        return len(self._buffer.getbuffer())

    def value(self) -> bytes:
        return self._buffer.getvalue()


class _Bounded7zFactory(_WriterFactory):  # type: ignore[misc,valid-type]
    def __init__(self, member_sizes: dict[str, int]) -> None:
        self._member_sizes = member_sizes
        self.products: dict[str, _Bounded7zBuffer] = {}

    def create(self, filename: str) -> _Bounded7zBuffer:
        if filename not in self._member_sizes or filename in self.products:
            raise ValueError("7z extractor requested an unexpected or duplicate member")
        product = _Bounded7zBuffer(self._member_sizes[filename])
        self.products[filename] = product
        return product


def _seven_zip_sources(
    content: bytes,
    occupied: set[str],
) -> tuple[list[tuple[str, str]], str | None]:
    """Read supported text members from a 7z archive within workspace limits.

    py7zr is deliberately optional at import time so rolling deployments from
    an older image do not silently turn an archive into a blank program.  Such
    an image receives a visible diagnostic file and an incomplete-source
    receipt until it is rebuilt with the declared dependency.
    """

    if len(content) > _MAX_ARCHIVE_BYTES:
        return [], "ARCHIVE_TOO_LARGE"
    if _py7zr is None:
        return [], "SEVEN_ZIP_SUPPORT_UNAVAILABLE"

    reserved = set(occupied)
    try:
        with _py7zr.SevenZipFile(io.BytesIO(content), mode="r") as archive:
            members = list(archive.list())
            if len(members) > _MAX_FILES * 8:
                return [], "ARCHIVE_MEMBER_LIMIT"
            candidates: list[tuple[PurePosixPath, int]] = []
            seen_members: set[str] = set()
            total = 0
            for member in members:
                if any(
                    bool(getattr(member, marker, False))
                    for marker in (
                        "is_directory",
                        "is_symlink",
                        "is_junction",
                        "is_socket",
                    )
                ):
                    continue
                validated = _safe_archive_source(
                    getattr(member, "filename", ""),
                    getattr(member, "uncompressed", -1),
                )
                if validated is None:
                    continue
                source, size = validated
                source_name = str(source)
                # Duplicate archive paths are ambiguous and extraction tools
                # disagree about which entry wins; reject rather than guess.
                if source_name in seen_members:
                    return [], "ARCHIVE_DUPLICATE_PATH"
                seen_members.add(source_name)
                if len(candidates) >= _MAX_FILES - len(occupied):
                    return [], "ARCHIVE_SOURCE_FILE_LIMIT"
                total += size
                if total > _MAX_TOTAL_BYTES:
                    return [], "ARCHIVE_EXPANDED_SIZE_LIMIT"
                candidates.append((source, size))
            if not candidates:
                return [], "ARCHIVE_HAS_NO_SUPPORTED_SOURCE_FILES"

            targets = [str(source) for source, _size in candidates]
            factory = _Bounded7zFactory({str(source): size for source, size in candidates})
            # WriterFactory keeps every byte in bounded memory. Archive paths,
            # links and special-file metadata therefore never reach the host
            # filesystem, even before post-extraction validation.
            archive.extract(targets=targets, factory=factory)
            result: list[tuple[str, str]] = []
            actual_total = 0
            for source, declared_size in candidates:
                extracted = factory.products.get(str(source))
                if extracted is None or extracted.size() != declared_size:
                    return [], "ARCHIVE_EXTRACTION_INCOMPLETE"
                data = extracted.value()
                actual_total += len(data)
                if actual_total > _MAX_TOTAL_BYTES:
                    return [], "ARCHIVE_EXPANDED_SIZE_LIMIT"
                text = _decode_text(data)
                if text is None:
                    continue
                path = _unique_path(str(source), reserved)
                result.append((path, text))
    except Exception:
        return [], "ARCHIVE_INVALID_OR_ENCRYPTED"
    if not result:
        return [], "ARCHIVE_HAS_NO_READABLE_SOURCE_FILES"
    occupied.update(reserved)
    return result, None


def _unavailable_source_files(
    *,
    path: str = "moodle-import.txt",
    content: str = "Исходный файл ответа Moodle не был доступен при импорте.\n",
) -> list[dict[str, str]]:
    return [
        {
            "path": path,
            "language": language_for_path(path),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
    ]


def historical_source_files(
    item: dict[str, Any],
    *,
    import_omissions: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Convert a bounded Moodle response into safe text files for a snapshot."""

    responses = item.get("responses")
    if not isinstance(responses, list):
        responses = []
    occupied: set[str] = set()
    sources: list[tuple[str, str]] = []
    archive_failures: list[tuple[str, str]] = []
    total = 0
    for response_index, raw_response in enumerate(responses, start=1):
        if not isinstance(raw_response, dict):
            continue
        response_source_start = len(sources)
        artifacts = raw_response.get("artifacts")
        if isinstance(artifacts, list):
            for artifact_index, raw_artifact in enumerate(artifacts, start=1):
                if not isinstance(raw_artifact, dict):
                    continue
                content = _decode_artifact(raw_artifact)
                if content is None:
                    continue
                filename = _safe_filename(
                    raw_artifact.get("filename"),
                    f"response-{response_index}-{artifact_index}.cpp",
                )
                suffix = PurePosixPath(filename).suffix.lower()
                if suffix == ".zip":
                    extracted = _archive_sources(content, occupied)
                    sources.extend(extracted)
                    total += sum(len(text.encode("utf-8")) for _path, text in extracted)
                    continue
                if suffix == ".7z":
                    extracted, failure = _seven_zip_sources(content, occupied)
                    sources.extend(extracted)
                    total += sum(len(text.encode("utf-8")) for _path, text in extracted)
                    if failure is not None:
                        archive_failures.append((filename, failure))
                        if import_omissions is not None:
                            import_omissions.append(
                                {
                                    "kind": "ATTACHMENT_ARCHIVE",
                                    "response_id": str(
                                        raw_response.get("response_id", response_index)
                                    )[:255],
                                    "filename": filename,
                                    "reason": failure,
                                }
                            )
                    continue
                if suffix not in _SOURCE_SUFFIXES:
                    continue
                text = _decode_text(content)
                if text is None:
                    continue
                try:
                    path = _unique_path(filename, occupied)
                except Exception:
                    continue
                total += len(text.encode("utf-8"))
                sources.append((path, text))
        answer = raw_response.get("answer_text")
        # Online text and file submissions are independent Moodle plugins.  A
        # teacher may enable both and a student may provide meaningful content
        # in each, so an attachment must never make the inline response vanish.
        if isinstance(answer, str) and answer.strip():
            answer_bytes = len(answer.encode("utf-8"))
            if answer_bytes <= _MAX_FILE_BYTES:
                if len(sources) == response_source_start:
                    fallback = (
                        "main.cpp" if len(responses) == 1 else f"response-{response_index}.cpp"
                    )
                else:
                    fallback = (
                        "moodle-online-text.cpp"
                        if len(responses) == 1
                        else f"response-{response_index}-online-text.cpp"
                    )
                try:
                    path = _unique_path(fallback, occupied)
                except Exception:
                    continue
                total += answer_bytes
                # Keep indentation, tabs, blank lines, trailing spaces and the
                # final-newline choice exactly as Moodle supplied them.
                sources.append((path, answer))
        if len(sources) > _MAX_FILES or total > _MAX_TOTAL_BYTES:
            return _unavailable_source_files()
    if not sources:
        if archive_failures:
            filename, reason = archive_failures[0]
            return _unavailable_source_files(
                path="moodle-archive-import-error.txt",
                content=(
                    f"Архив Moodle {filename} не удалось импортировать.\n"
                    f"Состояние импорта: {reason}.\n"
                    "Исходный архив сохранён в Moodle; повторите синхронизацию после "
                    "устранения причины.\n"
                ),
            )
        # Keep the submission visible even when Moodle omitted/blocked an
        # attachment.  The explicit text prevents an empty editor from looking
        # like a student's blank submission.
        return _unavailable_source_files()
    return [
        {
            "path": path,
            "language": language_for_path(path),
            "content": content,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        }
        for path, content in sources
    ]


def _external_timestamp(value: object) -> datetime:
    if isinstance(value, bool):
        return utcnow()
    try:
        timestamp = int(value or 0)
        if timestamp > 0:
            return datetime.fromtimestamp(timestamp, UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return utcnow()


def _source_omissions(item: dict[str, Any]) -> list[dict[str, str]]:
    """Return bounded provenance for answers/files Moodle could not transfer."""

    omissions: list[dict[str, str]] = []
    responses = item.get("responses")
    if not isinstance(responses, list):
        return omissions
    for response_index, response in enumerate(responses, start=1):
        if not isinstance(response, dict):
            continue
        response_id = str(response.get("response_id", response_index))[:255]
        if response.get("answer_complete") is False:
            omissions.append(
                {
                    "kind": "INLINE_ANSWER",
                    "response_id": response_id,
                    "reason": str(response.get("answer_omission_reason") or "UNAVAILABLE")[:255],
                }
            )
        artifacts = response.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts:
            if not isinstance(artifact, dict) or artifact.get("downloaded") is not False:
                continue
            omissions.append(
                {
                    "kind": "ATTACHMENT",
                    "response_id": response_id,
                    "filename": _safe_filename(artifact.get("filename"), "moodle-file"),
                    "reason": str(artifact.get("omission_reason") or "UNAVAILABLE")[:255],
                }
            )
            if len(omissions) >= 128:
                return omissions
    return omissions


def _actor_subjects(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for raw in value:
        subject = str(raw).strip()[:255]
        if subject and subject not in result:
            result.append(subject)
        if len(result) >= _MAX_IMPORT_ACTORS:
            break
    return result


def _record_import_actor(
    *,
    submission: Submission | None,
    mapping: ExternalMapping,
    actor_external_subject: str,
) -> None:
    """Record bounded import provenance for audit; this is never an ACL."""

    actor = actor_external_subject.strip()[:255]
    if not actor:
        return
    metadata = dict(mapping.metadata_json or {})
    mapping_actors = _actor_subjects(metadata.get("actor_external_subjects"))
    if actor not in mapping_actors and len(mapping_actors) < _MAX_IMPORT_ACTORS:
        mapping_actors.append(actor)
    metadata["actor_external_subjects"] = mapping_actors
    mapping.metadata_json = metadata
    if submission is None:
        return
    receipt = dict(submission.external_receipt or {})
    submission_actors = _actor_subjects(receipt.get("actor_external_subjects"))
    if actor not in submission_actors and len(submission_actors) < _MAX_IMPORT_ACTORS:
        submission_actors.append(actor)
    receipt["actor_external_subjects"] = submission_actors
    submission.external_receipt = receipt


def _imported_reviewer_name(item: dict[str, Any]) -> str:
    responses = item.get("responses")
    if not isinstance(responses, list):
        return ""
    for response in responses[:32]:
        if not isinstance(response, dict):
            continue
        name = " ".join(str(response.get("reviewer_name", "")).split())[:255]
        if name:
            return name
    return ""


async def _import_reviewer(
    db: AsyncSession,
    course: Course,
    item: dict[str, Any],
) -> ExternalPrincipal:
    reviewer_name = _imported_reviewer_name(item)
    if reviewer_name:
        reviewer_digest = hashlib.sha256(reviewer_name.casefold().encode("utf-8")).hexdigest()[:24]
        subject = f"moodle-history-reviewer:{course.external_id}:{reviewer_digest}"[:255]
        display_name = reviewer_name
    else:
        subject = f"moodle-history-reviewer:{course.external_id}"[:255]
        display_name = "Moodle · историческая оценка"
    row = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == course.connection_id,
            ExternalPrincipal.external_subject == subject,
        )
    )
    if row is None:
        row = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject=subject,
            display_name=display_name,
            active=False,
            preferences={
                "system_identity": "MOODLE_HISTORY_REVIEWER",
                "source": "MOODLE_COMMENT_SIGNATURE" if reviewer_name else "MOODLE_HISTORY",
            },
        )
        db.add(row)
        await db.flush()
    elif reviewer_name and row.display_name != display_name:
        row.display_name = display_name
    return row


async def _student_principal(
    db: AsyncSession,
    *,
    course: Course,
    item: dict[str, Any],
) -> ExternalPrincipal | None:
    subject = str(item.get("user_id", "")).strip()
    if not re.fullmatch(r"[1-9][0-9]{0,19}", subject):
        return None
    principal = await db.scalar(
        select(ExternalPrincipal).where(
            ExternalPrincipal.connection_id == course.connection_id,
            ExternalPrincipal.external_subject == subject,
        )
    )
    display_name = str(item.get("display_name", "")).strip()[:255] or f"Студент Moodle {subject}"
    if principal is None:
        principal = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject=subject,
            display_name=display_name,
            active=True,
            preferences={"created_by": "MOODLE_HISTORY_IMPORT"},
        )
        db.add(principal)
        await db.flush()
    elif display_name:
        principal.display_name = display_name
    membership = await db.scalar(
        select(CourseMembership).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == principal.id,
            CourseMembership.role == CourseRole.STUDENT.value,
        )
    )
    teacher_membership = await db.scalar(
        select(CourseMembership.id).where(
            CourseMembership.course_id == course.id,
            CourseMembership.principal_id == principal.id,
            CourseMembership.role == CourseRole.TEACHER.value,
            CourseMembership.active.is_(True),
        )
    )
    # A teacher can make a test attempt in Moodle.  Preserve that attempt under
    # the real principal, but never manufacture a second STUDENT role: the
    # authentication policy intentionally rejects ambiguous active course
    # roles.  System administrators can still inspect the imported test attempt.
    if membership is None and teacher_membership is None:
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=principal.id,
                role=CourseRole.STUDENT.value,
                active=True,
                external_revision=course.external_revision,
            )
        )
    return principal


def _score_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().replace(",", ".")
    # Scores arrive from a browser connector.  Bound the decimal parser input
    # so malformed exponents cannot trigger expensive arithmetic below.
    if not raw or len(raw) > 64:
        return None
    try:
        parsed = Decimal(raw)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _imported_grade(
    value: object,
    remote_maximum: object,
    local_maximum: Decimal,
) -> tuple[Decimal | None, str | None]:
    """Safely project a Moodle score onto the immutable local score scale."""

    grade = _score_decimal(value)
    local_max = _score_decimal(local_maximum)
    if grade is None or grade < 0 or local_max is None or local_max <= 0:
        return None, None
    remote_max = _score_decimal(remote_maximum)
    try:
        if remote_max is not None and remote_max > 0:
            normalized = grade * local_max / remote_max
            method = "PROPORTIONAL_TO_REMOTE_MAX"
        else:
            # Some Moodle views expose a checked grade without the activity
            # maximum.  Keep the work graded, interpret the value on the local
            # scale, and cap it rather than rejecting the historical result.
            normalized = grade
            method = "DIRECT_LOCAL_SCALE"
        if normalized > local_max:
            normalized = local_max
            method = f"{method}_CLAMPED"
        normalized = normalized.quantize(Decimal("0.01"))
    except (DecimalException, ValueError):
        return None, None
    return normalized, method


def _response_grade_provenance(item: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    responses = item.get("responses")
    if not isinstance(responses, list):
        return result
    for response in responses[:32]:
        if not isinstance(response, dict):
            continue
        entry = {
            "response_id": str(response.get("response_id", ""))[:255],
            "question_text": str(response.get("question_text", ""))[:2_000],
            "grade": response.get("grade"),
            "grade_max": response.get("grade_max"),
            "comment": str(response.get("comment", ""))[:20_000],
        }
        reviewer_name = " ".join(str(response.get("reviewer_name", "")).split())[:255]
        if reviewer_name:
            entry["reviewer_name"] = reviewer_name
        result.append(entry)
    return result


async def _sync_imported_decision(
    db: AsyncSession,
    *,
    submission: Submission,
    assessment: Assessment,
    course: Course,
    item: dict[str, Any],
) -> None:
    grade, normalization = _imported_grade(
        item.get("grade"),
        item.get("grade_max"),
        assessment.max_score,
    )
    imported = list(
        (
            await db.scalars(
                select(ReviewDecision)
                .where(ReviewDecision.submission_id == submission.id)
                .order_by(ReviewDecision.revision.desc())
            )
        ).all()
    )
    latest = imported[0] if imported else None
    # Never overwrite a teacher's local re-check with a later Moodle refresh.
    if latest is not None and latest.lms_export_state != "IMPORTED":
        return
    if grade is None:
        if latest is not None:
            latest.status = "SUPERSEDED"
        return
    comment = str(item.get("comment", ""))[:50_000]
    criterion_scores = {
        "source": "MOODLE_HISTORY",
        # Preserve the remote scale verbatim for auditability.  The local
        # assessment remains immutable; only the imported decision is scaled.
        "remote_grade": item.get("grade"),
        "remote_grade_max": item.get("grade_max"),
        "local_grade": format(grade, "f"),
        "local_grade_max": format(assessment.max_score, "f"),
        "normalization": normalization,
        "responses": _response_grade_provenance(item),
    }
    if (
        latest is not None
        and latest.grade == grade
        and latest.comment == comment
        and latest.criterion_scores == criterion_scores
    ):
        return
    if latest is not None:
        latest.status = "SUPERSEDED"
    reviewer = await _import_reviewer(db, course, item)
    db.add(
        ReviewDecision(
            submission_id=submission.id,
            reviewer_id=reviewer.id,
            revision=(latest.revision + 1) if latest else 1,
            grade=grade,
            comment=comment,
            criterion_scores=criterion_scores,
            evidence_ids=[],
            status="APPLIED",
            supersedes_id=latest.id if latest else None,
            lms_export_state="IMPORTED",
        )
    )


def _question_score(response: dict[str, Any], fallback: Decimal) -> Decimal:
    value = _score_decimal(response.get("grade_max"))
    if value is None or value <= 0 or value > Decimal("999999.99"):
        return fallback
    try:
        rounded = value.quantize(Decimal("0.01"))
        # Match native Quiz workspaces: a positive Moodle mark smaller than
        # local two-decimal precision still needs a nonzero grading scale.
        # _imported_grade keeps the remote maximum and scales the received mark
        # proportionally, so this must not manufacture a full-score decision.
        return rounded if rounded > 0 else Decimal("1.00")
    except DecimalException:
        return fallback


def _quiz_question_identity(cmid: int, response_id: str) -> tuple[str, str]:
    digest = hashlib.sha256(response_id.encode("utf-8")).hexdigest()
    return f"quiz:{cmid}:essay:{digest[:24]}", digest


def _quiz_response_submission(
    item: dict[str, Any], response: dict[str, Any], *, position: int
) -> dict[str, Any]:
    """Create the stable, public projection for one Essay inside a Quiz attempt."""

    response_id = str(response.get("response_id", "")).strip()[:255]
    attempt_id = str(item.get("attempt_id", "")).strip()[:160]
    cmid = int(item.get("cmid", 0))
    _question_external_id, response_digest = _quiz_question_identity(cmid, response_id)
    split = {
        **item,
        "attempt_id": f"{attempt_id[:120]}:essay:{response_digest[:16]}",
        "external_id": f"quiz:{cmid}:{attempt_id[:160]}:essay:{response_digest[:16]}",
        "grade": response.get("grade"),
        "grade_max": response.get("grade_max"),
        "comment": str(response.get("comment", ""))[:20_000],
        "responses": [response],
        "moodle_parent_external_id": str(item.get("external_id", ""))[:255],
        "moodle_parent_external_revision": str(item.get("external_revision", ""))[:255],
        "moodle_parent_attempt_id": attempt_id,
        "moodle_response_id": response_id,
        "moodle_response_position": position,
    }
    # The revision is intentionally per question.  A changed answer, question,
    # grade or comment updates only that local submission on the next sync.
    split["external_revision"] = canonical_hash(
        {
            "schema": "moodle-quiz-essay-split-v1",
            "parent_external_id": split["moodle_parent_external_id"],
            "response": response,
        }
    )
    return split


def _question_task_content(version: TaskVersion) -> dict[str, Any]:
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


async def _available_question_slug(
    db: AsyncSession,
    *,
    course_id: uuid.UUID,
    cmid: int,
    digest: str,
) -> str:
    prefix = f"moodle-quiz-{cmid}-essay-{digest[:12]}"[:150]
    for index in range(1, 101):
        candidate = prefix if index == 1 else f"{prefix}-{index}"
        occupied = await db.scalar(
            select(TaskBankItem.id).where(
                TaskBankItem.course_id == course_id,
                TaskBankItem.slug == candidate,
            )
        )
        if occupied is None:
            return candidate
    return f"{prefix[:140]}-{uuid.uuid4().hex[:12]}"


async def _quiz_activity_mapping(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
) -> ExternalMapping | None:
    return await db.scalar(
        select(ExternalMapping).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.local_id == assessment.id,
            ExternalMapping.external_type.in_(
                ["mod_quiz", "quiz", "moodle_quiz", "moodle_mod_quiz"]
            ),
        )
    )


async def _quiz_requires_question_split(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
    items: list[dict[str, Any]],
) -> bool:
    quiz_items = [item for item in items if str(item.get("module", "")) == "quiz"]
    if not quiz_items:
        return False
    if any(
        isinstance(item.get("responses"), list) and len(item["responses"]) > 1
        for item in quiz_items
    ):
        return True
    mapping = await _quiz_activity_mapping(db, course=course, assessment=assessment)
    metadata = dict(mapping.metadata_json or {}) if mapping is not None else {}
    activity = metadata.get("activity") if isinstance(metadata.get("activity"), dict) else {}
    essay_count = activity.get("essay_question_count")
    if isinstance(essay_count, int) and not isinstance(essay_count, bool) and essay_count > 1:
        return True
    cmids = {
        int(item["cmid"])
        for item in quiz_items
        if not isinstance(item.get("cmid"), bool) and str(item.get("cmid", "")).isdigit()
    }
    if not cmids:
        return False
    return (
        await db.scalar(
            select(ExternalMapping.id).where(
                ExternalMapping.connection_id == course.connection_id,
                ExternalMapping.external_type == "moodle_quiz_essay_question",
                ExternalMapping.external_id.like(f"quiz:{next(iter(cmids))}:essay:%"),
            )
        )
        is not None
    )


async def _question_task_item(
    db: AsyncSession,
    *,
    mapping: ExternalMapping | None,
    assessment: Assessment | None,
) -> TaskBankItem | None:
    metadata = dict(mapping.metadata_json or {}) if mapping is not None else {}
    raw_item_id = metadata.get("task_item_id")
    try:
        item_id = uuid.UUID(str(raw_item_id))
    except (TypeError, ValueError, AttributeError):
        item_id = None
    item = await db.get(TaskBankItem, item_id) if item_id is not None else None
    if item is not None or assessment is None:
        return item
    return await db.scalar(
        select(TaskBankItem)
        .join(TaskVersion, TaskVersion.item_id == TaskBankItem.id)
        .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
        .where(AssessmentItem.assessment_id == assessment.id)
        .order_by(TaskVersion.number.desc())
    )


async def _ensure_quiz_question_context(
    db: AsyncSession,
    *,
    course: Course,
    parent: Assessment,
    response: dict[str, Any],
    position: int,
    cmid: int,
    multi_file: bool,
) -> tuple[Assessment, TaskVersion]:
    """Return one managed assessment and exact task version per Essay question."""

    response_id = str(response.get("response_id", "")).strip()[:255]
    question_external_id, response_digest = _quiz_question_identity(cmid, response_id)
    statement = str(response.get("question_text", "")).strip()[:50_000]
    if not statement:
        statement = "Условие вопроса не удалось получить из Moodle."
    title = f"{parent.title} · задание {position}"[:255]
    score = _question_score(response, parent.max_score)
    question_revision = canonical_hash(
        {
            "schema": "moodle-quiz-essay-question-v1",
            "response_id": response_id,
            "question_text": statement,
            "grade_max": format(score, "f"),
            "multi_file": multi_file,
        }
    )
    mapping = await db.scalar(
        select(ExternalMapping).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.external_type == "moodle_quiz_essay_question",
            ExternalMapping.external_id == question_external_id,
        )
    )
    child = await db.get(Assessment, mapping.local_id) if mapping is not None else None
    item = await _question_task_item(db, mapping=mapping, assessment=child)
    if item is None:
        item = TaskBankItem(
            scope=TaskScope.COURSE.value,
            course_id=course.id,
            slug=await _available_question_slug(
                db,
                course_id=course.id,
                cmid=cmid,
                digest=response_digest,
            ),
            category="Moodle · исторические задания",
            tags=["moodle-import", "moodle-quiz", "historical", "essay-question"],
            created_by_id=parent.created_by_id,
        )
        db.add(item)
        await db.flush()

    versions = list(
        (
            await db.scalars(
                select(TaskVersion)
                .where(TaskVersion.item_id == item.id)
                .order_by(TaskVersion.number)
            )
        ).all()
    )
    version = next(
        (
            row
            for row in versions
            if isinstance(row.ai_policy, dict)
            and row.ai_policy.get("moodle_question_revision") == question_revision
        ),
        None,
    )
    if version is None:
        version = TaskVersion(
            item_id=item.id,
            number=(versions[-1].number + 1) if versions else 1,
            title=title,
            statement=statement,
            language="CPP",
            language_standard="C++17",
            multi_file=multi_file,
            starter_files=[],
            build_profile=("cpp-gcc-c++20-multi" if multi_file else "cpp-gcc-c++20-single"),
            public_examples=[],
            hidden_test_manifest={},
            max_score=score,
            difficulty="",
            ai_policy={
                "source": "MOODLE_HISTORY_QUIZ_ESSAY",
                "historical_import_only": True,
                "moodle_response_id": response_id,
                "moodle_question_revision": question_revision,
                "multi_file": multi_file,
            },
            content_hash="",
            status=TaskVersionStatus.DRAFT.value,
            authored_by_id=parent.created_by_id,
        )
        version.content_hash = canonical_hash(_question_task_content(version))
        db.add(version)
        await db.flush()

    if child is None:
        policy = dict(parent.policy or {})
        policy.update(
            {
                "historical_import_only": True,
                "moodle_quiz_question_split": True,
                "moodle_parent_assessment_id": str(parent.id),
                "moodle_response_id": response_id,
            }
        )
        child = Assessment(
            course_id=parent.course_id,
            section_id=parent.section_id,
            type=parent.type,
            title=title,
            instructions=statement,
            opens_at=parent.opens_at,
            closes_at=parent.closes_at,
            duration_seconds=parent.duration_seconds,
            attempt_limit=parent.attempt_limit,
            max_score=score,
            paste_policy=parent.paste_policy,
            student_ai_enabled=False,
            teacher_ai_enabled=parent.teacher_ai_enabled,
            review_required=parent.review_required,
            decision_support_enabled=parent.decision_support_enabled,
            autosubmit=parent.autosubmit,
            multi_file=multi_file,
            status=AssessmentStatus.DRAFT.value,
            policy=policy,
            created_by_id=parent.created_by_id,
        )
        db.add(child)
        await db.flush()
    elif child.course_id != course.id:
        raise ValueError("Moodle question mapping points to another course")

    attached = await db.scalar(
        select(AssessmentItem)
        .where(AssessmentItem.assessment_id == child.id)
        .order_by(AssessmentItem.position)
    )
    if attached is None:
        db.add(
            AssessmentItem(
                assessment_id=child.id,
                task_version_id=version.id,
                position=0,
                points=score,
                assignment_rule={},
            )
        )
    else:
        # This selects the most recently observed version for authoring views;
        # each historical Attempt below still points to its exact version.
        attached.task_version_id = version.id
        attached.points = score
    if child.status == AssessmentStatus.DRAFT.value:
        child.title = title
        child.instructions = statement
        child.max_score = score
        child.multi_file = multi_file

    metadata = {
        **(dict(mapping.metadata_json or {}) if mapping is not None else {}),
        "managed_by": "MOODLE_HISTORY_QUIZ_SPLIT",
        "course_id": course.external_id,
        "parent_assessment_id": str(parent.id),
        "module": "quiz",
        "cmid": cmid,
        "response_id": response_id,
        "position": position,
        "multi_file": multi_file,
        "question_revision": question_revision,
        "task_item_id": str(item.id),
        "task_version_id": str(version.id),
    }
    if mapping is None:
        mapping = ExternalMapping(
            connection_id=course.connection_id,
            local_type="MoodleQuizEssayAssessment",
            local_id=child.id,
            external_type="moodle_quiz_essay_question",
            external_id=question_external_id,
            external_revision=question_revision,
            metadata_json=metadata,
        )
        db.add(mapping)
    else:
        mapping.local_id = child.id
        mapping.external_revision = question_revision
        mapping.metadata_json = metadata
    await db.flush()
    return child, version


async def _adopt_legacy_combined_submission(
    db: AsyncSession,
    *,
    course: Course,
    parent: Assessment,
    child: Assessment,
    version: TaskVersion,
    legacy_external_id: str,
    split_external_id: str,
) -> bool:
    """Move the old combined import to the first Essay without duplicating it."""

    legacy = await db.scalar(
        select(ExternalMapping).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.external_type == "moodle_historical_submission",
            ExternalMapping.external_id == legacy_external_id,
        )
    )
    if legacy is None:
        return False
    existing = await db.scalar(
        select(ExternalMapping.id).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.external_type == "moodle_historical_submission",
            ExternalMapping.external_id == split_external_id,
        )
    )
    if existing is not None:
        return False
    submission = await db.get(Submission, legacy.local_id)
    attempt = await db.get(Attempt, submission.attempt_id) if submission is not None else None
    if (
        submission is None
        or attempt is None
        or submission.source != "MOODLE_IMPORT"
        or attempt.assessment_id != parent.id
    ):
        return False
    last_sequence = await db.scalar(
        select(func.max(Attempt.sequence)).where(
            Attempt.assessment_id == child.id,
            Attempt.principal_id == attempt.principal_id,
            Attempt.id != attempt.id,
        )
    )
    attempt.assessment_id = child.id
    attempt.assigned_task_version_id = version.id
    attempt.sequence = int(last_sequence or 0) + 1
    legacy.external_id = split_external_id
    # The mapping and the public provenance receipt are one identity pair.
    # Older aggregate imports stored the parent Quiz attempt id in the receipt;
    # repair it immediately when the row is adopted by the first Essay so no
    # transaction observer can see a split mapping with stale aggregate
    # provenance.
    submission.external_receipt = {
        **dict(submission.external_receipt or {}),
        "external_id": split_external_id,
    }
    # Force the flat materializer to replace the old two-file snapshot with
    # the first response and its question-level grade/comment.
    legacy.external_revision = ""
    metadata = dict(legacy.metadata_json or {})
    metadata["assessment_id"] = str(child.id)
    metadata["migrated_from_combined_quiz_attempt"] = legacy_external_id
    legacy.metadata_json = metadata
    await db.flush()
    return True


async def _retire_legacy_combined_submission(
    db: AsyncSession,
    *,
    course: Course,
    parent: Assessment,
    legacy_external_id: str,
    response_ids: list[str],
) -> None:
    """Hide an already-duplicated aggregate while preserving its audit rows."""

    legacy = await db.scalar(
        select(ExternalMapping).where(
            ExternalMapping.connection_id == course.connection_id,
            ExternalMapping.external_type == "moodle_historical_submission",
            ExternalMapping.external_id == legacy_external_id,
        )
    )
    if legacy is None:
        return
    submission = await db.get(Submission, legacy.local_id)
    attempt = await db.get(Attempt, submission.attempt_id) if submission is not None else None
    if (
        submission is None
        or attempt is None
        or submission.source != "MOODLE_IMPORT"
        or attempt.assessment_id != parent.id
    ):
        return
    attempt.state = AttemptState.VOID.value
    receipt = dict(submission.external_receipt or {})
    receipt.update(
        {
            "superseded_by_quiz_split": True,
            "superseded_response_ids": response_ids[:32],
        }
    )
    submission.external_receipt = receipt
    metadata = dict(legacy.metadata_json or {})
    metadata.update(
        {
            "superseded_by_quiz_split": True,
            "superseded_response_ids": response_ids[:32],
        }
    )
    legacy.metadata_json = metadata
    await db.flush()


async def _quiz_attempt_split_rows(
    db: AsyncSession,
    *,
    course: Course,
    parent: Assessment,
    cmid: int,
    parent_attempt_id: str,
    active_external_ids: set[str],
) -> list[tuple[ExternalMapping, Submission, Attempt, Assessment]]:
    """Return exact per-question imports for one Moodle Quiz attempt.

    Resolve the student from the newly materialized canonical rows, then scan
    that student's imported rows in this course.  Older connector builds used
    a different response identity (DOM id, question number or position), so a
    prefix-only lookup cannot find and retire those duplicate ``задание 1``
    rows.  The exact parent attempt and parent assessment checks below still
    make cleanup fail closed.
    """

    prefix = f"quiz:{cmid}:{parent_attempt_id[:160]}:essay:"
    canonical_rows = list(
        (
            await db.execute(
                select(ExternalMapping, Submission, Attempt, Assessment)
                .join(Submission, Submission.id == ExternalMapping.local_id)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
                .where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.external_type == "moodle_historical_submission",
                    ExternalMapping.external_id.in_(active_external_ids),
                    Submission.source == "MOODLE_IMPORT",
                    Assessment.course_id == course.id,
                )
            )
        ).all()
    )
    principal_ids = {
        attempt.principal_id for _mapping, _submission, attempt, _assessment in canonical_rows
    }
    if not principal_ids:
        # Compatibility fallback for an interrupted transaction in which no
        # active mapping was flushed yet.  This retains the old bounded lookup.
        canonical_rows = list(
            (
                await db.execute(
                    select(ExternalMapping, Submission, Attempt, Assessment)
                    .join(Submission, Submission.id == ExternalMapping.local_id)
                    .join(Attempt, Attempt.id == Submission.attempt_id)
                    .join(Assessment, Assessment.id == Attempt.assessment_id)
                    .where(
                        ExternalMapping.connection_id == course.connection_id,
                        ExternalMapping.external_type == "moodle_historical_submission",
                        ExternalMapping.external_id.startswith(prefix, autoescape=True),
                        Submission.source == "MOODLE_IMPORT",
                        Assessment.course_id == course.id,
                    )
                )
            ).all()
        )
        principal_ids = {
            attempt.principal_id for _mapping, _submission, attempt, _assessment in canonical_rows
        }
    if not principal_ids:
        return []

    rows = list(
        (
            await db.execute(
                select(ExternalMapping, Submission, Attempt, Assessment)
                .join(Submission, Submission.id == ExternalMapping.local_id)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .join(Assessment, Assessment.id == Attempt.assessment_id)
                .where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.external_type == "moodle_historical_submission",
                    Submission.source == "MOODLE_IMPORT",
                    Attempt.principal_id.in_(principal_ids),
                    Assessment.course_id == course.id,
                )
            )
        ).all()
    )
    exact: list[tuple[ExternalMapping, Submission, Attempt, Assessment]] = []
    for mapping, submission, attempt, assessment in rows:
        receipt = dict(submission.external_receipt or {})
        policy = dict(assessment.policy or {})
        if str(receipt.get("moodle_parent_attempt_id", "")) != parent_attempt_id or str(
            policy.get("moodle_parent_assessment_id", "")
        ) != str(parent.id):
            continue
        exact.append((mapping, submission, attempt, assessment))
    return exact


def _reactivate_retired_quiz_response(
    *,
    mapping: ExternalMapping,
    submission: Submission,
    attempt: Attempt,
) -> None:
    """Restore a response that reappeared in an authoritative Moodle view."""

    receipt = dict(submission.external_receipt or {})
    if receipt.get("retired_by_authoritative_quiz_refresh") is not True:
        return
    attempt.state = AttemptState.SUBMITTED.value
    for key in (
        "retired_by_authoritative_quiz_refresh",
        "retired_at",
        "retired_parent_external_revision",
    ):
        receipt.pop(key, None)
    receipt["active_in_parent_attempt"] = True
    submission.external_receipt = receipt
    metadata = dict(mapping.metadata_json or {})
    for key in (
        "retired_by_authoritative_quiz_refresh",
        "retired_at",
        "retired_parent_external_revision",
    ):
        metadata.pop(key, None)
    metadata["active_in_parent_attempt"] = True
    mapping.metadata_json = metadata


async def _retire_stale_quiz_response_submissions(
    db: AsyncSession,
    *,
    course: Course,
    parent: Assessment,
    item: dict[str, Any],
    cmid: int,
    active_external_ids: set[str],
) -> None:
    """VOID missing question children only after an authoritative full crawl.

    ``responses_complete`` is emitted by the browser connector only after all
    pages of the attempt were visited without a navigation/markup/limit error.
    Missing or false is intentionally non-authoritative, including during a
    rolling deployment with an older connector, and must never hide data.
    """

    if item.get("responses_complete") is not True:
        return
    parent_attempt_id = str(item.get("attempt_id", "")).strip()[:160]
    parent_external_id = str(item.get("external_id", "")).strip()[:255]
    if not parent_attempt_id or not parent_external_id:
        return
    rows = await _quiz_attempt_split_rows(
        db,
        course=course,
        parent=parent,
        cmid=cmid,
        parent_attempt_id=parent_attempt_id,
        active_external_ids=active_external_ids,
    )
    retired_at = utcnow().isoformat()
    parent_revision = str(item.get("external_revision", ""))[:255]
    for mapping, submission, attempt, _assessment in rows:
        if mapping.external_id in active_external_ids:
            continue
        attempt.state = AttemptState.VOID.value
        receipt = dict(submission.external_receipt or {})
        receipt.update(
            {
                "active_in_parent_attempt": False,
                "retired_by_authoritative_quiz_refresh": True,
                "retired_at": retired_at,
                "retired_parent_external_revision": parent_revision,
            }
        )
        submission.external_receipt = receipt
        metadata = dict(mapping.metadata_json or {})
        metadata.update(
            {
                "active_in_parent_attempt": False,
                "retired_by_authoritative_quiz_refresh": True,
                "retired_at": retired_at,
                "retired_parent_external_revision": parent_revision,
            }
        )
        mapping.metadata_json = metadata


async def _mark_managed_quiz_as_split_container(
    db: AsyncSession,
    *,
    course: Course,
    parent: Assessment,
) -> None:
    mapping = await _quiz_activity_mapping(db, course=course, assessment=parent)
    metadata = dict(mapping.metadata_json or {}) if mapping is not None else {}
    if metadata.get("managed_by") != "MOODLE_ACTIVITY_IMPORT":
        return
    was_legacy_container = metadata.get("historical_quiz_split_container") is True
    policy = dict(parent.policy or {})
    policy["historical_quiz_split_container"] = True
    parent.policy = policy
    metadata["historical_quiz_split_container"] = True
    mapping.metadata_json = metadata
    if parent.status != AssessmentStatus.DRAFT.value:
        return
    attached = (
        await db.execute(
            select(AssessmentItem, TaskVersion, TaskBankItem)
            .join(TaskVersion, TaskVersion.id == AssessmentItem.task_version_id)
            .join(TaskBankItem, TaskBankItem.id == TaskVersion.item_id)
            .where(AssessmentItem.assessment_id == parent.id)
        )
    ).first()
    if attached is not None:
        _assessment_item, task_version, task_item = attached
        # Splitting historical responses must not archive the launchable work.
        # Repair drafts archived by the old split-only representation.
        if was_legacy_container and task_version.status == TaskVersionStatus.DRAFT.value:
            task_item.archived_at = None


async def _materialize_flat_historical_submissions(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
    actor_external_subject: str,
    items: list[dict[str, Any]],
    attached_version: TaskVersion | None = None,
) -> MoodleHistoryImportStats:
    """Idempotently project read-only Moodle attempts into the review domain."""

    actor_external_subject = actor_external_subject.strip()[:255]
    if not actor_external_subject:
        return MoodleHistoryImportStats(skipped=len(items))
    if attached_version is None:
        attached_version = await db.scalar(
            select(TaskVersion)
            .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
            .where(AssessmentItem.assessment_id == assessment.id)
            .order_by(AssessmentItem.position)
        )
    if attached_version is None:
        return MoodleHistoryImportStats(skipped=len(items))
    stats = MoodleHistoryImportStats()
    for item in items:
        if not isinstance(item, dict):
            stats = stats.add("skipped")
            continue
        if str(item.get("state", "")).upper() == "IN_PROGRESS":
            # Moodle drafts are not submitted student works and must not enter
            # the teacher's review queue.
            stats = stats.add("skipped")
            continue
        external_id = str(item.get("external_id", "")).strip()[:255]
        external_revision = str(item.get("external_revision", "")).strip()[:255]
        if not external_id or not external_revision:
            stats = stats.add("skipped")
            continue
        principal = await _student_principal(db, course=course, item=item)
        if principal is None:
            stats = stats.add("skipped")
            continue
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.connection_id == course.connection_id,
                ExternalMapping.external_type == "moodle_historical_submission",
                ExternalMapping.external_id == external_id,
            )
        )
        if (
            mapping is not None
            and mapping.external_revision == external_revision
            and _historical_source_materialization_is_current(mapping)
        ):
            # Re-evaluate the imported decision even for an unchanged Moodle
            # revision.  This repairs attempts that an older importer left
            # ungraded when the remote and local score scales differed.
            submission = await db.get(Submission, mapping.local_id)
            if submission is not None:
                mapped_attempt = await db.get(Attempt, submission.attempt_id)
                if mapped_attempt is not None:
                    _reactivate_retired_quiz_response(
                        mapping=mapping,
                        submission=submission,
                        attempt=mapped_attempt,
                    )
                submission.external_receipt = {
                    **dict(submission.external_receipt or {}),
                    **_origin_receipt_fields(item),
                    # Also serves as an idempotent repair for databases written
                    # by the pre-split importer (or an interrupted migration).
                    "external_id": external_id,
                    "moodle_parent_external_id": item.get("moodle_parent_external_id"),
                    "moodle_parent_external_revision": item.get("moodle_parent_external_revision"),
                    "moodle_response_position": item.get("moodle_response_position"),
                    "historical_source_materialization_version": (
                        HISTORICAL_SOURCE_MATERIALIZATION_VERSION
                    ),
                }
                mapping_metadata = dict(mapping.metadata_json or {})
                mapping_metadata.update(
                    {
                        "assessment_id": str(assessment.id),
                        "moodle_parent_external_id": item.get("moodle_parent_external_id"),
                        "moodle_parent_external_revision": item.get(
                            "moodle_parent_external_revision"
                        ),
                        "moodle_parent_attempt_id": str(
                            item.get("moodle_parent_attempt_id") or item.get("attempt_id") or ""
                        )[:160],
                        "moodle_response_id": item.get("moodle_response_id"),
                        "moodle_response_position": item.get("moodle_response_position"),
                        "historical_source_materialization_version": (
                            HISTORICAL_SOURCE_MATERIALIZATION_VERSION
                        ),
                    }
                )
                mapping.metadata_json = mapping_metadata
                _record_import_actor(
                    submission=submission,
                    mapping=mapping,
                    actor_external_subject=actor_external_subject,
                )
                await _sync_imported_decision(
                    db,
                    submission=submission,
                    assessment=assessment,
                    course=course,
                    item=item,
                )
            stats = stats.add("unchanged")
            continue
        conversion_omissions: list[dict[str, str]] = []
        files = historical_source_files(item, import_omissions=conversion_omissions)
        source_omissions = [*_source_omissions(item), *conversion_omissions]
        if files and files[0]["path"] == "moodle-import.txt" and not source_omissions:
            source_omissions.append(
                {
                    "kind": "WORKSPACE",
                    "response_id": "",
                    "reason": "SOURCE_UNAVAILABLE_OR_EXCEEDS_LOCAL_LIMIT",
                }
            )
        submitted_at = _external_timestamp(item.get("submitted_at_epoch"))
        if mapping is None:
            last_sequence = await db.scalar(
                select(func.max(Attempt.sequence)).where(
                    Attempt.assessment_id == assessment.id,
                    Attempt.principal_id == principal.id,
                )
            )
            attempt = Attempt(
                assessment_id=assessment.id,
                assigned_task_version_id=attached_version.id,
                principal_id=principal.id,
                sequence=int(last_sequence or 0) + 1,
                state=AttemptState.SUBMITTED.value,
                started_at=submitted_at,
                current_revision=0,
                submitted_at=submitted_at,
                submission_source="MOODLE_IMPORT",
                integrity_policy={
                    "paste_policy": assessment.paste_policy,
                    "variant_locked": True,
                    "history_available": False,
                    "source": "MOODLE_HISTORY",
                },
            )
            db.add(attempt)
            await db.flush()
            workspace = Workspace(
                attempt_id=attempt.id,
                current_revision=0,
                multi_file=len(files) > 1,
            )
            db.add(workspace)
            await db.flush()
            for raw_file in files:
                db.add(
                    WorkspaceFile(
                        workspace_id=workspace.id,
                        path=raw_file["path"],
                        language=raw_file["language"],
                        content=raw_file["content"],
                        content_hash=raw_file["content_hash"],
                        created_revision=0,
                    )
                )
            await db.flush()
            snapshot_files = list(
                (
                    await db.scalars(
                        select(WorkspaceFile)
                        .where(WorkspaceFile.workspace_id == workspace.id)
                        .order_by(WorkspaceFile.path)
                    )
                ).all()
            )
            payload = [
                {
                    "id": str(row.id),
                    "path": row.path,
                    "language": row.language,
                    "content": row.content,
                    "content_hash": row.content_hash,
                }
                for row in snapshot_files
            ]
            workspace.aggregate_size = sum(
                len(row.content.encode("utf-8")) for row in snapshot_files
            )
            workspace.current_hash = canonical_hash(
                [
                    {"id": str(row.id), "path": row.path, "hash": row.content_hash}
                    for row in snapshot_files
                ]
            )
            snapshot = Snapshot(
                workspace_id=workspace.id,
                revision=0,
                event_chain_head="",
                manifest_hash=canonical_hash(payload),
                files=payload,
                reason="LMS_IMPORT",
            )
            db.add(snapshot)
            await db.flush()
            submission = Submission(
                attempt_id=attempt.id,
                snapshot_id=snapshot.id,
                revision=1,
                source="MOODLE_IMPORT",
                submitted_at=submitted_at,
                late=False,
                lms_export_state="IMPORTED",
                external_receipt={
                    "source": "MOODLE_HISTORY",
                    **_origin_receipt_fields(item),
                    "external_id": external_id,
                    "external_revision": external_revision,
                    "moodle_parent_external_id": item.get("moodle_parent_external_id"),
                    "moodle_parent_external_revision": item.get("moodle_parent_external_revision"),
                    "moodle_response_position": item.get("moodle_response_position"),
                    "has_edit_history": False,
                    "source_complete": not source_omissions,
                    "historical_source_materialization_version": (
                        HISTORICAL_SOURCE_MATERIALIZATION_VERSION
                    ),
                    "source_omissions": source_omissions,
                    "state": item.get("state"),
                    "actor_external_subjects": [actor_external_subject],
                },
            )
            db.add(submission)
            await db.flush()
            mapping = ExternalMapping(
                connection_id=course.connection_id,
                local_type="Submission",
                local_id=submission.id,
                external_type="moodle_historical_submission",
                external_id=external_id,
                external_revision=external_revision,
                metadata_json={
                    "course_id": course.external_id,
                    "assessment_id": str(assessment.id),
                    "module": item.get("module"),
                    "cmid": item.get("cmid"),
                    "moodle_parent_external_id": item.get("moodle_parent_external_id"),
                    "moodle_parent_external_revision": item.get("moodle_parent_external_revision"),
                    "moodle_parent_attempt_id": str(
                        item.get("moodle_parent_attempt_id") or item.get("attempt_id") or ""
                    )[:160],
                    "moodle_response_id": item.get("moodle_response_id"),
                    "moodle_response_position": item.get("moodle_response_position"),
                    "has_edit_history": False,
                    "source_complete": not source_omissions,
                    "historical_source_materialization_version": (
                        HISTORICAL_SOURCE_MATERIALIZATION_VERSION
                    ),
                    "actor_external_subjects": [actor_external_subject],
                },
            )
            db.add(mapping)
            stats = stats.add("created")
        else:
            submission = await db.get(Submission, mapping.local_id)
            attempt = await db.get(Attempt, submission.attempt_id) if submission else None
            workspace = (
                await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
                if attempt is not None
                else None
            )
            if submission is None or attempt is None or workspace is None:
                stats = stats.add("skipped")
                continue
            _reactivate_retired_quiz_response(
                mapping=mapping,
                submission=submission,
                attempt=attempt,
            )
            attempt.assessment_id = assessment.id
            attempt.assigned_task_version_id = attached_version.id
            # Historical imports have no event chain.  A refreshed remote
            # revision replaces only the currently presented immutable snapshot.
            next_revision = workspace.current_revision + 1
            await db.execute(
                delete(WorkspaceFile).where(WorkspaceFile.workspace_id == workspace.id)
            )
            for raw_file in files:
                db.add(
                    WorkspaceFile(
                        workspace_id=workspace.id,
                        path=raw_file["path"],
                        language=raw_file["language"],
                        content=raw_file["content"],
                        content_hash=raw_file["content_hash"],
                        created_revision=next_revision,
                    )
                )
            await db.flush()
            current_files = list(
                (
                    await db.scalars(
                        select(WorkspaceFile)
                        .where(WorkspaceFile.workspace_id == workspace.id)
                        .order_by(WorkspaceFile.path)
                    )
                ).all()
            )
            payload = [
                {
                    "id": str(row.id),
                    "path": row.path,
                    "language": row.language,
                    "content": row.content,
                    "content_hash": row.content_hash,
                }
                for row in current_files
            ]
            workspace.current_revision = next_revision
            workspace.multi_file = len(files) > 1
            workspace.aggregate_size = sum(
                len(row.content.encode("utf-8")) for row in current_files
            )
            workspace.current_hash = canonical_hash(
                [
                    {"id": str(row.id), "path": row.path, "hash": row.content_hash}
                    for row in current_files
                ]
            )
            attempt.current_revision = next_revision
            attempt.submitted_at = submitted_at
            snapshot = Snapshot(
                workspace_id=workspace.id,
                revision=next_revision,
                event_chain_head="",
                manifest_hash=canonical_hash(payload),
                files=payload,
                reason="LMS_IMPORT",
            )
            db.add(snapshot)
            await db.flush()
            submission.snapshot_id = snapshot.id
            submission.submitted_at = submitted_at
            submission.external_receipt = {
                **dict(submission.external_receipt or {}),
                **_origin_receipt_fields(item),
                "external_id": external_id,
                "external_revision": external_revision,
                "moodle_parent_external_id": item.get("moodle_parent_external_id"),
                "moodle_parent_external_revision": item.get("moodle_parent_external_revision"),
                "moodle_response_position": item.get("moodle_response_position"),
                "source_complete": not source_omissions,
                "historical_source_materialization_version": (
                    HISTORICAL_SOURCE_MATERIALIZATION_VERSION
                ),
                "source_omissions": source_omissions,
                "state": item.get("state"),
            }
            mapping.external_revision = external_revision
            mapping_metadata = dict(mapping.metadata_json or {})
            mapping_metadata["source_complete"] = not source_omissions
            mapping_metadata["assessment_id"] = str(assessment.id)
            mapping_metadata["moodle_parent_external_id"] = item.get("moodle_parent_external_id")
            mapping_metadata["moodle_parent_external_revision"] = item.get(
                "moodle_parent_external_revision"
            )
            mapping_metadata["moodle_parent_attempt_id"] = str(
                item.get("moodle_parent_attempt_id") or item.get("attempt_id") or ""
            )[:160]
            mapping_metadata["moodle_response_id"] = item.get("moodle_response_id")
            mapping_metadata["moodle_response_position"] = item.get("moodle_response_position")
            mapping_metadata["historical_source_materialization_version"] = (
                HISTORICAL_SOURCE_MATERIALIZATION_VERSION
            )
            mapping.metadata_json = mapping_metadata
            stats = stats.add("updated")
        _record_import_actor(
            submission=submission,
            mapping=mapping,
            actor_external_subject=actor_external_subject,
        )
        await _sync_imported_decision(
            db,
            submission=submission,
            assessment=assessment,
            course=course,
            item=item,
        )
    await db.flush()
    return stats


async def materialize_historical_submissions(
    db: AsyncSession,
    *,
    course: Course,
    assessment: Assessment,
    actor_external_subject: str,
    items: list[dict[str, Any]],
) -> MoodleHistoryImportStats:
    """Idempotently import Moodle history, splitting multi-Essay quizzes.

    Moodle models one Quiz attempt with several Essay questions.  Locally those
    questions are independent programming assignments: each gets a managed
    Assessment/TaskVersion and a separate Submission.  Attachments and ZIP
    members inside one response intentionally remain files of that one task.
    Assignment and single-Essay Quiz imports keep their original representation.
    """

    # A newer Moodle retry supersedes an older completed attempt as soon as it
    # appears in the report, even though an IN_PROGRESS row is not itself a
    # reviewable Submission.  Record that remote fact before the draft rows are
    # intentionally skipped by either flat or split materialization.
    actor = actor_external_subject.strip()[:255]
    if actor:
        observed_principals: dict[str, ExternalPrincipal] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            remote_attempt_id = str(
                item.get("moodle_parent_attempt_id") or item.get("attempt_id") or ""
            ).strip()
            remote_state = str(item.get("state", "")).strip().upper()
            remote_module = str(item.get("module", "")).strip().removeprefix("mod_").lower()
            remote_cmid = str(item.get("cmid", "")).strip()
            external_revision = str(item.get("external_revision", "")).strip()
            if (
                not re.fullmatch(r"[A-Za-z0-9:_-]{1,160}", remote_attempt_id)
                or remote_state not in {"IN_PROGRESS", "SUBMITTED", "GRADED", "UNKNOWN", "FINISHED"}
                or remote_module not in {"assign", "quiz"}
                or not remote_cmid.isdigit()
                or int(remote_cmid) <= 0
                or not external_revision
            ):
                continue
            subject = str(item.get("user_id", "")).strip()
            principal = observed_principals.get(subject)
            if principal is None:
                principal = await _student_principal(db, course=course, item=item)
            if principal is None:
                continue
            observed_principals[subject] = principal
            raw_epoch = item.get("submitted_at_epoch")
            submitted_at_epoch = (
                raw_epoch if isinstance(raw_epoch, int) and not isinstance(raw_epoch, bool) else 0
            )
            await observe_moodle_attempt(
                db,
                course=course,
                assessment=assessment,
                principal=principal,
                actor_external_subject=actor,
                remote_attempt_id=remote_attempt_id,
                state=remote_state,
                module=remote_module,
                cmid=remote_cmid,
                submitted_at_epoch=submitted_at_epoch,
                external_revision=external_revision,
            )
            # The same report page can contain several retries for one
            # student.  Persist the actor-scoped marker before observing the
            # next row so it is updated rather than inserted twice when the
            # session factory has autoflush disabled.
            await db.flush()
        # Flat/split materialization resolves the same students again.  Flush
        # the memberships and observation rows first so those idempotent
        # lookups cannot enqueue duplicate unique rows in this transaction.
        await db.flush()

    if not await _quiz_requires_question_split(
        db,
        course=course,
        assessment=assessment,
        items=items,
    ):
        return await _materialize_flat_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject=actor_external_subject,
            items=items,
        )

    await _mark_managed_quiz_as_split_container(db, course=course, parent=assessment)
    stats = MoodleHistoryImportStats()
    for item in items:
        if not isinstance(item, dict):
            stats = stats.add("skipped")
            continue
        if str(item.get("state", "")).upper() == "IN_PROGRESS":
            stats = stats.add("skipped")
            continue
        if str(item.get("module", "")) != "quiz":
            stats = stats.merge(
                await _materialize_flat_historical_submissions(
                    db,
                    course=course,
                    assessment=assessment,
                    actor_external_subject=actor_external_subject,
                    items=[item],
                )
            )
            continue
        raw_cmid = item.get("cmid")
        responses = item.get("responses")
        if (
            isinstance(raw_cmid, bool)
            or not str(raw_cmid or "").isdigit()
            or not isinstance(responses, list)
            or not responses
        ):
            stats = stats.add("skipped")
            continue
        cmid = int(raw_cmid)
        legacy_adopted = False
        response_ids: list[str] = []
        active_external_ids: set[str] = set()
        response_set_valid = True
        for position, response in enumerate(responses, start=1):
            if not isinstance(response, dict):
                response_set_valid = False
                stats = stats.add("skipped")
                continue
            response_id = str(response.get("response_id", "")).strip()[:255]
            if not response_id:
                response_set_valid = False
                stats = stats.add("skipped")
                continue
            if response_id in response_ids:
                response_set_valid = False
                stats = stats.add("skipped")
                continue
            response_ids.append(response_id)
            split_item = _quiz_response_submission(item, response, position=position)
            active_external_ids.add(str(split_item["external_id"]))
            multi_file = len(historical_source_files(split_item)) > 1
            child, version = await _ensure_quiz_question_context(
                db,
                course=course,
                parent=assessment,
                response=response,
                position=position,
                cmid=cmid,
                multi_file=multi_file,
            )
            if not legacy_adopted:
                legacy_adopted = await _adopt_legacy_combined_submission(
                    db,
                    course=course,
                    parent=assessment,
                    child=child,
                    version=version,
                    legacy_external_id=str(item.get("external_id", ""))[:255],
                    split_external_id=str(split_item["external_id"]),
                )
            stats = stats.merge(
                await _materialize_flat_historical_submissions(
                    db,
                    course=course,
                    assessment=child,
                    actor_external_subject=actor_external_subject,
                    items=[split_item],
                    attached_version=version,
                )
            )
        if not legacy_adopted:
            await _retire_legacy_combined_submission(
                db,
                course=course,
                parent=assessment,
                legacy_external_id=str(item.get("external_id", ""))[:255],
                response_ids=response_ids,
            )
        cleanup_item = item if response_set_valid else {**item, "responses_complete": False}
        await _retire_stale_quiz_response_submissions(
            db,
            course=course,
            parent=assessment,
            item=cleanup_item,
            cmid=cmid,
            active_external_ids=active_external_ids,
        )
    await db.flush()
    return stats


async def enqueue_historical_submission_imports(
    db: AsyncSession,
    *,
    course: Course,
    actor_external_subject: str,
) -> int:
    """Queue fast review-candidate scans for the teacher who synchronized the course.

    The exhaustive crawl is chained only after this priority pass completes.
    That prevents dozens of old graded responses from delaying a newly
    finished attempt which Moodle already marks as requiring manual grading.
    """

    actor_external_subject = actor_external_subject.strip()[:255]
    if not actor_external_subject:
        return 0
    actor_id = await db.scalar(
        select(ExternalPrincipal.id)
        .join(
            CourseMembership,
            CourseMembership.principal_id == ExternalPrincipal.id,
        )
        .join(
            MoodleCredential,
            (MoodleCredential.principal_id == ExternalPrincipal.id)
            & (MoodleCredential.connection_id == course.connection_id),
        )
        .where(
            CourseMembership.course_id == course.id,
            CourseMembership.role == CourseRole.TEACHER.value,
            CourseMembership.active.is_(True),
            ExternalPrincipal.connection_id == course.connection_id,
            ExternalPrincipal.external_subject == actor_external_subject,
            ExternalPrincipal.active.is_(True),
            MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
            MoodleCredential.status == "ACTIVE",
            MoodleCredential.revoked_at.is_(None),
            or_(
                MoodleCredential.expires_at.is_(None),
                MoodleCredential.expires_at > utcnow(),
            ),
        )
    )
    if actor_id is None or not await teacher_membership_is_authorized(db, actor_id):
        return 0

    mappings = list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == course.connection_id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                )
            )
        ).all()
    )
    active_rows = list(
        (
            await db.scalars(
                select(SyncOutbox).where(
                    SyncOutbox.course_id == course.id,
                    SyncOutbox.event_type == "moodle.history.import",
                    SyncOutbox.state.in_(_IMPORT_EVENT_STATES),
                )
            )
        ).all()
    )
    active_chains = {
        (
            row.aggregate_id,
            str((row.payload or {}).get("actor_external_subject", "")),
            (row.payload or {}).get("priority_only") is True,
        )
        for row in active_rows
    }
    queued = 0
    for mapping in mappings:
        assessment = await db.get(Assessment, mapping.local_id)
        if assessment is None or assessment.course_id != course.id:
            continue
        metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
        module = str(metadata.get("module", mapping.external_type)).lower().removeprefix("mod_")
        cmid = metadata.get("cmid", mapping.external_id)
        if module not in {"quiz", "assign"} or isinstance(cmid, bool) or not str(cmid).isdigit():
            continue
        chain_key = (assessment.id, actor_external_subject, True)
        if chain_key in active_chains:
            continue
        actor_hash = hashlib.sha256(actor_external_subject.encode("utf-8")).hexdigest()[:12]
        event_id = uuid.uuid4().hex[:12]
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                event_type="moodle.history.import",
                aggregate_type="Assessment",
                aggregate_id=assessment.id,
                idempotency_key=(
                    f"moodle-history-priority:{assessment.id.hex[:12]}:{actor_hash}:{event_id}"
                ),
                payload={
                    "course_id": course.external_id,
                    "actor_external_subject": actor_external_subject,
                    "module": module,
                    "cmid": int(cmid),
                    "cursor": "0:0",
                    # Five detail pages keep one Playwright operation below the
                    # reverse-proxy timeout even on the low-power N150 host.
                    "limit": 5,
                    "priority_only": True,
                },
            )
        )
        active_chains.add(chain_key)
        queued += 1
    await db.flush()
    return queued


__all__ = [
    "MoodleHistoryImportStats",
    "enqueue_historical_submission_imports",
    "historical_source_files",
    "materialize_historical_submissions",
]
