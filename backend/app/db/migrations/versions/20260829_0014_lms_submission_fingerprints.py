"""store exact fingerprints for answers delivered to an LMS

Revision ID: 20260829_0014
Revises: 20260827_0013
Create Date: 2026-08-29 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260829_0014"
down_revision: str | Sequence[str] | None = "20260827_0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "core_lmssubmissionfingerprint",
        sa.Column("outbox_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("course_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("principal_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("module", sa.String(length=16), nullable=False),
        sa.Column("external_activity_id", sa.String(length=64), nullable=False),
        sa.Column("external_attempt_id", sa.String(length=160), nullable=False, server_default=""),
        sa.Column(
            "external_question_slot",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
        sa.Column("answer_transport", sa.String(length=32), nullable=False),
        sa.Column("artifact_filename", sa.String(length=255), nullable=False),
        sa.Column("artifact_size", sa.BigInteger(), nullable=False),
        sa.Column("artifact_md5", sa.String(length=32), nullable=False),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False),
        sa.Column("comparison_md5", sa.String(length=32), nullable=False),
        sa.Column("comparison_sha256", sa.String(length=64), nullable=False),
        sa.Column("canonicalization", sa.String(length=32), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assessment_id"], ["core_assessment.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["attempt_id"], ["core_attempt.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["connection_id"], ["core_lmsconnection.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["course_id"], ["core_course.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["outbox_id"], ["core_syncoutbox.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["principal_id"], ["core_externalprincipal.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["snapshot_id"], ["core_snapshot.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submission_id"], ["core_submission.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("outbox_id", name="unique_lms_submission_fingerprint_outbox"),
    )
    op.create_index(
        "core_lmsfingerprint_lookup_idx",
        "core_lmssubmissionfingerprint",
        ["course_id", "assessment_id", "principal_id", "delivered_at"],
    )
    op.create_index(
        "core_lmsfingerprint_remote_lookup_idx",
        "core_lmssubmissionfingerprint",
        [
            "course_id",
            "module",
            "external_activity_id",
            "principal_id",
            "external_attempt_id",
            "external_question_slot",
            "delivered_at",
        ],
    )
    for column in (
        "outbox_id",
        "connection_id",
        "course_id",
        "assessment_id",
        "principal_id",
        "attempt_id",
        "submission_id",
        "snapshot_id",
    ):
        op.create_index(
            f"ix_core_lmssubmissionfingerprint_{column}",
            "core_lmssubmissionfingerprint",
            [column],
        )


def downgrade() -> None:
    op.drop_table("core_lmssubmissionfingerprint")
