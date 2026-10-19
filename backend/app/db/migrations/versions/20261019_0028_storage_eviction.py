"""Mark recoverable Moodle payloads evicted from the local cache."""

import sqlalchemy as sa
from alembic import op

revision = "20261019_0028"
down_revision = "20261019_0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("core_submission") as batch:
        batch.add_column(sa.Column("payload_evicted_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("core_submission") as batch:
        batch.drop_column("payload_evicted_at")
