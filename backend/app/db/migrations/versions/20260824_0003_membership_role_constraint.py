"""constrain LMS-projected course membership roles

Revision ID: 20260824_0003
Revises: 20260824_0002
Create Date: 2026-08-24 13:30:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260824_0003"
down_revision: str | Sequence[str] | None = "20260824_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch mode also supports the SQLite development migration path.
    with op.batch_alter_table("core_coursemembership") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_core_coursemembership_coursemembership_supported_role"),
            "role IN ('STUDENT', 'TEACHER')",
        )


def downgrade() -> None:
    with op.batch_alter_table("core_coursemembership") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_core_coursemembership_coursemembership_supported_role"),
            type_="check",
        )
