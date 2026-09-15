from __future__ import annotations

import hashlib
import stat
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from zipfile import ZIP_STORED, ZipFile, ZipInfo

from .errors import IntegrationProtocolError

MOODLE_QUIZ_ESSAY_ARTIFACT_MAX_BYTES = 4 * 1024 * 1024
_MAX_FILES = 128
_TRANSLATION_UNIT_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
_SOURCE_SUFFIXES = {
    *_TRANSLATION_UNIT_SUFFIXES,
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".inc",
    ".txt",
}
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_UNSAFE_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_BASENAMES = {
    "AUX",
    "CON",
    "NUL",
    "PRN",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class MoodleQuizEssayArtifact:
    """A bounded binary artifact ready for the browser worker to upload."""

    filename: str
    raw_bytes: bytes
    sha256: str
    size: int


MoodleSubmissionArtifact = MoodleQuizEssayArtifact


@dataclass(frozen=True, slots=True)
class _SourceFile:
    path: str
    content: bytes


def build_moodle_quiz_essay_artifact(
    files: Sequence[Mapping[str, object]],
    *,
    force_archive: bool = False,
    maximum_bytes: int = MOODLE_QUIZ_ESSAY_ARTIFACT_MAX_BYTES,
) -> MoodleQuizEssayArtifact:
    """Build the attachment used by a Moodle Quiz Essay response.

    Exactly one C/C++ translation unit is uploaded as ``main.c`` or
    ``main.cpp``. Any other actual file set -- including a lone header/text
    companion or one translation unit plus ``input.txt`` -- is stored in a
    deterministic, uncompressed ``submission.zip``. The hard 4 MiB limit
    applies to the resulting bytes (that is, to the payload after transport
    base64 decoding).
    """

    if not isinstance(force_archive, bool):
        raise IntegrationProtocolError("Moodle Essay archive flag is invalid")
    if (
        isinstance(maximum_bytes, bool)
        or not isinstance(maximum_bytes, int)
        or not 1 <= maximum_bytes <= MOODLE_QUIZ_ESSAY_ARTIFACT_MAX_BYTES
    ):
        raise IntegrationProtocolError("Moodle Essay artifact size limit is invalid")
    if isinstance(files, str | bytes) or not isinstance(files, Sequence):
        raise IntegrationProtocolError("Moodle Essay artifact has an invalid file list")
    if not 1 <= len(files) <= _MAX_FILES:
        raise IntegrationProtocolError("Moodle Essay artifact has an invalid file count")

    normalized: list[_SourceFile] = []
    seen_paths: set[str] = set()
    source_size = 0
    for item in files:
        if not isinstance(item, Mapping):
            raise IntegrationProtocolError("Moodle Essay artifact contains an invalid file")
        _reject_symlink(item)
        path = _validate_source_path(item.get("path"))
        if path in seen_paths:
            raise IntegrationProtocolError("Moodle Essay artifact contains duplicate file paths")
        seen_paths.add(path)

        content = item.get("content")
        if not isinstance(content, str):
            raise IntegrationProtocolError("Moodle Essay artifact source must be UTF-8 text")
        try:
            encoded = content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise IntegrationProtocolError(
                "Moodle Essay artifact source is not valid UTF-8"
            ) from exc
        source_size += len(encoded)
        if source_size > maximum_bytes:
            raise IntegrationProtocolError("Moodle Essay artifact exceeds the 4 MiB limit")
        normalized.append(_SourceFile(path=path, content=encoded))

    single_suffix = PurePosixPath(normalized[0].path).suffix.lower()
    if len(normalized) == 1 and not force_archive and single_suffix in _TRANSLATION_UNIT_SUFFIXES:
        filename = "main.c" if single_suffix == ".c" else "main.cpp"
        raw_bytes = normalized[0].content
    else:
        filename = "submission.zip"
        raw_bytes = _deterministic_zip(normalized)

    if len(raw_bytes) > maximum_bytes:
        raise IntegrationProtocolError("Moodle Essay artifact exceeds the 4 MiB limit")
    return MoodleQuizEssayArtifact(
        filename=filename,
        raw_bytes=raw_bytes,
        sha256=hashlib.sha256(raw_bytes).hexdigest(),
        size=len(raw_bytes),
    )


def build_moodle_submission_artifact(
    files: Sequence[Mapping[str, object]],
    *,
    force_archive: bool = False,
    maximum_bytes: int = MOODLE_QUIZ_ESSAY_ARTIFACT_MAX_BYTES,
) -> MoodleSubmissionArtifact:
    """Preserve empty source files without asking Moodle to upload zero bytes.

    Moodle's upload repository rejects empty files. An archive retains the
    exact filename and zero-byte contents; adding whitespace or sample code
    would instead change the student's answer. Online-text answers deliberately
    use the raw-source builder below, since an empty textarea is valid.
    """
    artifact = build_moodle_quiz_essay_artifact(
        files, force_archive=force_archive, maximum_bytes=maximum_bytes
    )
    if artifact.size == 0:
        return build_moodle_quiz_essay_artifact(
            files, force_archive=True, maximum_bytes=maximum_bytes
        )
    return artifact


def build_moodle_online_text_artifact(
    files: Sequence[Mapping[str, object]],
    *,
    maximum_bytes: int = MOODLE_QUIZ_ESSAY_ARTIFACT_MAX_BYTES,
) -> MoodleSubmissionArtifact:
    """Select the sole translation unit for a Moodle online-text response.

    The IDE workspace and the LMS response transport are deliberately separate
    contracts.  A single-file programming workspace may contain companion
    ``.txt`` files used by the program, but an Online text/Essay text control
    can carry only the program itself.  Consequently those companion files are
    kept in the workspace and omitted only here, at the Moodle delivery
    boundary.  More than one translation unit remains ambiguous and is
    rejected instead of silently choosing a file.
    """

    if isinstance(files, str | bytes) or not isinstance(files, Sequence):
        raise IntegrationProtocolError("Moodle online-text artifact has an invalid file list")
    translation_units = [
        item
        for item in files
        if isinstance(item, Mapping)
        and isinstance(item.get("path"), str)
        and PurePosixPath(item["path"]).suffix.lower() in _TRANSLATION_UNIT_SUFFIXES
    ]
    if len(translation_units) != 1:
        raise IntegrationProtocolError(
            "Moodle online-text delivery requires exactly one C/C++ translation unit"
        )
    return build_moodle_quiz_essay_artifact(
        translation_units,
        maximum_bytes=maximum_bytes,
    )


def _validate_source_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise IntegrationProtocolError("Moodle Essay artifact contains an unsafe source path")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise IntegrationProtocolError("Moodle Essay artifact contains an unsafe source path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} or not _is_safe_filename(part) for part in raw_parts):
        raise IntegrationProtocolError("Moodle Essay artifact contains an unsafe source path")
    path = PurePosixPath(value)
    if path.is_absolute() or path.suffix.lower() not in _SOURCE_SUFFIXES:
        raise IntegrationProtocolError("Moodle Essay artifact contains an unsafe source path")
    return value


def _is_safe_filename(value: str) -> bool:
    if not value or value.endswith((" ", ".")):
        return False
    if len(value.encode("utf-8")) > 255:
        return False
    if any(character in _UNSAFE_FILENAME_CHARACTERS for character in value):
        return False
    if any(unicodedata.category(character).startswith("C") for character in value):
        return False
    return value.split(".", 1)[0].upper() not in _WINDOWS_RESERVED_BASENAMES


def _reject_symlink(item: Mapping[str, object]) -> None:
    if item.get("is_symlink") is True:
        raise IntegrationProtocolError("Moodle Essay artifact cannot contain symbolic links")
    entry_kind = item.get("type", item.get("kind"))
    if isinstance(entry_kind, str) and entry_kind.strip().lower() in {"link", "symlink"}:
        raise IntegrationProtocolError("Moodle Essay artifact cannot contain symbolic links")
    mode = item.get("mode")
    if isinstance(mode, int) and not isinstance(mode, bool) and stat.S_ISLNK(mode):
        raise IntegrationProtocolError("Moodle Essay artifact cannot contain symbolic links")


def _deterministic_zip(files: Sequence[_SourceFile]) -> bytes:
    buffer = BytesIO()
    try:
        with ZipFile(buffer, mode="w", compression=ZIP_STORED, allowZip64=False) as archive:
            for source in sorted(files, key=lambda item: item.path):
                info = ZipInfo(source.path, date_time=_FIXED_ZIP_TIMESTAMP)
                info.compress_type = ZIP_STORED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                info.extra = b""
                info.comment = b""
                archive.writestr(info, source.content)
    except (OSError, RuntimeError, ValueError) as exc:
        raise IntegrationProtocolError("Moodle Essay ZIP artifact could not be created") from exc
    return buffer.getvalue()


__all__ = [
    "MOODLE_QUIZ_ESSAY_ARTIFACT_MAX_BYTES",
    "MoodleQuizEssayArtifact",
    "MoodleSubmissionArtifact",
    "build_moodle_online_text_artifact",
    "build_moodle_quiz_essay_artifact",
    "build_moodle_submission_artifact",
]
