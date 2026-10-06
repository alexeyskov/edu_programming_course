import importlib
import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.analysis import AICodeReview


def test_ai_review_migration_defaults_off_and_preserves_existing_assessments():
    migration = importlib.import_module("app.db.migrations.versions.20261004_0021_ai_code_review")
    manual_migration = importlib.import_module(
        "app.db.migrations.versions.20261004_0022_manual_ai_review"
    )
    recommendation_migration = importlib.import_module(
        "app.db.migrations.versions.20261005_0023_ai_review_recommendation"
    )
    exit_migration = importlib.import_module(
        "app.db.migrations.versions.20261005_0024_ai_recommendation_exit"
    )
    retry_migration = importlib.import_module(
        "app.db.migrations.versions.20261005_0026_ai_review_retries"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    assessment = sa.Table("core_assessment", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    for name in (
        "core_externalprincipal", "core_submission", "core_snapshot",
        "core_reviewdecision", "core_syncoutbox",
    ):
        sa.Table(name, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    original_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(assessment.insert(), {"id": original_id})
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            updated = sa.Table("core_assessment", sa.MetaData(), autoload_with=connection)
            row = connection.execute(sa.select(updated)).mappings().one()
            assert uuid.UUID(str(row["id"])) == original_id
            assert row["ai_review_enabled"] is False
            table = AICodeReview.__table__
            for name in ("core_externalprincipal", "core_submission", "core_snapshot"):
                connection.execute(metadata.tables[name].insert(), {"id": original_id})
            legacy = sa.Table(table.name, sa.MetaData(), autoload_with=connection)
            report_id = uuid.uuid4()
            connection.execute(
                legacy.insert(),
                {
                    "id": report_id.hex,
                    "created_at": datetime.now(UTC),
                    "updated_at": datetime.now(UTC),
                    "submission_id": original_id.hex,
                    "snapshot_id": original_id.hex,
                    "requested_by_id": original_id.hex,
                    "manifest_hash": "a" * 64,
                    "state": "COMPLETED",
                    "run_token": "b" * 32,
                    "prompt_version": "code-review-v1",
                    "summary": "Existing cached report",
                    "findings": [],
                    "model": "test-model",
                    "error": "",
                },
            )
            with Operations.context(MigrationContext.configure(connection)):
                manual_migration.upgrade()
                recommendation_migration.upgrade()
                exit_migration.upgrade()
                retry_migration.upgrade()
            migrated = sa.Table(table.name, sa.MetaData(), autoload_with=connection)
            report_row = connection.execute(sa.select(migrated)).mappings().one()
            assert uuid.UUID(str(report_row["id"])) == report_id
            assert report_row["summary"] == "Existing cached report"
            assert report_row["manual_requested"] is False
            assert report_row["requested_with_system_access"] is False
            assert report_row["recommended_grade"] is None
            assert report_row["recommended_comment"] == ""
            assert report_row["approved_decision_id"] is None
            assert report_row["discarded_at"] is None
            assert report_row["moodle_refresh_outbox_id"] is None
            assert report_row["moodle_refresh_error"] == ""
            assert report_row["attempt_count"] == 0
            assert report_row["next_attempt_at"] is None
            assert report_row["state"] == "COMPLETED"
            columns = sa.inspect(connection).get_columns(table.name)
            assert {column["name"] for column in columns} == set(table.columns.keys())
            for column in columns:
                if column["name"] in {"manual_requested", "requested_with_system_access"}:
                    assert column["nullable"] is False
                    assert column["default"] in {"0", "false"}
            uniques = sa.inspect(connection).get_unique_constraints(table.name)
            assert any(
                set(item["column_names"]) == {"submission_id", "snapshot_id"} for item in uniques
            )
            connection.execute(migrated.update().values(state="SKIPPED"))
            with Operations.context(MigrationContext.configure(connection)):
                retry_migration.downgrade()
                exit_migration.downgrade()
                recommendation_migration.downgrade()
                manual_migration.downgrade()
            assert connection.scalar(sa.select(legacy.c.summary)) == "Existing cached report"
            assert connection.scalar(sa.select(legacy.c.state)) == "FAILED"
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert table.name not in sa.inspect(connection).get_table_names()
            assert connection.scalar(sa.select(assessment.c.id)) == original_id
            assert "ai_review_enabled" not in {
                column["name"] for column in sa.inspect(connection).get_columns(assessment.name)
            }
    finally:
        engine.dispose()


def test_ai_draft_migration_preserves_existing_manual_drafts():
    migration = importlib.import_module(
        "app.db.migrations.versions.20261005_0025_ai_review_draft"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    sa.Table("core_aicodereview", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    draft = sa.Table(
        "core_reviewdraft", metadata, sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("grade", sa.Numeric(8, 2)), sa.Column("comment", sa.Text()),
    )
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            connection.execute(draft.insert(), {
                "id": uuid.uuid4(), "grade": 8, "comment": "Существующий черновик",
            })
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            upgraded = sa.Table(draft.name, sa.MetaData(), autoload_with=connection)
            row = connection.execute(sa.select(upgraded)).mappings().one()
            assert row["ai_review_id"] is None
            assert row["grade"] == 8
            assert row["comment"] == "Существующий черновик"
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert connection.scalar(sa.select(draft.c.comment)) == "Существующий черновик"
    finally:
        engine.dispose()
