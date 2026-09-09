from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel, utcnow
from app.models.enums import AttemptState, RunOrigin, RunStatus
from app.models.types import JSONValue


class Attempt(UUIDTimestampModel):
    __tablename__ = "core_attempt"
    __table_args__ = (
        UniqueConstraint(
            "assessment_id", "principal_id", "sequence", name="unique_attempt_sequence"
        ),
    )

    assessment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_assessment.id", ondelete="RESTRICT"), index=True
    )
    # Nullable only for upgrading historical rows; every new attempt must snapshot its variant.
    assigned_task_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_taskversion.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    sequence: Mapped[int] = mapped_column(SmallInteger, default=1)
    state: Mapped[str] = mapped_column(String(20), default=AttemptState.ACTIVE.value)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expected_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    epoch: Mapped[int] = mapped_column(SmallInteger, default=1)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    submission_source: Mapped[str] = mapped_column(String(32), default="")
    reopen_reason: Mapped[str] = mapped_column(Text, default="")
    integrity_policy: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    client_context: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class Workspace(UUIDTimestampModel):
    __tablename__ = "core_workspace"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="CASCADE"), unique=True
    )
    current_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    current_hash: Mapped[str] = mapped_column(String(64), default="")
    multi_file: Mapped[bool] = mapped_column(Boolean, default=False)
    aggregate_size: Mapped[int] = mapped_column(BigInteger, default=0)
    event_chain_head: Mapped[str] = mapped_column(String(64), default="")


class WorkspaceFile(UUIDTimestampModel):
    __tablename__ = "core_workspacefile"
    __table_args__ = (UniqueConstraint("workspace_id", "path", name="unique_workspace_path"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_workspace.id", ondelete="CASCADE"), index=True
    )
    path: Mapped[str] = mapped_column(String(512))
    language: Mapped[str] = mapped_column(String(20), default="CPP")
    content: Mapped[str] = mapped_column(Text, default="")
    content_hash: Mapped[str] = mapped_column(String(64))
    created_revision: Mapped[int] = mapped_column(BigInteger, default=0)
    deleted_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class EditEvent(UUIDTimestampModel):
    __tablename__ = "core_editevent"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "client_request_id", name="unique_workspace_client_request"
        ),
        UniqueConstraint("workspace_id", "sequence", name="unique_event_sequence"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_workspace.id", ondelete="CASCADE"), index=True
    )
    epoch: Mapped[int] = mapped_column(SmallInteger)
    sequence: Mapped[int] = mapped_column(BigInteger)
    client_id: Mapped[str] = mapped_column(String(100), default="")
    client_request_id: Mapped[str] = mapped_column(String(100))
    source: Mapped[str] = mapped_column(String(32), default="TYPING")
    event_type: Mapped[str] = mapped_column(String(32), default="REPLACE_CONTENT")
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_workspacefile.id", ondelete="SET NULL"), nullable=True, index=True
    )
    changes: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    previous_hash: Mapped[str] = mapped_column(String(64), default="")
    event_hash: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    client_context: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class ClipboardReceipt(UUIDTimestampModel):
    __tablename__ = "core_clipboardreceipt"

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="CASCADE"), index=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_workspacefile.id", ondelete="CASCADE"), index=True
    )
    principal_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="CASCADE"), index=True
    )
    revision: Mapped[int] = mapped_column(BigInteger)
    text_hash: Mapped[str] = mapped_column(String(64))
    text_length: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Snapshot(UUIDTimestampModel):
    __tablename__ = "core_snapshot"
    __table_args__ = (
        UniqueConstraint("workspace_id", "revision", name="unique_snapshot_revision"),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_workspace.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(BigInteger)
    event_chain_head: Mapped[str] = mapped_column(String(64), default="")
    manifest_hash: Mapped[str] = mapped_column(String(64))
    files: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    reason: Mapped[str] = mapped_column(String(32))


class Submission(UUIDTimestampModel):
    __tablename__ = "core_submission"
    __table_args__ = (
        UniqueConstraint("attempt_id", "revision", name="unique_submission_revision"),
    )

    attempt_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="RESTRICT"), index=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_snapshot.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(SmallInteger, default=1)
    source: Mapped[str] = mapped_column(String(32))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    late: Mapped[bool] = mapped_column(Boolean, default=False)
    lms_export_state: Mapped[str] = mapped_column(String(32), default="PENDING")
    external_receipt: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    client_context: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class RunRequest(UUIDTimestampModel):
    __tablename__ = "core_runrequest"
    __table_args__ = (
        CheckConstraint(
            "(evidence_report_id IS NULL AND evidence_case_index IS NULL) OR "
            "(evidence_report_id IS NOT NULL AND evidence_case_index >= 0)",
            name="runrequest_evidence_case_binding",
        ),
        UniqueConstraint(
            "evidence_report_id",
            "evidence_case_index",
            name="unique_evidence_report_case",
        ),
        Index(
            "core_runrequest_user_status_updated_idx",
            "requested_by_id",
            "status",
            "updated_at",
        ),
        Index(
            "core_runrequest_user_created_idx",
            "requested_by_id",
            "created_at",
        ),
    )

    origin: Mapped[str] = mapped_column(String(32), default=RunOrigin.STUDENT_ATTEMPT.value)
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_attempt.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    teacher_experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_teacherexperiment.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    evidence_report_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("core_evidencereport.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    evidence_case_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    revision: Mapped[int] = mapped_column(BigInteger)
    mode: Mapped[str] = mapped_column(String(20), default="RUN")
    build_profile: Mapped[str] = mapped_column(String(100))
    filesystem_profile: Mapped[str] = mapped_column(String(100), default="UNRESTRICTED_CONTAINER")
    network_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    stdin: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.QUEUED.value)
    external_job_id: Mapped[str] = mapped_column(String(255), default="")
    client_context: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)


class RunResult(UUIDTimestampModel):
    __tablename__ = "core_runresult"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_runrequest.id", ondelete="CASCADE"), unique=True
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_reason: Mapped[str] = mapped_column(String(64), default="")
    stdout: Mapped[str] = mapped_column(Text, default="")
    stderr: Mapped[str] = mapped_column(Text, default="")
    diagnostics: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONValue, default=dict)
    executor_version: Mapped[str] = mapped_column(String(100), default="")
    filesystem_policy_version: Mapped[str] = mapped_column(String(100), default="")
    filesystem_isolated: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    network_enabled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
