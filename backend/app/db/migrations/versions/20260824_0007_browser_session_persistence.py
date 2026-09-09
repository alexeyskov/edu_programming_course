"""add browser-session revision and lease fields

Revision ID: 20260824_0007
Revises: 20260824_0006
Create Date: 2026-08-24 20:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0007"
down_revision: str | Sequence[str] | None = "20260824_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("core_moodlecredential") as batch_op:
        # The server default backfills credentials created by the MOBILE_TOKEN
        # implementation and also keeps direct database inserts well-defined.
        batch_op.add_column(
            sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False)
        )
        batch_op.add_column(sa.Column("lease_owner", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("core_moodlecredential") as batch_op:
        batch_op.drop_column("last_used_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("revision")
