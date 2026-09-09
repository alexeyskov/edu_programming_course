from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StringConstraints,
    field_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


RelativePath = Annotated[
    str,
    StringConstraints(strip_whitespace=False, min_length=1, max_length=240),
]


class SourceFile(StrictModel):
    path: RelativePath
    content: Annotated[str, StringConstraints(max_length=2 * 1024 * 1024)]

    @field_validator("path")
    @classmethod
    def path_is_safe(cls, value: str) -> str:
        # Full canonical validation is repeated at the filesystem boundary.
        if "\x00" in value or "\\" in value or value.startswith("/"):
            raise ValueError("path must be a normalized relative POSIX path")
        components = value.split("/")
        if any(part in {"", ".", ".."} for part in components):
            raise ValueError("path must not contain empty, dot, or parent components")
        if any(len(part.encode("utf-8")) > 100 for part in components):
            raise ValueError("path component is too long")
        return value


class Action(StrEnum):
    COMPILE = "compile"
    COMPILE_AND_RUN = "compile_and_run"


class ExecutionLimits(StrictModel):
    cpu_seconds: Annotated[StrictInt, Field(ge=1, le=300)]
    memory_mb: Annotated[StrictInt, Field(ge=1, le=65_536)]


class ExecutionRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    ]
    profile_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    action: Action = Action.COMPILE_AND_RUN
    files: Annotated[list[SourceFile], Field(min_length=1, max_length=64)]
    stdin: Annotated[str, StringConstraints(max_length=1024 * 1024)] = ""
    limits: ExecutionLimits | None = None

    @field_validator("files")
    @classmethod
    def paths_are_unique(cls, files: list[SourceFile]) -> list[SourceFile]:
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        return files


class InteractiveSessionCreateRequest(StrictModel):
    """Compile a manifest and keep the resulting program connected to stdin."""

    schema_version: Literal["1.0"] = "1.0"
    request_id: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    ]
    owner_key: Annotated[
        str,
        StringConstraints(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    ]
    profile_id: Annotated[str, StringConstraints(min_length=1, max_length=80)]
    files: Annotated[list[SourceFile], Field(min_length=1, max_length=64)]
    limits: ExecutionLimits | None = None

    @field_validator("files")
    @classmethod
    def paths_are_unique(cls, files: list[SourceFile]) -> list[SourceFile]:
        paths = [item.path for item in files]
        if len(paths) != len(set(paths)):
            raise ValueError("file paths must be unique")
        return files


class InteractiveSessionCommand(StrictModel):
    owner_key: Annotated[
        str,
        StringConstraints(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$"),
    ]


class InteractiveSessionInput(InteractiveSessionCommand):
    text: Annotated[str, StringConstraints(max_length=64 * 1024)]


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticRange(StrictModel):
    start_line: int = Field(ge=1)
    start_column: int = Field(ge=1)
    end_line: int = Field(ge=1)
    end_column: int = Field(ge=1)


class RelatedDiagnostic(StrictModel):
    message: str
    file: str | None = None
    range: DiagnosticRange | None = None


class FixIt(StrictModel):
    file: str
    range: DiagnosticRange
    replacement: str


class Diagnostic(StrictModel):
    producer: str
    producer_version: str | None = None
    severity: Severity
    code: str | None = None
    message: str
    file: str | None = None
    range: DiagnosticRange | None = None
    related: list[RelatedDiagnostic] = Field(default_factory=list)
    fix_its: list[FixIt] = Field(default_factory=list)


class JobStatus(StrEnum):
    COMPILED = "COMPILED"
    SUCCESS = "SUCCESS"
    COMPILE_ERROR = "COMPILE_ERROR"
    RUNTIME_ERROR = "RUNTIME_ERROR"
    TIME_LIMIT = "TIME_LIMIT"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    WORKSPACE_LIMIT = "WORKSPACE_LIMIT"
    FILESYSTEM_DENIED = "FILESYSTEM_DENIED"
    INFRA_ERROR = "INFRA_ERROR"


class PhaseStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIME_LIMIT = "TIME_LIMIT"
    MEMORY_LIMIT = "MEMORY_LIMIT"
    OUTPUT_LIMIT = "OUTPUT_LIMIT"
    WORKSPACE_LIMIT = "WORKSPACE_LIMIT"
    INFRA_ERROR = "INFRA_ERROR"


class BoundedText(StrictModel):
    text: str
    bytes_captured: int = Field(ge=0)
    truncated: bool


class PhaseResult(StrictModel):
    status: PhaseStatus
    exit_code: int | None
    duration_ms: int = Field(ge=0)
    stdout: BoundedText
    stderr: BoundedText


class ProfileSummary(StrictModel):
    id: str
    language: Literal["c", "cpp"]
    compiler_family: Literal["gcc", "clang"]
    compiler_version: str | None
    standard: str
    mode: Literal["single", "multi"]


class IsolationSummary(StrictModel):
    policy: Literal["UNRESTRICTED_CONTAINER"] = "UNRESTRICTED_CONTAINER"
    policy_version: str = "unrestricted-container-v1"
    executor: Literal["local"]
    filesystem_isolated: bool
    network: Literal["denied", "host"]
    warning: str | None = None


class ExecutionResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str
    job_id: str
    status: JobStatus
    manifest_sha256: str
    executable_sha256: str | None = None
    profile: ProfileSummary
    isolation: IsolationSummary
    compilation: PhaseResult
    execution: PhaseResult | None = None
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class InteractiveSessionResponse(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    session_id: str
    status: Literal[
        "RUNNING",
        "SUCCESS",
        "COMPILE_ERROR",
        "RUNTIME_ERROR",
        "TIME_LIMIT",
        "MEMORY_LIMIT",
        "OUTPUT_LIMIT",
        "WORKSPACE_LIMIT",
        "STOPPED",
        "INFRA_ERROR",
    ]
    terminal: bool
    exit_code: int | None = None
    duration_ms: int = Field(ge=0)
    stdout: str = ""
    stderr: str = ""
    output_truncated: bool = False
    input_closed: bool = False
    compilation: PhaseResult
    diagnostics: list[Diagnostic] = Field(default_factory=list)


class ProfileResponse(StrictModel):
    id: str
    language: Literal["c", "cpp"]
    compiler_family: Literal["gcc", "clang"]
    standard: str
    mode: Literal["single", "multi"]
    available: bool
