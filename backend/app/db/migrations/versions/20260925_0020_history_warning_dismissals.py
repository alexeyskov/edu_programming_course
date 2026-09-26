"""Remember acknowledged import problems separately for each teacher."""

import sqlalchemy as sa
from alembic import op

revision = "20260925_0020"
down_revision = "20260910_0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "core_historywarningdismissal",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("warning_id", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["core_externalprincipal.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["assessment_id"], ["core_assessment.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "principal_id", "assessment_id", "warning_id", name="unique_history_warning_dismissal"
        ),
    )
    op.create_index(
        "ix_core_historywarningdismissal_assessment_id",
        "core_historywarningdismissal", ["assessment_id"],
    )


def downgrade() -> None:
    op.drop_table("core_historywarningdismissal")
