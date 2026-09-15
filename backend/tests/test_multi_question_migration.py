from __future__ import annotations

import importlib
import uuid

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_multi_question_migration_preserves_old_fingerprints_and_allows_distinct_slots():
    migration = importlib.import_module(
        "app.db.migrations.versions.20260910_0019_multi_question_quiz"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table("core_attempt", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    fingerprints = sa.Table(
        "core_lmssubmissionfingerprint",
        metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("outbox_id", sa.Uuid(), nullable=False),
        sa.Column("external_question_slot", sa.String(64), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("outbox_id", name="unique_lms_submission_fingerprint_outbox"),
    )
    outbox_id = uuid.uuid4()
    original = {
        "id": uuid.uuid4(),
        "outbox_id": outbox_id,
        "external_question_slot": "1",
        "artifact_sha256": "a" * 64,
    }
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(fingerprints.insert(), original)
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            assert dict(connection.execute(sa.select(fingerprints)).mappings().one()) == original
            connection.execute(
                fingerprints.insert(),
                {
                    **original,
                    "id": uuid.uuid4(),
                    "external_question_slot": "3",
                    "artifact_sha256": "b" * 64,
                },
            )
            assert connection.scalar(sa.select(sa.func.count()).select_from(fingerprints)) == 2
            constraints = sa.inspect(connection).get_unique_constraints(fingerprints.name)
            assert any(
                set(constraint["column_names"]) == {"outbox_id", "external_question_slot"}
                for constraint in constraints
            )
            assert "core_moodlequizquestion" in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
