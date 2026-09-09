"""add the administrator-managed global course catalogue

Revision ID: 20260825_0009
Revises: 20260825_0008
Create Date: 2026-08-25 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260825_0009"
down_revision: str | Sequence[str] | None = "20260825_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("core_course") as batch_op:
        batch_op.add_column(
            sa.Column(
                "catalog_enabled",
                sa.Boolean(),
                server_default=sa.false(),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column("catalog_added_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            "core_course_connection_catalog_idx",
            ["connection_id", "catalog_enabled"],
            unique=False,
        )

    # Preserve courses that went through the explicit discovery + confirmation
    # workflow.  Rows created implicitly by older pluginless logins have no
    # confirmed import and therefore remain outside the catalogue.
    course = sa.table(
        "core_course",
        sa.column("id", sa.Uuid()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
        sa.column("catalog_enabled", sa.Boolean()),
        sa.column("catalog_added_at", sa.DateTime(timezone=True)),
    )
    import_job = sa.table(
        "core_courseimportjob",
        sa.column("confirmed_course_id", sa.Uuid()),
        sa.column("state", sa.String(length=20)),
    )
    confirmed_course_ids = sa.select(import_job.c.confirmed_course_id).where(
        import_job.c.state == "CONFIRMED",
        import_job.c.confirmed_course_id.is_not(None),
    )
    bind = op.get_bind()
    bind.execute(
        sa.update(course)
        .where(course.c.id.in_(confirmed_course_ids))
        .values(catalog_enabled=True, catalog_added_at=course.c.updated_at)
    )

    # Older pluginless login revisions also activated memberships for every
    # dashboard course.  Revoke only memberships outside the preserved list.
    membership = sa.table(
        "core_coursemembership",
        sa.column("course_id", sa.Uuid()),
        sa.column("active", sa.Boolean()),
    )
    bind.execute(
        sa.update(membership)
        .where(membership.c.course_id.not_in(confirmed_course_ids))
        .values(active=False)
    )


def downgrade() -> None:
    with op.batch_alter_table("core_course") as batch_op:
        batch_op.drop_index("core_course_connection_catalog_idx")
        batch_op.drop_column("catalog_added_at")
        batch_op.drop_column("catalog_enabled")
