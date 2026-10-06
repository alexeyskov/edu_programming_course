"""Opt-in advisory AI review for immutable submission snapshots."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004_0021"
down_revision = "20260925_0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "core_assessment",
        sa.Column("ai_review_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "core_aicodereview",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("requested_by_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("run_token", sa.String(32), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "findings", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["submission_id"], ["core_submission.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["snapshot_id"], ["core_snapshot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["requested_by_id"], ["core_externalprincipal.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("submission_id", "snapshot_id", name="unique_ai_review_snapshot"),
        sa.CheckConstraint(
            "state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')", name="ai_review_state"
        ),
    )
    op.create_index("ix_core_aicodereview_submission_id", "core_aicodereview", ["submission_id"])
    op.create_index(
        "core_ai_review_state_created_idx", "core_aicodereview", ["state", "created_at"],
    )
    op.create_index(
        "ix_core_aicodereview_requested_by_id", "core_aicodereview", ["requested_by_id"]
    )


def downgrade() -> None:
    op.drop_table("core_aicodereview")
    op.drop_column("core_assessment", "ai_review_enabled")
