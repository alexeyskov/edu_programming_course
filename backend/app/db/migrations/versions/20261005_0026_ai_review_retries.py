"""Persist the bounded retry budget for advisory AI reviews."""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0026"
down_revision = "20261005_0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("core_aicodereview") as batch:
        batch.add_column(sa.Column(
            "attempt_count", sa.Integer(), nullable=False, server_default="0",
        ))
        batch.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_check_constraint("ai_review_attempt_count", "attempt_count >= 0")


def downgrade() -> None:
    with op.batch_alter_table("core_aicodereview") as batch:
        batch.drop_constraint("ai_review_attempt_count", type_="check")
        batch.drop_column("next_attempt_at")
        batch.drop_column("attempt_count")
