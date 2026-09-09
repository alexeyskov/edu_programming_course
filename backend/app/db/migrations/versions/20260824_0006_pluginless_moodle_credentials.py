"""add encrypted pluginless Moodle credentials and login throttling

Revision ID: 20260824_0006
Revises: 20260824_0005
Create Date: 2026-08-24 18:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

Text = sa.Text

revision: str = "20260824_0006"
down_revision: str | Sequence[str] | None = "20260824_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_moodlecredential",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("encrypted_secret", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "metadata_json",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql"),
            nullable=False,
        ),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["core_lmsconnection.id"],
            name=op.f("fk_core_moodlecredential_connection_id_core_lmsconnection"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["core_externalprincipal.id"],
            name=op.f("fk_core_moodlecredential_principal_id_core_externalprincipal"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_core_moodlecredential")),
        sa.UniqueConstraint(
            "connection_id",
            "principal_id",
            "kind",
            name="unique_moodle_principal_credential",
        ),
    )
    with op.batch_alter_table("core_moodlecredential") as batch_op:
        batch_op.create_index(
            "core_moodlecredential_status_idx", ["connection_id", "status"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_core_moodlecredential_connection_id"),
            ["connection_id"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_core_moodlecredential_principal_id"),
            ["principal_id"],
            unique=False,
        )

    op.create_table(
        "core_moodleloginattempt",
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("network_hash", sa.String(length=64), nullable=False),
        sa.Column("username_hash", sa.String(length=64), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["connection_id"],
            ["core_lmsconnection.id"],
            name=op.f("fk_core_moodleloginattempt_connection_id_core_lmsconnection"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_core_moodleloginattempt")),
    )
    with op.batch_alter_table("core_moodleloginattempt") as batch_op:
        batch_op.create_index(
            "core_moodleloginattempt_rate_idx",
            ["connection_id", "network_hash", "attempted_at"],
            unique=False,
        )
        batch_op.create_index(
            "core_moodleloginattempt_user_rate_idx",
            ["connection_id", "username_hash", "attempted_at"],
            unique=False,
        )
        batch_op.create_index(
            batch_op.f("ix_core_moodleloginattempt_connection_id"),
            ["connection_id"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("core_moodleloginattempt") as batch_op:
        batch_op.drop_index(batch_op.f("ix_core_moodleloginattempt_connection_id"))
        batch_op.drop_index("core_moodleloginattempt_user_rate_idx")
        batch_op.drop_index("core_moodleloginattempt_rate_idx")
    op.drop_table("core_moodleloginattempt")

    with op.batch_alter_table("core_moodlecredential") as batch_op:
        batch_op.drop_index(batch_op.f("ix_core_moodlecredential_principal_id"))
        batch_op.drop_index(batch_op.f("ix_core_moodlecredential_connection_id"))
        batch_op.drop_index("core_moodlecredential_status_idx")
    op.drop_table("core_moodlecredential")
