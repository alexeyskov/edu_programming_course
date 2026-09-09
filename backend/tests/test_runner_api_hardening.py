from __future__ import annotations

from datetime import timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.attempts import _enforce_run_budget, _store_run_result
from app.db.base import utcnow
from app.integrations.runner import RunnerResult
from app.models.attempts import Attempt, RunRequest, RunResult, Workspace
from app.models.courses import Course
from app.models.enums import RunOrigin, RunStatus
from app.models.identity import ExternalPrincipal, LMSConnection
from app.models.tasks import Assessment
from app.services.common import DomainError


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


async def _dev_login(client: AsyncClient, role: str) -> dict:
    response = await client.post(
        "/api/v1/auth/dev-login",
        headers=await _csrf(client),
        json={"role": role},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _seed_attempt_and_runs(session_factory):
    async with session_factory() as db:
        teacher = await db.scalar(
            select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "dev-teacher")
        )
        student = await db.scalar(
            select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "dev-student")
        )
        course = await db.scalar(select(Course).where(Course.external_id == "dev-cpp"))
        assert teacher is not None and student is not None and course is not None
        assessment = Assessment(
            course_id=course.id,
            title="Runner audit",
            status="PUBLISHED",
            created_by_id=teacher.id,
        )
        db.add(assessment)
        await db.flush()
        attempt = Attempt(
            assessment_id=assessment.id,
            principal_id=student.id,
            state="ACTIVE",
        )
        db.add(attempt)
        await db.flush()
        db.add(Workspace(attempt_id=attempt.id))
        student_run = RunRequest(
            origin=RunOrigin.STUDENT_ATTEMPT.value,
            attempt_id=attempt.id,
            requested_by_id=student.id,
            revision=0,
            mode="RUN",
            build_profile="cpp-gcc-c++20-single",
            filesystem_profile="FILESYSTEM_ONLY",
            network_enabled=False,
            status=RunStatus.COMPLETED.value,
        )
        private_run = RunRequest(
            origin=RunOrigin.TEACHER_EXPERIMENT.value,
            attempt_id=attempt.id,
            requested_by_id=teacher.id,
            revision=0,
            mode="RUN",
            build_profile="cpp-gcc-c++20-single",
            filesystem_profile="FILESYSTEM_ONLY",
            network_enabled=False,
            status=RunStatus.COMPLETED.value,
            external_job_id="teacher-job",
        )
        db.add_all([student_run, private_run])
        await db.flush()
        db.add(
            RunResult(
                run_id=private_run.id,
                exit_code=0,
                exit_reason="SUCCESS",
                metrics={},
                executor_version="bubblewrap:gcc:14",
                filesystem_policy_version="filesystem-only-v1",
                filesystem_isolated=True,
                network_enabled=False,
            )
        )
        await db.commit()
        return attempt.id, student_run.id, private_run.id, student.id


async def test_private_teacher_run_is_not_disclosed_to_student(app_bundle):
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with (
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as teacher_client,
        AsyncClient(
            transport=ASGITransport(app=app), base_url="http://testserver"
        ) as student_client,
    ):
        await _dev_login(teacher_client, "TEACHER")
        await _dev_login(student_client, "STUDENT")
        attempt_id, student_run_id, private_run_id, _ = await _seed_attempt_and_runs(
            session_factory
        )

        hidden = await student_client.get(f"/api/v1/runs/{private_run_id}")
        assert hidden.status_code == 404
        assert hidden.json()["code"] == "RUN_NOT_FOUND"
        official = await student_client.get(f"/api/v1/runs/{student_run_id}")
        assert official.status_code == 200
        assert "actual_network_enabled" not in official.json()

        history = await student_client.get(
            f"/api/v1/attempts/{attempt_id}/history", params={"limit": 100}
        )
        assert history.status_code == 200
        assert [row["id"] for row in history.json()] == [str(student_run_id)]

        teacher_view = await teacher_client.get(f"/api/v1/runs/{private_run_id}")
        assert teacher_view.status_code == 200, teacher_view.text
        assert teacher_view.json()["actual_network_enabled"] is False
        assert teacher_view.json()["actual_filesystem_isolated"] is True
        assert teacher_view.json()["actual_filesystem_policy_version"] == "filesystem-only-v1"


