from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from app.schemas.common import EmptyMutation, ReadModel, SHA256Hex


class EvidenceRunCreateRequest(EmptyMutation):
    pass


class EvidenceCaseOutcomeRead(ReadModel):
    case_index: int = Field(ge=0, le=19)
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    run_id: UUID
    status: Literal["PASSED", "FAILED", "INFRASTRUCTURE_ERROR"]
    comparison: Literal["EXACT", "TRIM_TRAILING_WHITESPACE"]
    exit_code: int | None = None
    actual_stdout_sha256: SHA256Hex
    expected_stdout_sha256: SHA256Hex
    actual_stdout_preview: Annotated[str, StringConstraints(max_length=4_096)] = ""
    stderr_preview: Annotated[str, StringConstraints(max_length=4_096)] = ""
    filesystem_isolated: bool | None = None
    network_enabled: bool | None = None


class EvidenceFindingRead(ReadModel):
    code: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    message: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]
    case_index: int | None = Field(default=None, ge=0, le=19)
    run_id: UUID | None = None


class EvidenceReportRead(ReadModel):
    id: UUID
    submission_id: UUID
    snapshot_id: UUID
    task_version_id: UUID
    requested_by_id: UUID
    hidden_test_manifest_hash: SHA256Hex
    task_content_hash: SHA256Hex
    snapshot_manifest_hash: SHA256Hex
    status: Literal["RUNNING", "COMPLETED", "FAILED"]
    passed_cases: int = Field(ge=0, le=20)
    total_cases: int = Field(ge=1, le=20)
    outcomes: list[EvidenceCaseOutcomeRead] = Field(default_factory=list, max_length=20)
    findings: list[EvidenceFindingRead] = Field(default_factory=list, max_length=100)
    failure_code: Annotated[str, StringConstraints(max_length=100)] = ""
    failure_message: Annotated[str, StringConstraints(max_length=2_000)] = ""
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


__all__ = [
    "EvidenceCaseOutcomeRead",
    "EvidenceFindingRead",
    "EvidenceReportRead",
    "EvidenceRunCreateRequest",
]
