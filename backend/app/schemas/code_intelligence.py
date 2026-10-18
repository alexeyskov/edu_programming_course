"""Deliberately narrow browser protocol, not an arbitrary JSON-RPC tunnel."""

from __future__ import annotations

import uuid
from pathlib import PurePosixPath
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_SOURCE_BYTES = 2 * 1024 * 1024
SOURCE_SUFFIXES = frozenset(
    {
        ".c",
        ".cc",
        ".cpp",
        ".cxx",
        ".c++",
        ".h",
        ".hh",
        ".hpp",
        ".hxx",
        ".h++",
        ".ipp",
        ".tpp",
        ".inc",
    }
)


def source_path(value: str) -> str:
    if (
        not value
        or len(value) > 256
        or "\\" in value
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or any(not part or part.startswith(".") for part in value.split("/"))
        or PurePosixPath(value).suffix.lower() not in SOURCE_SUFFIXES
    ):
        raise ValueError("Expected a safe relative C/C++ source or header path")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CodeIntelligenceScope(StrictModel):
    kind: Literal["attempt", "submission", "experiment"]
    id: uuid.UUID


class CodeIntelligenceFile(StrictModel):
    path: str
    content: str = Field(max_length=MAX_SOURCE_BYTES)

    _path = field_validator("path")(source_path)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str) -> str:
        try:
            size = len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("Source must be valid UTF-8 text") from exc
        if size > MAX_SOURCE_BYTES or "\x00" in value:
            raise ValueError("Source exceeds the text size limit")
        return value


class CodeIntelligencePosition(StrictModel):
    line: int = Field(ge=0, le=1_000_000, strict=True)
    character: int = Field(ge=0, le=MAX_SOURCE_BYTES, strict=True)


class CodeIntelligenceContext(StrictModel):
    triggerKind: int | None = Field(default=None, ge=1, le=3, strict=True)
    triggerCharacter: str | None = Field(default=None, min_length=1, max_length=4)
    isRetrigger: bool | None = Field(default=None, strict=True)
    includeDeclaration: bool | None = Field(default=None, strict=True)
    tabSize: int | None = Field(default=None, ge=2, le=8, strict=True)
    insertSpaces: bool | None = Field(default=None, strict=True)


class CodeIntelligenceQuery(StrictModel):
    scope: CodeIntelligenceScope
    files: list[CodeIntelligenceFile] | None = Field(default=None, max_length=64)
    method: Literal[
        "completion",
        "hover",
        "signatureHelp",
        "definition",
        "references",
        "documentSymbol",
        "diagnostics",
        "formatting",
    ]
    path: str
    position: CodeIntelligencePosition | None = None
    context: CodeIntelligenceContext | None = None

    _path = field_validator("path")(source_path)

    @model_validator(mode="after")
    def bounded_manifest(self) -> Self:
        if self.scope.kind == "submission" and self.files is not None:
            raise ValueError("Submission source is immutable and provided by the server")
        if self.files is not None:
            if len({item.path for item in self.files}) != len(self.files):
                raise ValueError("Duplicate source paths")
            if sum(len(item.content.encode("utf-8")) for item in self.files) > MAX_SOURCE_BYTES:
                raise ValueError("Source manifest exceeds the size limit")
        if self.method in {"completion", "hover", "signatureHelp", "definition", "references"}:
            if self.position is None:
                raise ValueError("This operation requires a position")
        return self


class CodeIntelligenceRead(StrictModel):
    result: Any = None
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
