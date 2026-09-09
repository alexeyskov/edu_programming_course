"""record every successfully delivered LMS checkpoint

Revision ID: 20260830_0015
Revises: 20260829_0014
Create Date: 2026-08-30 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0015"
down_revision: str | Sequence[str] | None = "20260829_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "core_lmssubmissionfingerprint",
        "submission_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
    op.add_column(
        "core_lmssubmissionfingerprint",
        sa.Column("checkpoint_reason", sa.String(length=32), nullable=False, server_default=""),
    )
    op.add_column(
        "core_lmssubmissionfingerprint",
        sa.Column("terminal", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("core_lmssubmissionfingerprint", "terminal")
    op.drop_column("core_lmssubmissionfingerprint", "checkpoint_reason")
    op.alter_column(
        "core_lmssubmissionfingerprint",
        "submission_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
