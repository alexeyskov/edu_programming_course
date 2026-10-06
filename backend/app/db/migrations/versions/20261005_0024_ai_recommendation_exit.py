"""Track explicit approval or dismissal of an AI recommendation on leaving review."""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0024"
down_revision = "20261005_0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("core_aicodereview") as batch:
        batch.add_column(sa.Column("approved_decision_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("discarded_at", sa.DateTime(timezone=True)))
        batch.add_column(sa.Column("moodle_refresh_outbox_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column(
            "moodle_refresh_error", sa.Text(), nullable=False, server_default="",
        ))
        batch.create_foreign_key(
            "fk_ai_review_approval", "core_reviewdecision", ["approved_decision_id"], ["id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_ai_review_refresh", "core_syncoutbox", ["moodle_refresh_outbox_id"], ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("core_aicodereview") as batch:
        batch.drop_constraint("fk_ai_review_refresh", type_="foreignkey")
        batch.drop_constraint("fk_ai_review_approval", type_="foreignkey")
        batch.drop_column("moodle_refresh_error")
        batch.drop_column("moodle_refresh_outbox_id")
        batch.drop_column("discarded_at")
        batch.drop_column("approved_decision_id")
