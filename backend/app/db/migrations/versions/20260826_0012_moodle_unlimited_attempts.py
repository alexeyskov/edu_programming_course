"""preserve Moodle unlimited-attempt semantics

Revision ID: 20260826_0012
Revises: 20260826_0011
Create Date: 2026-08-26 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0012"
down_revision: str | Sequence[str] | None = "20260826_0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("core_assessment") as batch_op:
        batch_op.alter_column(
            "attempt_limit",
            existing_type=sa.SmallInteger(),
            nullable=True,
        )


def downgrade() -> None:
    # A downgrade cannot represent Moodle's unlimited value; use the original
    # conservative one-attempt default before restoring NOT NULL.
    op.execute("UPDATE core_assessment SET attempt_limit = 1 WHERE attempt_limit IS NULL")
    with op.batch_alter_table("core_assessment") as batch_op:
        batch_op.alter_column(
            "attempt_limit",
            existing_type=sa.SmallInteger(),
            nullable=False,
        )
