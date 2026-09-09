"""allow the explicitly unrestricted container runner

Revision ID: 20260826_0011
Revises: 20260825_0010
Create Date: 2026-08-26 04:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "20260826_0011"
down_revision: str | Sequence[str] | None = "20260825_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The selected execution policy deliberately runs a normal subprocess in
    # the dedicated runner container.  Persist the truthful policy returned by
    # the runner instead of rejecting/rewriting it as isolated.
    with op.batch_alter_table("core_runresult") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_core_runresult_runresult_filesystem_isolated"),
            type_="check",
        )
        batch_op.drop_constraint(
            op.f("ck_core_runresult_runresult_network_disabled"),
            type_="check",
        )
    with op.batch_alter_table("core_runrequest") as batch_op:
        batch_op.drop_constraint(
            op.f("ck_core_runrequest_runrequest_network_disabled"),
            type_="check",
        )


def downgrade() -> None:
    # Existing unrestricted results cannot satisfy the old constraints.  Keep
    # the downgrade deterministic by clearing only policy observations; run
    # output, diagnostics and audit rows are preserved.
    op.execute(
        "UPDATE core_runresult SET filesystem_isolated = NULL, "
        "network_enabled = NULL WHERE filesystem_isolated IS NOT TRUE "
        "OR network_enabled IS NOT FALSE"
    )
    op.execute("UPDATE core_runrequest SET network_enabled = false")
    with op.batch_alter_table("core_runrequest") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_core_runrequest_runrequest_network_disabled"),
            "network_enabled = false",
        )
    with op.batch_alter_table("core_runresult") as batch_op:
        batch_op.create_check_constraint(
            op.f("ck_core_runresult_runresult_network_disabled"),
            "network_enabled IS NOT TRUE",
        )
        batch_op.create_check_constraint(
            op.f("ck_core_runresult_runresult_filesystem_isolated"),
            "filesystem_isolated IS NULL OR filesystem_isolated = true",
        )
