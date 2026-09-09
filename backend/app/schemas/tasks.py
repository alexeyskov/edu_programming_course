from __future__ import annotations

import json
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)

from app.models.enums import TaskScope, TaskVersionStatus
from app.schemas.common import (
    EmptyMutation,
    JsonObject,
    MutationModel,
    ReadModel,
    Score,
    SHA256Hex,
    SourceFileInput,
    SourceFileRead,
    ValidationResult,
)

Tag = Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]
HIDDEN_TEST_MAX_CASES = 20
HIDDEN_TEST_MAX_AGGREGATE_BYTES = 1_048_576
HIDDEN_TEST_MAX_STREAM_BYTES = 262_144
TRANSLATION_UNIT_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx"})


def valid_single_file_starter_paths(paths: list[str]) -> bool:
    suffixes = [PurePosixPath(path).suffix.lower() for path in paths]
    return sum(suffix in TRANSLATION_UNIT_SUFFIXES for suffix in suffixes) == 1 and all(
        suffix in TRANSLATION_UNIT_SUFFIXES or suffix == ".txt" for suffix in suffixes
    )


class HiddenTestCase(MutationModel):
    """One deterministic, stdin/stdout-based hidden test.

    The intentionally small contract is runner-independent and contains no shell
    commands, paths, environment variables, scoring weights, or executable policy.
    """

    name: Annotated[str, StringConstraints(min_length=1, max_length=100, strip_whitespace=True)]
    stdin: Annotated[str, StringConstraints(max_length=262_144)]
    expected_stdout: Annotated[str, StringConstraints(max_length=262_144)]
    comparison: Literal["EXACT", "TRIM_TRAILING_WHITESPACE"]

    @field_validator("stdin", "expected_stdout")
    @classmethod
    def bounded_utf8_stream(cls, value: str) -> str:
        if len(value.encode("utf-8")) > HIDDEN_TEST_MAX_STREAM_BYTES:
            raise ValueError("hidden test stream exceeds the 262144-byte UTF-8 limit")
        return value


class HiddenTestManifestV1(MutationModel):
    schema_version: Annotated[int, Field(strict=True, ge=1, le=1)]
    cases: list[HiddenTestCase] = Field(min_length=1, max_length=HIDDEN_TEST_MAX_CASES)

    @field_validator("cases")
    @classmethod
    def unique_case_names(cls, value: list[HiddenTestCase]) -> list[HiddenTestCase]:
        if len({case.name.casefold() for case in value}) != len(value):
            raise ValueError("hidden test case names must be unique")
        return value

    @model_validator(mode="after")
    def bounded_encoded_size(self) -> HiddenTestManifestV1:
        encoded = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(encoded) > HIDDEN_TEST_MAX_AGGREGATE_BYTES:
            raise ValueError("hidden test manifest exceeds the 1 MiB aggregate limit")
        return self


def parse_hidden_test_manifest(value: object) -> HiddenTestManifestV1 | None:
    """Return a validated v1 manifest; an exactly empty object disables hidden tests."""

    if value == {}:
        return None
    if not isinstance(value, dict):
        raise ValueError("hidden test manifest must be an object")
    try:
        return HiddenTestManifestV1.model_validate(value)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        location = ".".join(str(part) for part in first.get("loc", ()))
        message = str(first.get("msg", "invalid hidden test manifest"))
        prefix = f"{location}: " if location else ""
        raise ValueError(f"{prefix}{message}") from exc


class PublicExample(MutationModel):
    title: Annotated[str, StringConstraints(max_length=255)] = ""
    stdin: Annotated[str, StringConstraints(max_length=262_144)] = ""
    stdout: Annotated[str, StringConstraints(max_length=262_144)] = ""
    explanation: Annotated[str, StringConstraints(max_length=10_000)] = ""


class TaskBankItemCreateRequest(MutationModel):
    scope: TaskScope = TaskScope.COURSE
    course: UUID | None = None
    slug: Annotated[
        str,
        StringConstraints(
            min_length=1,
            max_length=160,
            pattern=r"^[a-z0-9][a-z0-9_-]*$",
            strip_whitespace=True,
        ),
    ]
    category: Annotated[str, StringConstraints(max_length=255, strip_whitespace=True)] = ""
    tags: list[Tag] = Field(default_factory=list, max_length=50)

    @field_validator("tags")
    @classmethod
    def unique_tags(cls, value: list[str]) -> list[str]:
        if len({tag.casefold() for tag in value}) != len(value):
            raise ValueError("tags must be unique")
        return value

    @model_validator(mode="after")
    def scope_has_expected_course(self) -> TaskBankItemCreateRequest:
        if self.scope == TaskScope.COURSE and self.course is None:
            raise ValueError("course is required for COURSE-scoped task items")
        if self.scope == TaskScope.SYSTEM and self.course is not None:
            raise ValueError("course must be omitted for SYSTEM-scoped task items")
        return self


