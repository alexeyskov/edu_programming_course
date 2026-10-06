"""Track one-off teacher AI requests independently of automatic assessment policy."""

import sqlalchemy as sa
from alembic import op

revision = "20261004_0022"
down_revision = "20261004_0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("manual_requested", "requested_with_system_access"):
        op.add_column(
            "core_aicodereview",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.false()),
        )


def downgrade() -> None:
    op.drop_column("core_aicodereview", "requested_with_system_access")
    op.drop_column("core_aicodereview", "manual_requested")
