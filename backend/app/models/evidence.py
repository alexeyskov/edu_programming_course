from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import UUIDTimestampModel
from app.models.types import JSONValue


class EvidenceReport(UUIDTimestampModel):
    """Auditable aggregate of deterministic hidden-test executions.

    This is decision-support evidence only. Deliberately, it has no grade, score,
    or automatic-decision field.
    """

    __tablename__ = "core_evidencereport"
    __table_args__ = (
        CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED')",
            name="evidence_report_status",
        ),
        CheckConstraint(
            "passed_cases >= 0 AND total_cases >= 0 AND passed_cases <= total_cases",
            name="evidence_report_case_counts",
        ),
        Index(
            "core_evidence_submission_status_created_idx",
            "submission_id",
            "status",
            "created_at",
        ),
        Index(
            "one_running_evidence_report_per_snapshot",
            "submission_id",
            "snapshot_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
            sqlite_where=text("status = 'RUNNING'"),
        ),
        UniqueConstraint(
            "submission_id",
            "requested_by_id",
            "idempotency_key_hash",
            name="unique_evidence_request_key",
        ),
    )

    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_submission.id", ondelete="RESTRICT"), index=True
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_snapshot.id", ondelete="RESTRICT"), index=True
    )
    task_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_taskversion.id", ondelete="RESTRICT"), index=True
    )
    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("core_externalprincipal.id", ondelete="RESTRICT"), index=True
    )
    idempotency_key_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    hidden_test_manifest_hash: Mapped[str] = mapped_column(String(64))
    task_content_hash: Mapped[str] = mapped_column(String(64))
    snapshot_manifest_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="RUNNING")
    passed_cases: Mapped[int] = mapped_column(Integer, default=0)
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    outcomes: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    findings: Mapped[list[Any]] = mapped_column(JSONValue, default=list)
    failure_code: Mapped[str] = mapped_column(String(100), default="")
    failure_message: Mapped[str] = mapped_column(Text, default="")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["EvidenceReport"]
