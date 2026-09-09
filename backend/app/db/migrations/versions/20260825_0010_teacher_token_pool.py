"""add the revocable teacher-token pool

Revision ID: 20260825_0010
Revises: 20260825_0009
Create Date: 2026-08-25 18:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0010"
down_revision: str | Sequence[str] | None = "20260825_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_teacheraccesstoken",
        sa.Column("public_id", sa.String(length=24), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("secret_hash", sa.String(length=255), nullable=False),
        sa.Column("created_by_id", sa.Uuid(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by_id"],
            ["core_externalprincipal.id"],
            name="fk_core_teacheraccesstoken_created_by_id_core_externalprincipal",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_core_teacheraccesstoken"),
        sa.UniqueConstraint("public_id", name="uq_core_teacheraccesstoken_public_id"),
    )
    op.create_index(
        "ix_core_teacheraccesstoken_created_by_id",
        "core_teacheraccesstoken",
        ["created_by_id"],
        unique=False,
    )
    op.create_index(
        "ix_core_teacheraccesstoken_public_id",
        "core_teacheraccesstoken",
        ["public_id"],
        unique=True,
    )
    op.create_table(
        "core_teachertokengrant",
        sa.Column("token_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["principal_id"],
            ["core_externalprincipal.id"],
            name="fk_core_teachertokengrant_principal_id_core_externalprincipal",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["token_id"],
            ["core_teacheraccesstoken.id"],
            name="fk_core_teachertokengrant_token_id_core_teacheraccesstoken",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_core_teachertokengrant"),
        sa.UniqueConstraint("principal_id", name="unique_principal_teacher_grant"),
        sa.UniqueConstraint("token_id", name="unique_teacher_token_grant"),
    )
    op.create_index(
        "ix_core_teachertokengrant_principal_id",
        "core_teachertokengrant",
        ["principal_id"],
        unique=False,
    )
    op.create_index(
        "ix_core_teachertokengrant_token_id",
        "core_teachertokengrant",
        ["token_id"],
        unique=False,
    )
    with op.batch_alter_table("core_logintransaction") as batch_op:
        batch_op.add_column(sa.Column("teacher_token_id", sa.Uuid(), nullable=True))
        batch_op.create_index(
            "ix_core_logintransaction_teacher_token_id",
            ["teacher_token_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            op.f("fk_core_logintransaction_teacher_token_id_core_teacheraccesstoken"),
            "core_teacheraccesstoken",
            ["teacher_token_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # From this revision onward Moodle course markup proves enrollment only;
    # a platform teacher-token binding is required for the TEACHER role.
    membership = sa.table(
        "core_coursemembership",
        sa.column("role", sa.String(length=10)),
        sa.column("active", sa.Boolean()),
    )
    op.get_bind().execute(
        sa.update(membership)
        .where(membership.c.role == "TEACHER", membership.c.active.is_(True))
        .values(active=False)
    )


def downgrade() -> None:
    with op.batch_alter_table("core_logintransaction") as batch_op:
        batch_op.drop_constraint(
            op.f("fk_core_logintransaction_teacher_token_id_core_teacheraccesstoken"),
            type_="foreignkey",
        )
        batch_op.drop_index("ix_core_logintransaction_teacher_token_id")
        batch_op.drop_column("teacher_token_id")
    op.drop_index(
        "ix_core_teachertokengrant_token_id",
        table_name="core_teachertokengrant",
    )
    op.drop_index(
        "ix_core_teachertokengrant_principal_id",
        table_name="core_teachertokengrant",
    )
    op.drop_table("core_teachertokengrant")
    op.drop_index(
        "ix_core_teacheraccesstoken_public_id",
        table_name="core_teacheraccesstoken",
    )
    op.drop_index(
        "ix_core_teacheraccesstoken_created_by_id",
        table_name="core_teacheraccesstoken",
    )
    op.drop_table("core_teacheraccesstoken")
