"""add deterministic hidden-test evidence reports

Revision ID: 20260824_0005
Revises: 20260824_0004
Create Date: 2026-08-24 17:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

Text = sa.Text

revision: str = "20260824_0005"
down_revision: str | Sequence[str] | None = "20260824_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_evidencereport",
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("task_version_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key_hash", sa.String(length=64), nullable=True),
        sa.Column("hidden_test_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("task_content_hash", sa.String(length=64), nullable=False),
        sa.Column("snapshot_manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("passed_cases", sa.Integer(), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column(
            "outcomes",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column(
            "findings",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("failure_code", sa.String(length=100), nullable=False),
        sa.Column("failure_message", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "passed_cases >= 0 AND total_cases >= 0 AND passed_cases <= total_cases",
            name=op.f("ck_core_evidencereport_evidence_report_case_counts"),
        ),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'COMPLETED', 'FAILED')",
            name=op.f("ck_core_evidencereport_evidence_report_status"),
        ),
        sa.ForeignKeyConstraint(
            ["requested_by_id"],
            ["core_externalprincipal.id"],
            name=op.f("fk_core_evidencereport_requested_by_id_core_externalprincipal"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["core_snapshot.id"],
            name=op.f("fk_core_evidencereport_snapshot_id_core_snapshot"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id"],
            ["core_submission.id"],
            name=op.f("fk_core_evidencereport_submission_id_core_submission"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_version_id"],
            ["core_taskversion.id"],
            name=op.f("fk_core_evidencereport_task_version_id_core_taskversion"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_core_evidencereport")),
        sa.UniqueConstraint(
            "submission_id",
            "requested_by_id",
            "idempotency_key_hash",
            name="unique_evidence_request_key",
        ),
    )
    with op.batch_alter_table("core_evidencereport") as batch_op:
        batch_op.create_index(
            "core_evidence_submission_status_created_idx",
            ["submission_id", "status", "created_at"],
            unique=False,
        )
        batch_op.create_index(
            "one_running_evidence_report_per_snapshot",
            ["submission_id", "snapshot_id"],
            unique=True,
            postgresql_where=sa.text("status = 'RUNNING'"),
            sqlite_where=sa.text("status = 'RUNNING'"),
        )
        batch_op.create_index(
            batch_op.f("ix_core_evidencereport_requested_by_id"),
            ["requested_by_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_core_evidencereport_snapshot_id"),
            ["snapshot_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_core_evidencereport_submission_id"),
            ["submission_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_core_evidencereport_task_version_id"),
            ["task_version_id"],
            unique=False,
        )

    with op.batch_alter_table("core_runrequest") as batch_op:
        batch_op.add_column(sa.Column("evidence_report_id", sa.Uuid(), nullable=True))
        batch_op.add_column(sa.Column("evidence_case_index", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_core_runrequest_evidence_report_id_core_evidencereport"),
            "core_evidencereport",
            ["evidence_report_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            op.f("ck_core_runrequest_runrequest_evidence_case_binding"),
            "(evidence_report_id IS NULL AND evidence_case_index IS NULL) OR "
            "(evidence_report_id IS NOT NULL AND evidence_case_index >= 0)",
        )
        batch_op.create_unique_constraint(
            "unique_evidence_report_case",
            ["evidence_report_id", "evidence_case_index"],
        )
        batch_op.create_index(
            batch_op.f("ix_core_runrequest_evidence_report_id"),
            ["evidence_report_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("core_runrequest") as batch_op:
        batch_op.drop_index(batch_op.f("ix_core_runrequest_evidence_report_id"))
        batch_op.drop_constraint("unique_evidence_report_case", type_="unique")
        batch_op.drop_constraint(
            op.f("ck_core_runrequest_runrequest_evidence_case_binding"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("fk_core_runrequest_evidence_report_id_core_evidencereport"),
            type_="foreignkey",
        )
        batch_op.drop_column("evidence_case_index")
        batch_op.drop_column("evidence_report_id")

    with op.batch_alter_table("core_evidencereport") as batch_op:
        batch_op.drop_index(batch_op.f("ix_core_evidencereport_task_version_id"))
        batch_op.drop_index(batch_op.f("ix_core_evidencereport_submission_id"))
        batch_op.drop_index(batch_op.f("ix_core_evidencereport_snapshot_id"))
        batch_op.drop_index(batch_op.f("ix_core_evidencereport_requested_by_id"))
        batch_op.drop_index("core_evidence_submission_status_created_idx")
        batch_op.drop_index("one_running_evidence_report_per_snapshot")
    op.drop_table("core_evidencereport")
