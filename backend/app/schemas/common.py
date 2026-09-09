from __future__ import annotations

from decimal import Decimal
from pathlib import PurePosixPath
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, field_validator


class ReadModel(BaseModel):
    """Base for public response DTOs backed by SQLAlchemy entities or projections."""

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MutationModel(BaseModel):
    """Base for request DTOs: silently accepting an unknown mutation is unsafe."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class EmptyMutation(MutationModel):
    pass


JsonObject = dict[str, JsonValue]
Revision = Annotated[int, Field(ge=0)]
PositiveRevision = Annotated[int, Field(ge=1)]
Score = Annotated[
    Decimal,
    Field(ge=0, max_digits=8, decimal_places=2),
]
Probability = Annotated[
    Decimal,
    Field(ge=0, le=1, max_digits=7, decimal_places=6),
]
SHA256Hex = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{64}$"),
]
ShortText = Annotated[str, StringConstraints(max_length=255)]
SourceContent = Annotated[str, StringConstraints(max_length=2_097_152)]


def validate_source_path(value: str) -> str:
    if not value or len(value) > 512:
        raise ValueError("path must contain between 1 and 512 characters")
    if value.startswith("/") or "\\" in value or "\x00" in value:
        raise ValueError("path must be a relative POSIX path")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("path must not contain empty, dot, or parent segments")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError("path must remain inside the workspace")
    allowed_suffixes = {
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
    if path.suffix.lower() not in allowed_suffixes:
        raise ValueError("path extension is not allowed for a C/C++ workspace")
    return value


class SourceFileInput(MutationModel):
    path: str
    content: SourceContent = ""
    language: Annotated[str, StringConstraints(min_length=1, max_length=20)] | None = None
    read_only: bool = False

    _valid_path = field_validator("path")(validate_source_path)


class SourceFileRead(ReadModel):
    id: UUID | None = None
    path: str
    content: str
    language: str | None = None
    read_only: bool = False

    _valid_path = field_validator("path")(validate_source_path)


class ValidationIssue(ReadModel):
    field: Annotated[str, StringConstraints(max_length=255)] | None = None
    code: Annotated[str, StringConstraints(max_length=100)] | None = None
    message: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class ValidationResult(ReadModel):
    valid: bool
    errors: list[ValidationIssue] = Field(default_factory=list, max_length=200)
    warnings: list[ValidationIssue] = Field(default_factory=list, max_length=200)


class ErrorResponse(ReadModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    message: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    fields: dict[str, list[str]] = Field(default_factory=dict)
    trace_id: Annotated[str, StringConstraints(max_length=100)] | None = None


class Page[T](ReadModel):
    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1, le=500)
    offset: int = Field(ge=0)


__all__ = [
    "EmptyMutation",
    "ErrorResponse",
    "JsonObject",
    "MutationModel",
    "Page",
    "PositiveRevision",
    "Probability",
    "ReadModel",
    "Revision",
    "SHA256Hex",
    "Score",
    "ShortText",
    "SourceContent",
    "SourceFileInput",
    "SourceFileRead",
    "ValidationIssue",
    "ValidationResult",
    "validate_source_path",
]
