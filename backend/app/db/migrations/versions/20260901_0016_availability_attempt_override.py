"""support Moodle target-specific attempt limits

Revision ID: 20260901_0016
Revises: 20260830_0015
Create Date: 2026-09-01 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0016"
down_revision: str | Sequence[str] | None = "20260830_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "core_availabilityrule",
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
    )
    op.add_column(
        "core_availabilityrule",
        sa.Column("attempt_limit", sa.SmallInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("core_availabilityrule", "attempt_limit")
    op.drop_column("core_availabilityrule", "duration_seconds")
