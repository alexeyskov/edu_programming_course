from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import Request
from sqlalchemy import select

from app.api import attempts, reviews
from app.schemas.runs import InteractiveRunInputRequest, InteractiveRunRead


@pytest.mark.parametrize("role", ["student", "teacher"])
@pytest.mark.parametrize("action", ["state", "input", "eof", "stop"])
async def test_interactive_followup_releases_database_before_runner(
    db,
    app_bundle,
    monkeypatch,
    role,
    action,
):
    app, _, _ = app_bundle
    owner_id, entity_id, run_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    auth = SimpleNamespace(principal_id=owner_id, has_capability=lambda _: False)
    entity = SimpleNamespace(id=entity_id, state="ACTIVE", deadline_at=None)
    run = SimpleNamespace(id=run_id, status="RUNNING")
    request = Request({"type": "http", "app": app, "headers": []})
    response = InteractiveRunRead(
        session_id="a" * 32,
        status="RUNNING",
        terminal=False,
        duration_ms=10,
    )

    async def context(*args, **kwargs):
        # Real SQL transaction/connection, as opened by the authorization and
        # ownership queries. No live runner or student code is executed.
        await db.execute(select(1))
        return (entity, None, None, []) if role == "student" else (entity, None, [])

    async def lookup(*args, **kwargs):
        return run

    async def external_call(*args, **kwargs):
        assert not db.in_transaction(), "Runner wait retained a database pool connection"
        return response.model_dump(mode="json")

    recorded = []

    async def record(*args, **kwargs):
        recorded.append(kwargs["run_id"])

    monkeypatch.setattr(attempts.RunnerAdapter, f"interactive_{action}", external_call)
    if role == "student":
        monkeypatch.setattr(attempts, "_student_interactive_context", context)
        monkeypatch.setattr(db, "scalar", lookup)
        monkeypatch.setattr(attempts, "_record_interactive_response", record)
        actual = await attempts._student_interactive_command(
            action=action,
            attempt_id=entity_id,
            session_id="a" * 32,
            request=request,
            auth=auth,
            db=db,
            text="sample input",
        )
    else:
        monkeypatch.setattr(reviews, "_interactive_experiment", context)
        monkeypatch.setattr(reviews, "_teacher_interactive_run", lookup)
        monkeypatch.setattr(reviews, "_record_interactive_response", record)
        handler = {
            "state": reviews.interactive_experiment_state,
            "input": reviews.interactive_experiment_input,
            "eof": reviews.interactive_experiment_eof,
            "stop": reviews.stop_interactive_experiment,
        }[action]
        kwargs = (
            {"payload": InteractiveRunInputRequest(text="sample input")}
            if action == "input"
            else {}
        )
        actual = await handler(
            experiment_id=entity_id,
            session_id="a" * 32,
            request=request,
            auth=auth,
            db=db,
            **kwargs,
        )
    assert actual == response
    assert recorded == [run_id]
