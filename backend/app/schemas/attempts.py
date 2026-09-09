from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.models.enums import AttemptState
from app.schemas.assessments import PastePolicy
from app.schemas.common import (
    EmptyMutation,
    JsonObject,
    MutationModel,
    ReadModel,
    Revision,
    SHA256Hex,
    SourceContent,
    validate_source_path,
)

EditSource = Literal["TYPING", "INTERNAL_PASTE"]
CheckpointStatus = Literal["SYNCED", "PENDING", "ERROR"]
AttemptClosureReason = Literal["LMS_ATTEMPT_FINALIZED"]


class AttemptStartRequest(EmptyMutation):
    pass


class AttemptStudentRead(ReadModel):
    """Attempt state without assigned variant IDs or captured integrity policy."""

    id: UUID
    assessment_id: UUID
    title: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    statement: str = ""
    sequence: int = Field(ge=1)
    state: AttemptState
    started_at: datetime
    expected_end_at: datetime | None = None
    deadline_at: datetime | None = None
    current_revision: Revision
    submitted_at: datetime | None = None
    paste_policy: PastePolicy = "INTERNAL_ONLY"
    ai_enabled: bool = False
    multi_file: bool = False
    last_checkpoint_at: datetime | None = None
    checkpoint_status: CheckpointStatus = "SYNCED"
    closure_reason: AttemptClosureReason | None = None
    # True only for an upgrading LMS-owned ACTIVE row which predates the live
    # Moodle preparation contract.  It reveals no connector policy; clients
    # use it to call the idempotent start endpoint before showing the editor.
    requires_live_lms_preparation: bool = False


class AttemptTeacherRead(AttemptStudentRead):
    assigned_task_version_id: UUID | None = None
    principal_id: UUID
    epoch: int = Field(ge=1)
    submission_source: Annotated[str, StringConstraints(max_length=32)] = ""
    reopen_reason: Annotated[str, StringConstraints(max_length=20_000)] = ""
    integrity_policy: JsonObject = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class WorkspaceFileRead(ReadModel):
    id: UUID
    path: str
    language: Annotated[str, StringConstraints(min_length=1, max_length=20)]
    content: str
    read_only: bool = False
    created_revision: Revision = 0
    deleted_revision: Revision | None = None

    _valid_path = field_validator("path")(validate_source_path)


class WorkspaceStudentRead(ReadModel):
    id: UUID
    attempt_id: UUID
    current_revision: Revision
    multi_file: bool
    aggregate_size: int = Field(ge=0)
    files: list[WorkspaceFileRead] = Field(default_factory=list, max_length=256)


class WorkspaceTeacherRead(WorkspaceStudentRead):
    current_hash: SHA256Hex
    event_chain_head: SHA256Hex
    created_at: datetime
    updated_at: datetime


class WorkspaceFilePatchRequest(MutationModel):
    content: SourceContent
    source: EditSource = "TYPING"
    receipt_id: UUID | None = None
    client_id: Annotated[str, StringConstraints(max_length=100)] = ""
    client_request_id: Annotated[str, StringConstraints(max_length=100)] | None = None

    @model_validator(mode="after")
    def receipt_matches_source(self) -> WorkspaceFilePatchRequest:
        if self.source == "INTERNAL_PASTE" and self.receipt_id is None:
            raise ValueError("receipt_id is required for INTERNAL_PASTE")
        if self.source == "TYPING" and self.receipt_id is not None:
            raise ValueError("receipt_id is only valid for INTERNAL_PASTE")
        return self


class WorkspaceFilePatchRead(ReadModel):
    file: WorkspaceFileRead
    revision: Revision
    workspace_revision: Revision | None = None


class WorkspaceFileCreateRequest(MutationModel):
    path: str
    language: Annotated[str, StringConstraints(min_length=1, max_length=20)] | None = None

    _valid_path = field_validator("path")(validate_source_path)


class WorkspaceFileCreateRead(ReadModel):
    file: WorkspaceFileRead
    revision: Revision


class WorkspaceFileDeleteRequest(EmptyMutation):
    pass


