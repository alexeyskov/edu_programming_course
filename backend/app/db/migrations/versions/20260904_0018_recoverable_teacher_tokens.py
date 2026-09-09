"""add encrypted recoverable teacher tokens

Revision ID: 20260904_0018
Revises: 20260903_0017
Create Date: 2026-09-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260904_0018"
down_revision: str | Sequence[str] | None = "20260903_0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows intentionally remain NULL: their Argon2id verifiers cannot
    # be reversed. They keep authenticating and become revealable after an
    # administrator rotates them through the protected API.
    op.add_column(
        "core_teacheraccesstoken",
        sa.Column("encrypted_secret", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("core_teacheraccesstoken", "encrypted_secret")
