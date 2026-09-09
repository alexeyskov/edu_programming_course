"""materialise Playwright for legacy pluginless Moodle connections

Revision ID: 20260825_0008
Revises: 20260824_0007
Create Date: 2026-08-25 01:00:00.000000
"""

from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0008"
down_revision: str | Sequence[str] | None = "20260824_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _playwright_config(provider: object, config: object) -> dict[str, Any] | None:
    """Return an upgraded config only for an implicit pluginless Moodle row."""

    if str(provider).upper() != "MOODLE" or not isinstance(config, Mapping):
        return None
    current = dict(config)
    if str(current.get("auth_mode", "PLUGINLESS")).upper() != "PLUGINLESS":
        return None
    if "pluginless_transport" in current:
        return None
    return {**current, "pluginless_transport": "PLAYWRIGHT"}


def upgrade() -> None:
    connection = sa.table(
        "core_lmsconnection",
        sa.column("id", sa.Uuid()),
        sa.column("provider", sa.String(length=20)),
        sa.column("config", sa.JSON()),
    )
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(connection.c.id, connection.c.provider, connection.c.config)
    ).mappings()
    for row in rows:
        upgraded = _playwright_config(row["provider"], row["config"])
        if upgraded is None:
            continue
        bind.execute(
            sa.update(connection).where(connection.c.id == row["id"]).values(config=upgraded)
        )


def downgrade() -> None:
    # This data migration is intentionally irreversible. Removing the explicit
    # marker would silently switch migrated installations back to MOBILE_TOKEN;
    # older application revisions already understand an explicit PLAYWRIGHT.
    pass