class WorkspaceFileDeleteRead(ReadModel):
    file_id: UUID
    revision: Revision


class ContiguousEditChange(ReadModel):
    type: Literal["contiguous_delta"]
    offset: int = Field(ge=0)
    delete_count: int = Field(ge=0)
    insert_text: Annotated[str, StringConstraints(max_length=2_097_152)] = ""
    offset_encoding: Literal["unicode_codepoint"]
    previous_content_hash: SHA256Hex
    content_hash: SHA256Hex


class FileCreateChange(ReadModel):
    type: Literal["file_create"]
    path: str
    content_hash: SHA256Hex

    _valid_path = field_validator("path")(validate_source_path)


class FileDeleteChange(ReadModel):
    type: Literal["file_delete"]
    path: str
    content_hash: SHA256Hex

    _valid_path = field_validator("path")(validate_source_path)


EditChange = Annotated[
    ContiguousEditChange | FileCreateChange | FileDeleteChange,
    Field(discriminator="type"),
]


class ClientContextRead(ReadModel):
    """Minimal, self-reported client metadata retained for attempt auditing."""

    ip_address: Annotated[str, StringConstraints(max_length=64)] = ""
    browser: Annotated[str, StringConstraints(max_length=80)] = ""
    browser_version: Annotated[str, StringConstraints(max_length=32)] = ""
    operating_system: Annotated[str, StringConstraints(max_length=80)] = ""
    device_type: Annotated[str, StringConstraints(max_length=16)] = ""


class EditEventRead(ReadModel):
    id: UUID
    epoch: int = Field(ge=1)
    sequence: Revision
    client_id: str = ""
    client_request_id: str
    source: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    event_type: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    file_id: UUID | None = None
    changes: list[EditChange] = Field(default_factory=list, max_length=100_000)
    previous_hash: Annotated[str, StringConstraints(max_length=64)] = ""
    event_hash: SHA256Hex
    received_at: datetime
    client: ClientContextRead | None = None


class AttemptHistoryEventRead(ReadModel):
    id: UUID
    type: Literal["edit", "run", "snapshot", "paste_blocked", "internal_paste", "submit"]
    label: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    detail: Annotated[str, StringConstraints(max_length=2_000)] | None = None
    at: datetime
    revision: Revision
    event: EditEventRead | None = None
    client: ClientContextRead | None = None


class ClipboardReceiptCreateRequest(MutationModel):
    """file_id identifies the copied source; the receipt remains attempt-scoped."""

    file_id: UUID
    text: Annotated[str, StringConstraints(min_length=1, max_length=1_048_576)]
    revision: Revision


class ClipboardReceiptRead(ReadModel):
    id: UUID
    expires_at: datetime


class SnapshotTeacherRead(ReadModel):
    id: UUID
    workspace_id: UUID
    revision: Revision
    event_chain_head: SHA256Hex
    manifest_hash: SHA256Hex
    files: list[JsonObject]
    reason: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    created_at: datetime


class AttemptSubmitRequest(MutationModel):
    revision: Revision


class AttemptSubmitRead(ReadModel):
    submission_id: UUID
    receipt_id: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    submitted_at: datetime
    revision: Revision


__all__ = [
    "AttemptHistoryEventRead",
    "AttemptStartRequest",
    "AttemptStudentRead",
    "AttemptSubmitRead",
    "AttemptSubmitRequest",
    "AttemptTeacherRead",
    "CheckpointStatus",
    "ClientContextRead",
    "ClipboardReceiptCreateRequest",
    "ClipboardReceiptRead",
    "ContiguousEditChange",
    "EditChange",
    "EditEventRead",
    "EditSource",
    "FileCreateChange",
    "FileDeleteChange",
    "SnapshotTeacherRead",
    "WorkspaceFileCreateRead",
    "WorkspaceFileCreateRequest",
    "WorkspaceFileDeleteRead",
    "WorkspaceFileDeleteRequest",
    "WorkspaceFilePatchRead",
    "WorkspaceFilePatchRequest",
    "WorkspaceFileRead",
    "WorkspaceStudentRead",
    "WorkspaceTeacherRead",
]
