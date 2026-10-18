"""Enable advisory AI review by default for new assessments only."""

import sqlalchemy as sa
from alembic import op

revision = "20261019_0027"
down_revision = "20261005_0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Do not overwrite saved administrator choices or enqueue historical work.
    with op.batch_alter_table("core_assessment") as batch:
        batch.alter_column(
            "ai_review_enabled",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.true(),
        )


def downgrade() -> None:
    with op.batch_alter_table("core_assessment") as batch:
        batch.alter_column(
            "ai_review_enabled",
            existing_type=sa.Boolean(),
            existing_nullable=False,
            server_default=sa.false(),
        )
