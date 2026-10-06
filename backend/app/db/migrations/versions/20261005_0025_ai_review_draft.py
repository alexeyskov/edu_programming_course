"""Keep an AI-derived draft separate from an approved teacher decision."""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0025"
down_revision = "20261005_0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("core_reviewdraft") as batch:
        batch.add_column(sa.Column("ai_review_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_review_draft_ai", "core_aicodereview", ["ai_review_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("core_reviewdraft") as batch:
        batch.drop_constraint("fk_review_draft_ai", type_="foreignkey")
        batch.drop_column("ai_review_id")
