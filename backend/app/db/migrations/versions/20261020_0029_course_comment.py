"""Add an administrator-managed display comment without changing LMS titles."""

import sqlalchemy as sa
from alembic import op

revision = "20261020_0029"
down_revision = "20261019_0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("core_course") as batch:
        batch.add_column(sa.Column("comment", sa.String(255), nullable=False, server_default=""))


def downgrade() -> None:
    with op.batch_alter_table("core_course") as batch:
        batch.drop_column("comment")
