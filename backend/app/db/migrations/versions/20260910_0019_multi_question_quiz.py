"""Bind separate question workspaces to one Moodle Quiz attempt.

Revision ID: 20260910_0019
Revises: 20260904_0018
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260910_0019"
down_revision: str | Sequence[str] | None = "20260904_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_moodlequizquestion",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("root_attempt_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("question_slot", sa.String(64), nullable=False),
        sa.Column("position", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("question_max_mark", sa.Numeric(16, 7), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["root_attempt_id"], ["core_attempt.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["attempt_id"], ["core_attempt.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("attempt_id", name="uq_core_moodlequizquestion_attempt_id"),
        sa.UniqueConstraint(
            "root_attempt_id", "question_slot", name="unique_quiz_session_slot"
        ),
        sa.UniqueConstraint(
            "root_attempt_id", "position", name="unique_quiz_session_position"
        ),
    )
    op.create_index(
        "ix_core_moodlequizquestion_root_attempt_id",
        "core_moodlequizquestion",
        ["root_attempt_id"],
    )
    with op.batch_alter_table("core_lmssubmissionfingerprint") as batch:
        batch.drop_constraint("unique_lms_submission_fingerprint_outbox", type_="unique")
        batch.create_unique_constraint(
            "unique_lms_fingerprint_outbox_slot", ["outbox_id", "external_question_slot"]
        )


def downgrade() -> None:
    # Multi-answer receipts cannot be collapsed to one outbox row without loss.
    # Let the unique constraint fail if such data exists instead of deleting it.
    with op.batch_alter_table("core_lmssubmissionfingerprint") as batch:
        batch.drop_constraint("unique_lms_fingerprint_outbox_slot", type_="unique")
        batch.create_unique_constraint("unique_lms_submission_fingerprint_outbox", ["outbox_id"])
    op.drop_table("core_moodlequizquestion")
