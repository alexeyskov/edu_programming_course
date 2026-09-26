from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from sqlalchemy import event

from app.models.courses import Course
from app.models.tasks import Assessment
from app.services.moodle_history import materialize_historical_submissions
from tests.test_history_inventory import seed_roster
from tests.test_moodle_history import _finished_item, _seed_history_target

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "history_diagnostic", ROOT / "scripts/diagnose_moodle_history.py",
)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


async def test_history_diagnostic_identifies_missing_group_scope_without_changing_data(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        item = {**_finished_item(), "user_id": "77"}
        await materialize_historical_submissions(
            db, course=await db.get(Course, ids["course_id"]),
            assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42", items=[item],
        )
    async with sessions() as db:
        engine = db.bind.sync_engine
        statements = []

        def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", capture)
        try:
            report = await diagnostic.read_history_report(db, ids["assessment_id"], "42")
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert report["diagnosis"] == ["NO_SUBMISSIONS_IN_TEACHER_GROUP_SCOPE"]
        assert report["answers"]["stored"] == report["answers"]["latest_with_system_access"] == 1
        assert report["answers"]["latest_in_teacher_groups"] == 0
        assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
        assert not any("core_snapshot" in sql or "core_workspacefile" in sql for sql in statements)
        assert not db.dirty and not db.new and not db.deleted
        output = json.dumps(report, default=str)
        assert "int main" not in output and "Student" not in output and "cookies" not in output


async def test_history_diagnostic_reports_ready_answers_for_an_assigned_teacher(app_bundle):
    _, sessions, settings = app_bundle
    ids = await _seed_history_target(sessions, settings=settings)
    async with sessions() as db, db.begin():
        await seed_roster(db, ids)
        await materialize_historical_submissions(
            db, course=await db.get(Course, ids["course_id"]),
            assessment=await db.get(Assessment, ids["assessment_id"]),
            actor_external_subject="42", items=[{**_finished_item(), "user_id": "77"}],
        )
    async with sessions() as db:
        report = await diagnostic.read_history_report(db, ids["assessment_id"], "42")
        assert report["diagnosis"] == ["REVIEWABLE_ANSWERS_PRESENT"]
        assert report["answers"]["latest_in_teacher_groups"] == 1
        assert report["teacher"]["explicit_groups"] == 1
        assert report["roster"]["active_students_with_groups"] == 2
