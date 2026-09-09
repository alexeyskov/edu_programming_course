"""persist and constrain effective runner isolation policy

Revision ID: 20260824_0002
Revises: 20260824_0001
Create Date: 2026-08-24 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0002"
down_revision: str | Sequence[str] | None = "20260824_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch operations keep the development SQLite migration path functional
    # while emitting normal ALTER TABLE operations on PostgreSQL.
    with op.batch_alter_table("core_runrequest") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_core_runrequest_runrequest_network_disabled"),
            "network_enabled = false",
        )
        batch_op.create_index(
            "core_runrequest_user_status_updated_idx",
            ["requested_by_id", "status", "updated_at"],
            unique=False,
        )
        batch_op.create_index(
            "core_runrequest_user_created_idx",
            ["requested_by_id", "created_at"],
            unique=False,
        )
    with op.batch_alter_table("core_runresult") as batch_op:
        batch_op.add_column(sa.Column("filesystem_isolated", sa.Boolean(), nullable=True))
        batch_op.add_column(sa.Column("network_enabled", sa.Boolean(), nullable=True))
        batch_op.create_check_constraint(
            op.f("ck_core_runresult_runresult_network_disabled"),
            "network_enabled IS NOT TRUE",
        )
        batch_op.create_check_constraint(
            op.f("ck_core_runresult_runresult_filesystem_isolated"),
            "filesystem_isolated IS NULL OR filesystem_isolated = true",
        )


def downgrade() -> None:
    with op.batch_alter_table("core_runresult") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_core_runresult_runresult_filesystem_isolated"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_core_runresult_runresult_network_disabled"),
            type_="check",
        )
        batch_op.drop_column("network_enabled")
        batch_op.drop_column("filesystem_isolated")
    with op.batch_alter_table("core_runrequest") as batch_op:
        batch_op.drop_index("core_runrequest_user_created_idx")
        batch_op.drop_index("core_runrequest_user_status_updated_idx")
        batch_op.drop_constraint(
            op.f("ck_core_runrequest_runrequest_network_disabled"),
            type_="check",
        )
