import importlib
import uuid

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.db.base import utcnow
from app.models.integration import HistoryWarningDismissal


def test_warning_migration_schema_and_persistent_personal_acknowledgements():
    migration = importlib.import_module(
        "app.db.migrations.versions.20260925_0020_history_warning_dismissals"
    )
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    for name in ("core_externalprincipal", "core_assessment"):
        sa.Table(name, metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    try:
        with engine.begin() as connection:
            metadata.create_all(connection)
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            table = HistoryWarningDismissal.__table__
            assessment_id = uuid.uuid4()
            actors = [uuid.uuid4(), uuid.uuid4()]
            for actor_id in actors:
                connection.execute(table.insert(), {
                    "principal_id": actor_id, "assessment_id": assessment_id,
                    "warning_id": "a" * 64, "created_at": utcnow(), "updated_at": utcnow(),
                })
            assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 2
            uniques = sa.inspect(connection).get_unique_constraints(table.name)
            assert any(set(item["column_names"]) == {
                "principal_id", "assessment_id", "warning_id",
            } for item in uniques)
            foreign_keys = sa.inspect(connection).get_foreign_keys(table.name)
            assert all(key["options"]["ondelete"] == "CASCADE" for key in foreign_keys)
        with engine.connect() as connection:
            rows = connection.execute(sa.select(table)).mappings().all()
            assert {row["principal_id"] for row in rows} == set(actors)
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
            assert table.name not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