async def test_unrestricted_runner_result_is_stored_with_truthful_policy(db):
    connection = LMSConnection(
        name="Runner test LMS",
        provider="MOCK",
        base_url="https://runner-policy.test",
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="runner-policy-user",
        display_name="Runner user",
    )
    db.add(principal)
    await db.flush()
    run = RunRequest(
        origin=RunOrigin.STUDENT_ATTEMPT.value,
        requested_by_id=principal.id,
        revision=0,
        mode="RUN",
        build_profile="cpp",
        filesystem_profile="FILESYSTEM_ONLY",
        network_enabled=False,
        status=RunStatus.RUNNING.value,
    )
    db.add(run)
    await db.commit()

    unsafe = RunnerResult(
        external_job_id="unsafe",
        status=RunStatus.COMPLETED.value,
        exit_code=0,
        exit_reason="SUCCESS",
        stdout="",
        stderr="",
        diagnostics=[],
        metrics={},
        executor_version="runner",
        filesystem_policy_version="unsafe",
        filesystem_isolated=False,
        network_enabled=True,
    )
    stored = await _store_run_result(db, run_id=run.id, result=unsafe)
    result = await db.scalar(select(RunResult).where(RunResult.run_id == run.id))

    assert stored.status == RunStatus.COMPLETED.value
    assert result is not None
    assert result.exit_reason == "SUCCESS"
    assert result.network_enabled is True
    assert result.filesystem_isolated is False


async def test_stale_recovery_and_transactional_user_limits(db, app_bundle):
    _, _, settings = app_bundle
    connection = LMSConnection(
        name="Budget LMS",
        provider="MOCK",
        base_url="https://runner-budget.test",
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="budget-user",
        display_name="Budget user",
    )
    db.add(principal)
    await db.flush()
    old = utcnow() - timedelta(minutes=10)
    stale = RunRequest(
        origin=RunOrigin.STUDENT_ATTEMPT.value,
        requested_by_id=principal.id,
        revision=0,
        mode="RUN",
        build_profile="cpp",
        filesystem_profile="FILESYSTEM_ONLY",
        network_enabled=False,
        status=RunStatus.RUNNING.value,
        created_at=old,
        updated_at=old,
    )
    db.add(stale)
    await db.commit()

    recovered = await _enforce_run_budget(
        db,
        principal_id=principal.id,
        settings=settings,
    )
    await db.commit()
    stale_result = await db.scalar(select(RunResult).where(RunResult.run_id == stale.id))
    assert recovered == 1
    assert stale.status == RunStatus.FAILED.value
    assert stale_result is not None
    assert stale_result.exit_reason == "STALE_RUN_RECOVERED"

    active = RunRequest(
        origin=RunOrigin.STUDENT_ATTEMPT.value,
        requested_by_id=principal.id,
        revision=1,
        mode="RUN",
        build_profile="cpp",
        filesystem_profile="FILESYSTEM_ONLY",
        network_enabled=False,
        status=RunStatus.RUNNING.value,
    )
    db.add(active)
    await db.commit()
    settings.runner_max_concurrent_runs_per_user = 1
    with pytest.raises(DomainError) as concurrency:
        await _enforce_run_budget(db, principal_id=principal.id, settings=settings)
    assert concurrency.value.code == "RUN_CONCURRENCY_LIMIT"

    active.status = RunStatus.FAILED.value
    await db.commit()
    settings.runner_max_concurrent_runs_per_user = 2
    settings.runner_rate_limit_runs = 1
    with pytest.raises(DomainError) as rate:
        await _enforce_run_budget(db, principal_id=principal.id, settings=settings)
    assert rate.value.code == "RUN_RATE_LIMIT"
