"""record the network and browser context of student activity

Revision ID: 20260903_0017
Revises: 20260901_0016
Create Date: 2026-09-03 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260903_0017"
down_revision: str | Sequence[str] | None = "20260901_0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json_type() -> sa.JSON:
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )


def upgrade() -> None:
    for table_name in (
        "core_attempt",
        "core_editevent",
        "core_submission",
        "core_runrequest",
    ):
        op.add_column(
            table_name,
            sa.Column(
                "client_context",
                _json_type(),
                nullable=False,
                server_default=sa.text("'{}'"),
            ),
        )


def downgrade() -> None:
    for table_name in (
        "core_runrequest",
        "core_submission",
        "core_editevent",
        "core_attempt",
    ):
        op.drop_column(table_name, "client_context")