class TaskBankItemUpdateRequest(MutationModel):
    category: Annotated[str, StringConstraints(max_length=255, strip_whitespace=True)] | None = None
    tags: list[Tag] | None = Field(default=None, max_length=50)
    archived: bool | None = None

    @model_validator(mode="after")
    def has_change(self) -> TaskBankItemUpdateRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be supplied")
        if self.tags is not None and len({tag.casefold() for tag in self.tags}) != len(self.tags):
            raise ValueError("tags must be unique")
        return self


class TaskVersionCreateRequest(MutationModel):
    title: Annotated[str, StringConstraints(min_length=1, max_length=255, strip_whitespace=True)]
    statement: Annotated[str, StringConstraints(min_length=1, max_length=500_000)]
    language: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    language_standard: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    multi_file: bool = False
    starter_files: list[SourceFileInput] = Field(min_length=1, max_length=64)
    build_profile: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    public_examples: list[PublicExample] = Field(default_factory=list, max_length=100)
    hidden_test_manifest: JsonObject = Field(default_factory=dict)
    max_score: Score
    difficulty: Annotated[str, StringConstraints(max_length=32)] = ""
    ai_policy: JsonObject = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_starter_files(self) -> TaskVersionCreateRequest:
        paths = [item.path for item in self.starter_files]
        if len(set(paths)) != len(paths):
            raise ValueError("starter file paths must be unique")
        if sum(len(item.content.encode("utf-8")) for item in self.starter_files) > 8_388_608:
            raise ValueError("starter files exceed the 8 MiB aggregate limit")
        if not self.multi_file and not valid_single_file_starter_paths(paths):
            raise ValueError(
                "single-file task versions must contain exactly one C/C++ translation unit "
                "and may additionally contain .txt data files"
            )
        return self


class TaskVersionStudentRead(ReadModel):
    """Assigned content without task-bank/version identifiers or hidden policy."""

    title: str
    statement: str
    language: str
    language_standard: str
    multi_file: bool
    starter_files: list[SourceFileRead] = Field(default_factory=list)
    public_examples: list[PublicExample] = Field(default_factory=list)
    max_score: Score
    difficulty: str = ""
    status: TaskVersionStatus
    published_at: datetime | None = None


class TaskVersionTeacherRead(TaskVersionStudentRead):
    id: UUID
    item_id: UUID
    number: int = Field(ge=1)
    build_profile: str
    hidden_test_manifest: JsonObject = Field(default_factory=dict)
    ai_policy: JsonObject = Field(default_factory=dict)
    content_hash: SHA256Hex
    authored_by_id: UUID
    created_at: datetime
    updated_at: datetime


class TaskBankItemRead(ReadModel):
    id: UUID
    course: UUID | None = Field(
        default=None,
        validation_alias=AliasChoices("course", "course_id"),
    )
    scope: TaskScope
    slug: str
    category: str
    tags: list[Tag] = Field(default_factory=list)
    created_at: datetime
    archived_at: datetime | None = None
    latest_version: TaskVersionTeacherRead | None = None


class TaskVersionValidateRequest(EmptyMutation):
    pass


class TaskVersionPublishRequest(EmptyMutation):
    pass


class TaskVersionValidationRead(ValidationResult):
    task_version_id: UUID


__all__ = [
    "HIDDEN_TEST_MAX_AGGREGATE_BYTES",
    "HIDDEN_TEST_MAX_CASES",
    "HIDDEN_TEST_MAX_STREAM_BYTES",
    "HiddenTestCase",
    "HiddenTestManifestV1",
    "PublicExample",
    "TaskBankItemCreateRequest",
    "TaskBankItemRead",
    "TaskBankItemUpdateRequest",
    "TaskVersionCreateRequest",
    "TaskVersionPublishRequest",
    "TaskVersionStudentRead",
    "TaskVersionTeacherRead",
    "TaskVersionValidateRequest",
    "valid_single_file_starter_paths",
    "TaskVersionValidationRead",
    "parse_hidden_test_manifest",
]
