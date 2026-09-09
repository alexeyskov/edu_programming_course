from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import Any

from app.models.integration import AuditEntry


@dataclass(slots=True)
class DomainError(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.message


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_hash(value: Any) -> str:
    return sha256_text(canonical_json(value))


def positive_decimal(value: object) -> Decimal | None:
    """Parse a finite positive external numeric value without float arithmetic."""

    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() and parsed > 0 else None


def validate_source_path(value: str) -> str:
    if not value or "\x00" in value or "\\" in value:
        raise DomainError(422, "INVALID_SOURCE_PATH", "Use a non-empty POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DomainError(422, "INVALID_SOURCE_PATH", "Absolute paths and traversal are forbidden")
    normalized = str(path)
    if len(normalized) > 512:
        raise DomainError(422, "INVALID_SOURCE_PATH", "Source path is too long")
    allowed = {
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
    if path.suffix.lower() not in allowed:
        raise DomainError(422, "INVALID_SOURCE_PATH", "File extension is not allowed")
    return normalized


def language_for_path(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".txt":
        return "TEXT"
    return "C" if suffix == ".c" else "CPP"


def contiguous_delta(previous: str, current: str) -> dict[str, Any]:
    prefix = 0
    limit = min(len(previous), len(current))
    while prefix < limit and previous[prefix] == current[prefix]:
        prefix += 1
    suffix = 0
    old_remaining = len(previous) - prefix
    new_remaining = len(current) - prefix
    while (
        suffix < old_remaining
        and suffix < new_remaining
        and previous[len(previous) - suffix - 1] == current[len(current) - suffix - 1]
    ):
        suffix += 1
    insert_end = len(current) - suffix if suffix else len(current)
    return {
        "type": "contiguous_delta",
        "offset": prefix,
        "delete_count": len(previous) - prefix - suffix,
        "insert_text": current[prefix:insert_end],
        "offset_encoding": "unicode_codepoint",
    }


def audit_row(
    *,
    actor_id: uuid.UUID | None,
    action: str,
    object_type: str = "",
    object_id: uuid.UUID | None = None,
    course_id: uuid.UUID | None = None,
    request_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> AuditEntry:
    return AuditEntry(
        actor_id=actor_id,
        action=action,
        object_type=object_type,
        object_id=object_id,
        course_id=course_id,
        request_id=request_id[:100],
        metadata_json=metadata or {},
    )
