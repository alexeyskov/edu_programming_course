from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from app.models.enums import RunOrigin, RunStatus
from app.schemas.common import JsonObject, MutationModel, ReadModel, Revision


class RunCreateRequest(MutationModel):
    revision: Revision
    stdin: Annotated[str, StringConstraints(max_length=1_048_576)] = ""
    mode: Literal["RUN", "TEST"] = "RUN"


class InteractiveRunCreateRequest(MutationModel):
    revision: Revision


class InteractiveRunInputRequest(MutationModel):
    text: Annotated[str, StringConstraints(max_length=65_536)]


class DiagnosticRange(ReadModel):
    start_line: int | None = Field(default=None, ge=1)
    start_column: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)
    end_column: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def ordered_range(self) -> DiagnosticRange:
        if self.start_line is not None and self.end_line is not None:
            if self.end_line < self.start_line:
                raise ValueError("end_line must not precede start_line")
        if (
            self.start_line is not None
            and self.start_column is not None
            and self.end_line == self.start_line
            and self.end_column is not None
        ):
            if self.end_column < self.start_column:
                raise ValueError("end_column must not precede start_column")
        return self


class DiagnosticRead(ReadModel):
    id: UUID | str | None = None
    file_id: UUID | None = None
    file: Annotated[str, StringConstraints(max_length=512)] | None = None
    range: DiagnosticRange
    severity: Literal["error", "warning", "info"]
    code: Annotated[str, StringConstraints(max_length=100)] | None = None
    message: Annotated[str, StringConstraints(min_length=1, max_length=20_000)]
    notes: list[Annotated[str, StringConstraints(max_length=4_000)]] = Field(
        default_factory=list,
        max_length=100,
    )
    related: list[JsonObject] = Field(default_factory=list, max_length=100)
    fix_its: list[JsonObject] = Field(default_factory=list, max_length=100)


class RunMetricsRead(ReadModel):
    wall_time_ms: int | None = Field(default=None, ge=0)
    cpu_time_ms: int | None = Field(default=None, ge=0)
    peak_memory_kb: int | None = Field(default=None, ge=0)
    compilation_duration_ms: int | None = Field(default=None, ge=0)
    execution_duration_ms: int | None = Field(default=None, ge=0)
    manifest_sha256: Annotated[str, StringConstraints(max_length=64)] | None = None
    executable_sha256: Annotated[str, StringConstraints(max_length=64)] | None = None
    extra: JsonObject = Field(default_factory=dict)


class RunResultPayloadRead(ReadModel):
    exit_code: int | None = None
    exit_reason: Annotated[str, StringConstraints(max_length=64)] = ""
    stdout: Annotated[str, StringConstraints(max_length=4_194_304)] = ""
    stderr: Annotated[str, StringConstraints(max_length=4_194_304)] = ""
    diagnostics: list[DiagnosticRead] = Field(default_factory=list, max_length=10_000)
    metrics: RunMetricsRead = Field(default_factory=RunMetricsRead)
    completed_at: datetime | None = None


class RunStudentRead(ReadModel):
    """Runner response without selected build/filesystem policy or external job IDs."""

    id: UUID
    status: RunStatus
    revision: Revision
    result: RunResultPayloadRead | None = None
    created_at: datetime
    updated_at: datetime


class RunTeacherRead(RunStudentRead):
    """Run metadata including requested and runner-confirmed isolation policies."""

    origin: RunOrigin
    attempt_id: UUID | None = None
    submission_id: UUID | None = None
    teacher_experiment_id: UUID | None = None
    evidence_report_id: UUID | None = None
    evidence_case_index: int | None = Field(default=None, ge=0, le=19)
    requested_by_id: UUID
    mode: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    build_profile: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    filesystem_profile: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    network_enabled: bool
    external_job_id: Annotated[str, StringConstraints(max_length=255)] = ""
    actual_executor_version: Annotated[str, StringConstraints(max_length=100)] | None = None
    actual_filesystem_policy_version: Annotated[str, StringConstraints(max_length=100)] | None = (
        None
    )
    actual_filesystem_isolated: bool | None = None
    actual_network_enabled: bool | None = None


class InteractiveRunRead(ReadModel):
    session_id: Annotated[
        str, StringConstraints(min_length=32, max_length=32, pattern=r"^[0-9a-f]{32}$")
    ]
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
    stdout: Annotated[str, StringConstraints(max_length=4_194_304)] = ""
    stderr: Annotated[str, StringConstraints(max_length=4_194_304)] = ""
    output_truncated: bool = False
    input_closed: bool = False
    diagnostics: list[DiagnosticRead] = Field(default_factory=list, max_length=10_000)


__all__ = [
    "DiagnosticRange",
    "DiagnosticRead",
    "RunCreateRequest",
    "InteractiveRunCreateRequest",
    "InteractiveRunInputRequest",
    "InteractiveRunRead",
    "RunMetricsRead",
    "RunResultPayloadRead",
    "RunStudentRead",
    "RunTeacherRead",
]
