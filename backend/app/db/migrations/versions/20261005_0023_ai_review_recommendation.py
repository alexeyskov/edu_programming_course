"""Keep AI grade suggestions separate from the official teacher decision."""

import sqlalchemy as sa
from alembic import op

revision = "20261005_0023"
down_revision = "20261004_0022"
branch_labels = None
depends_on = None


def _state_constraint_name():
    constraints = sa.inspect(op.get_bind()).get_check_constraints("core_aicodereview")
    return next(
        item["name"] for item in constraints
        if item["name"].endswith("ai_review_state")
    )


def upgrade() -> None:
    name = _state_constraint_name()
    with op.batch_alter_table("core_aicodereview") as batch:
        batch.add_column(sa.Column("recommended_grade", sa.Numeric(8, 2), nullable=True))
        batch.add_column(sa.Column(
            "recommended_comment", sa.Text(), nullable=False, server_default="",
        ))
        batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(
            op.f(name), "state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'SKIPPED')",
        )


def downgrade() -> None:
    name = _state_constraint_name()
    op.execute("UPDATE core_aicodereview SET state = 'FAILED' WHERE state = 'SKIPPED'")
    with op.batch_alter_table("core_aicodereview") as batch:
        batch.drop_constraint(op.f(name), type_="check")
        batch.create_check_constraint(
            op.f(name), "state IN ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')",
        )
        batch.drop_column("recommended_comment")
        batch.drop_column("recommended_grade")
