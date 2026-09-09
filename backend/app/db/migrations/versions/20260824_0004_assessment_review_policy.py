"""add assessment review and decision-support policy flags

Revision ID: 20260824_0004
Revises: 20260824_0003
Create Date: 2026-08-24 16:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0004"
down_revision: str | Sequence[str] | None = "20260824_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Defaults preserve the prior behavior for existing and connector-created rows.
    # Batch mode keeps the SQLite development migration path supported.
    with op.batch_alter_table("core_assessment") as batch_op:
        batch_op.add_column(
            sa.Column(
                "review_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "decision_support_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("core_assessment") as batch_op:
        batch_op.drop_column("decision_support_enabled")
        batch_op.drop_column("review_required")
