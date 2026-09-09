"""persist bounded course synchronization diagnostics

Revision ID: 20260827_0013
Revises: 20260826_0012
Create Date: 2026-08-27 20:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0013"
down_revision: str | Sequence[str] | None = "20260826_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("core_course") as batch_op:
        batch_op.add_column(
            sa.Column("sync_error_code", sa.String(length=64), nullable=False, server_default="")
        )
        batch_op.add_column(
            sa.Column("sync_error_message", sa.Text(), nullable=False, server_default="")
        )
        batch_op.add_column(sa.Column("sync_error_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(
            sa.Column(
                "sync_error_retryable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("core_course") as batch_op:
        batch_op.drop_column("sync_error_retryable")
        batch_op.drop_column("sync_error_at")
        batch_op.drop_column("sync_error_message")
        batch_op.drop_column("sync_error_code")
