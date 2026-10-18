from __future__ import annotations

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PROFILES = {
    f"{language}-{compiler}-{standard}-{mode}": (language, compiler, standard)
    for language, standard in (("cpp", "c++20"), ("c", "c17"))
    for compiler in ("gcc", "clang")
    for mode in ("single", "multi")
}
SUFFIXES = {
    ".c",
    ".cpp",
    ".cc",
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


def safe_path(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or len(value) > 256
        or path.is_absolute()
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(
            part in {"", ".", ".."} or part.startswith(".") for part in value.split("/")
        )
        or path.suffix.lower() not in SUFFIXES
    ):
        raise ValueError("only relative source/header paths are allowed")
    return value


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceFile(StrictModel):
    path: str
    content: str = Field(max_length=2 * 1024 * 1024)
    _validate_path = field_validator("path")(safe_path)


class Position(StrictModel):
    line: int = Field(ge=0, le=200000)
    character: int = Field(ge=0, le=2000000)


class CompletionContext(StrictModel):
    triggerKind: int = Field(default=1, ge=1, le=3)
    triggerCharacter: str | None = Field(default=None, max_length=4)
    includeDeclaration: bool | None = None
    isRetrigger: bool | None = None
    tabSize: int | None = Field(default=None, ge=2, le=8)
    insertSpaces: bool | None = None


class Query(StrictModel):
    workspace_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    build_profile: str
    files: list[SourceFile] = Field(min_length=1, max_length=64)
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
    position: Position | None = None
    context: CompletionContext | None = None
    _validate_path = field_validator("path")(safe_path)

    @model_validator(mode="after")
    def valid_manifest(self) -> Query:
        if self.build_profile not in PROFILES:
            raise ValueError("unsupported runner build profile")
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)) or self.path not in paths:
            raise ValueError("files must be unique and include requested path")
        if (
            sum(len(file.content.encode("utf-8")) for file in self.files)
            > 2 * 1024 * 1024
        ):
            raise ValueError("workspace exceeds 2 MiB")
        if any("\x00" in file.content for file in self.files):
            raise ValueError("NUL bytes are not allowed")
        if (
            self.method
            in {"completion", "hover", "signatureHelp", "definition", "references"}
            and self.position is None
        ):
            raise ValueError("method requires a position")
        if self.position:
            text = next(file.content for file in self.files if file.path == self.path)
            lines = text.split("\n")
            if self.position.line >= len(lines):
                raise ValueError("position is outside document")
            if (
                self.position.character
                > len(lines[self.position.line].encode("utf-16-le")) // 2
            ):
                raise ValueError("position is outside line")
        return self
