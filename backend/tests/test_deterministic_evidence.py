from __future__ import annotations

import uuid
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import utcnow
from app.integrations.runner import RunnerResult
from app.models.attempts import Attempt, RunRequest, RunResult, Snapshot, Submission, Workspace
from app.models.courses import Course
from app.models.enums import RunOrigin, RunStatus
from app.models.evidence import EvidenceReport
from app.models.identity import ExternalPrincipal
from app.models.review import ReviewClaim
from app.models.tasks import Assessment, TaskBankItem, TaskVersion
from app.schemas.tasks import parse_hidden_test_manifest
from app.services.common import canonical_hash, sha256_text


async def _login(client: AsyncClient, role: str) -> tuple[dict, dict[str, str]]:
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    token = csrf.json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/dev-login",
        headers={"X-CSRFToken": token},
        json={"role": role},
    )
    assert response.status_code == 201, response.text
    return response.json(), {"X-CSRFToken": token}


async def _seed_submission(
    session_factory,
    *,
    manifest: dict,
    decision_support_enabled: bool = True,
    review_required: bool = True,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as db:
        teacher = await db.scalar(
            select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "dev-teacher")
        )
        student = await db.scalar(
            select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "dev-student")
        )
        course = await db.scalar(select(Course).where(Course.external_id == "dev-cpp"))
        assert teacher is not None and student is not None and course is not None

        item = TaskBankItem(
            course_id=course.id,
            slug=f"evidence-{uuid.uuid4().hex[:12]}",
            created_by_id=teacher.id,
        )
        db.add(item)
        await db.flush()
        version = TaskVersion(
            item_id=item.id,
            number=1,
            title="Deterministic evidence",
            statement="Print the requested value.",
            language="CPP",
            language_standard="C++20",
            multi_file=False,
            starter_files=[{"path": "main.cpp", "content": ""}],
            build_profile="cpp-gcc-c++20-single",
            public_examples=[],
            hidden_test_manifest=manifest,
            max_score=10,
            content_hash=sha256_text("immutable-task-content"),
            status="PUBLISHED",
            authored_by_id=teacher.id,
            published_at=utcnow(),
        )
        db.add(version)
        await db.flush()
        assessment = Assessment(
            course_id=course.id,
            title="Evidence assessment",
            status="PUBLISHED",
            review_required=review_required,
            decision_support_enabled=decision_support_enabled,
            created_by_id=teacher.id,
        )
        db.add(assessment)
        await db.flush()
        attempt = Attempt(
            assessment_id=assessment.id,
            assigned_task_version_id=version.id,
            principal_id=student.id,
            state="SUBMITTED",
            current_revision=3,
            submitted_at=utcnow(),
        )
        db.add(attempt)
        await db.flush()
        workspace = Workspace(
            attempt_id=attempt.id,
            current_revision=3,
            event_chain_head=sha256_text("event-chain"),
        )
        db.add(workspace)
        await db.flush()
        source = '#include <iostream>\nint main(){std::cout << "OK\\n";}\n'
        files = [
            {
                "id": str(uuid.uuid4()),
                "path": "main.cpp",
                "language": "CPP",
                "content": source,
                "content_hash": sha256_text(source),
            }
        ]
        snapshot = Snapshot(
            workspace_id=workspace.id,
            revision=3,
            event_chain_head=workspace.event_chain_head,
            manifest_hash=canonical_hash(files),
            files=files,
            reason="SUBMISSION",
        )
        db.add(snapshot)
        await db.flush()
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            source="STUDENT_FINISH",
        )
        db.add(submission)
        await db.flush()
        db.add(
            ReviewClaim(
                submission_id=submission.id,
                owner_id=teacher.id,
                lease_expires_at=utcnow() + timedelta(minutes=5),
                heartbeat_at=utcnow(),
            )
        )
        await db.commit()
        return submission.id, assessment.id, teacher.id, student.id


def _manifest() -> dict:
    return {
        "schema_version": 1,
        "cases": [
            {
                "name": "exact",
                "stdin": "exact\n",
                "expected_stdout": "OK\n",
                "comparison": "EXACT",
            },
            {
                "name": "trailing whitespace",
                "stdin": "trim\n",
                "expected_stdout": "value\n",
                "comparison": "TRIM_TRAILING_WHITESPACE",
            },
        ],
    }


async def test_hidden_manifest_v1_contract_is_strict_and_bounded() -> None:
    parsed = parse_hidden_test_manifest(_manifest())
    assert parsed is not None
    assert len(parsed.cases) == 2
    assert parse_hidden_test_manifest({}) is None

    invalid_values = [
        {"schema_version": True, "cases": [_manifest()["cases"][0]]},
        {"schema_version": 2, "cases": [_manifest()["cases"][0]]},
        {"schema_version": 1, "cases": []},
        {
            "schema_version": 1,
            "cases": [dict(_manifest()["cases"][0], shell="./solution")],
        },
        {
            "schema_version": 1,
            "cases": [dict(_manifest()["cases"][0], name=f"case-{index}") for index in range(21)],
        },
        {
            "schema_version": 1,
            "cases": [dict(_manifest()["cases"][0], stdin="я" * 131_073)],
        },
    ]
    for value in invalid_values:
        try:
            parse_hidden_test_manifest(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"manifest should have been rejected: {value!r}")


async def test_invalid_hidden_manifest_blocks_task_publication(app_bundle) -> None:
    app, _session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as teacher:
        session, headers = await _login(teacher, "TEACHER")
        course_id = session["memberships"][0]["course_id"]
        item = await teacher.post(
            "/api/v1/task-bank/items",
            headers=headers,
            json={
                "scope": "COURSE",
                "course": course_id,
                "slug": "invalid-hidden-test-contract",
                "category": "tests",
                "tags": [],
            },
        )
        assert item.status_code == 201, item.text
        version = await teacher.post(
            f"/api/v1/task-bank/items/{item.json()['id']}/versions",
            headers=headers,
            json={
                "title": "Invalid hidden tests",
                "statement": "This draft must not publish.",
                "language": "CPP",
                "language_standard": "C++20",
                "multi_file": False,
                "starter_files": [{"path": "main.cpp", "content": ""}],
                "build_profile": "cpp-gcc-c++20-single",
                "public_examples": [],
                "hidden_test_manifest": {"schema_version": 1, "cases": []},
                "max_score": "10.00",
                "difficulty": "",
                "ai_policy": {},
            },
        )
        assert version.status_code == 201, version.text
        validation = await teacher.post(
            f"/api/v1/task-versions/{version.json()['id']}/validate",
            headers=headers,
            json={},
        )
        publication = await teacher.post(
            f"/api/v1/task-versions/{version.json()['id']}/publish",
            headers=headers,
            json={},
        )
        assert validation.status_code == 200
        assert validation.json()["valid"] is False
        assert validation.json()["errors"][0]["code"] == "INVALID_HIDDEN_TEST_MANIFEST"
        assert publication.status_code == 409
        assert publication.json()["code"] == "TASK_VERSION_INVALID"


async def test_evidence_run_is_immutable_isolated_auditable_and_review_evidence(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_url = "http://runner.test"
    dispatched: list[dict] = []

    async def fake_dispatch(_adapter, **kwargs):
        dispatched.append(kwargs)
        stdout = "OK\n" if kwargs["stdin"] == "exact\n" else "value   \n\n"
        return RunnerResult(
            external_job_id=f"job-{len(dispatched)}",
            status=RunStatus.COMPLETED.value,
            exit_code=0,
            exit_reason="SUCCESS",
            stdout=stdout,
            stderr="",
            diagnostics=[],
            metrics={},
            executor_version="test-runner:gcc:14",
            filesystem_policy_version="filesystem-only-v1",
            filesystem_isolated=True,
            network_enabled=False,
        )

    monkeypatch.setattr("app.api.evidence.RunnerAdapter.dispatch", fake_dispatch)

    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as student,
    ):
        _teacher_session, teacher_headers = await _login(teacher, "TEACHER")
        await _login(student, "STUDENT")
        submission_id, assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )

        created = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers=teacher_headers,
            json={},
        )
        assert created.status_code == 201, created.text
        report = created.json()
        assert report["status"] == "COMPLETED"
        assert (report["passed_cases"], report["total_cases"]) == (2, 2)
        assert [row["status"] for row in report["outcomes"]] == ["PASSED", "PASSED"]
        assert all(row["filesystem_isolated"] is True for row in report["outcomes"])
        assert all(row["network_enabled"] is False for row in report["outcomes"])
        assert all("expected_stdout" not in row for row in report["outcomes"])
        run_ids = [row["run_id"] for row in report["outcomes"]]

        assert len(dispatched) == 2
        assert all(call["mode"] == "TEST" for call in dispatched)
        assert all(call["files"][0]["path"] == "main.cpp" for call in dispatched)
        assert all("std::cout" in call["files"][0]["content"] for call in dispatched)

        replay = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers={**teacher_headers, "Idempotency-Key": "same-evidence-request"},
            json={},
        )
        first_keyed = replay
        # The first request did not have a key, so this creates one additional report.
        assert first_keyed.status_code == 201, first_keyed.text
        keyed_report_id = first_keyed.json()["id"]
        dispatch_count = len(dispatched)
        keyed_replay = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers={**teacher_headers, "Idempotency-Key": "same-evidence-request"},
            json={},
        )
        assert keyed_replay.status_code == 201
        assert keyed_replay.json()["id"] == keyed_report_id
        assert len(dispatched) == dispatch_count

        listed = await teacher.get(f"/api/v1/submissions/{submission_id}/evidence-runs")
        detail = await teacher.get(f"/api/v1/evidence-runs/{report['id']}")
        submission = await teacher.get(f"/api/v1/submissions/{submission_id}")
        queue = await teacher.get(f"/api/v1/assessments/{assessment_id}/submissions")
        official_run = await teacher.get(f"/api/v1/runs/{run_ids[0]}")
        assert listed.status_code == detail.status_code == submission.status_code == 200
        assert queue.status_code == official_run.status_code == 200
        assert {row["id"] for row in listed.json()} == {report["id"], keyed_report_id}
        assert (submission.json()["tests_passed"], submission.json()["tests_total"]) == (2, 2)
        assert (queue.json()[0]["tests_passed"], queue.json()[0]["tests_total"]) == (2, 2)
        assert official_run.json()["origin"] == "IMMUTABLE_SUBMISSION"
        assert official_run.json()["evidence_report_id"] == report["id"]

        hidden_list = await student.get(f"/api/v1/submissions/{submission_id}/evidence-runs")
        hidden_detail = await student.get(f"/api/v1/evidence-runs/{report['id']}")
        hidden_run = await student.get(f"/api/v1/runs/{run_ids[0]}")
        assert hidden_list.status_code == hidden_detail.status_code == 403
        assert hidden_run.status_code == 404

        async with session_factory() as db:
            official = await db.get(RunRequest, uuid.UUID(run_ids[0]))
            assert official is not None
            private_run = RunRequest(
                origin=RunOrigin.TEACHER_EXPERIMENT.value,
                attempt_id=official.attempt_id,
                submission_id=submission_id,
                requested_by_id=official.requested_by_id,
                revision=official.revision,
                mode="TEST",
                build_profile=official.build_profile,
                filesystem_profile="FILESYSTEM_ONLY",
                network_enabled=False,
                status=RunStatus.COMPLETED.value,
            )
            db.add(private_run)
            await db.commit()
            private_run_id = private_run.id

        rejected_private = await teacher.post(
            f"/api/v1/submissions/{submission_id}/review-decisions",
            headers=teacher_headers,
            json={
                "grade": "7.00",
                "comment": "A private experiment is not official evidence.",
                "criterion_scores": {},
                "evidence_ids": [str(private_run_id)],
            },
        )
        assert rejected_private.status_code == 422
        assert rejected_private.json()["code"] == "INVALID_REVIEW_EVIDENCE"

        decision = await teacher.post(
            f"/api/v1/submissions/{submission_id}/review-decisions",
            headers=teacher_headers,
            json={
                "grade": "7.00",
                "comment": "Human decision informed by deterministic evidence.",
                "criterion_scores": {},
                "evidence_ids": [run_ids[0]],
            },
        )
        assert decision.status_code == 201, decision.text
        assert decision.json()["evidence_ids"] == [run_ids[0]]

        settings.runner_url = ""
        replay_after_policy_change = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers={**teacher_headers, "Idempotency-Key": "same-evidence-request"},
            json={},
        )
        assert replay_after_policy_change.status_code == 201
        assert replay_after_policy_change.json()["id"] == keyed_report_id

    async with session_factory() as db:
        runs = list(
            (
                await db.scalars(
                    select(RunRequest)
                    .where(RunRequest.evidence_report_id == uuid.UUID(report["id"]))
                    .order_by(RunRequest.evidence_case_index)
                )
            ).all()
        )
        assert len(runs) == 2
        assert all(run.origin == RunOrigin.IMMUTABLE_SUBMISSION.value for run in runs)
        assert all(run.submission_id == submission_id for run in runs)
        assert all(run.network_enabled is True for run in runs)
        assert all(run.filesystem_profile == "UNRESTRICTED_CONTAINER" for run in runs)
        assert (
            await db.scalar(
                select(func.count())
                .select_from(RunResult)
                .where(
                    RunResult.run_id.in_([run.id for run in runs]),
                    RunResult.filesystem_isolated.is_(True),
                    RunResult.network_enabled.is_(False),
                )
            )
            == 2
        )


async def test_evidence_run_policy_guards_and_no_manifest(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_url = "http://runner.test"
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as student,
    ):
        _session, headers = await _login(teacher, "TEACHER")
        await _login(student, "STUDENT")
        no_manifest_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest={},
        )
        missing = await teacher.post(
            f"/api/v1/submissions/{no_manifest_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert missing.status_code == 409
        assert missing.json()["code"] == "HIDDEN_TESTS_NOT_CONFIGURED"

        disabled_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
            decision_support_enabled=False,
        )
        disabled = await teacher.post(
            f"/api/v1/submissions/{disabled_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert disabled.status_code == 403
        assert disabled.json()["code"] == "DECISION_SUPPORT_DISABLED"

        no_review_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
            review_required=False,
        )
        no_review = await teacher.post(
            f"/api/v1/submissions/{no_review_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert no_review.status_code == 409
        assert no_review.json()["code"] == "REVIEW_NOT_REQUIRED"

        unclaimed_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        async with session_factory() as db:
            claim = await db.scalar(
                select(ReviewClaim).where(ReviewClaim.submission_id == unclaimed_id)
            )
            assert claim is not None
            await db.delete(claim)
            await db.commit()
        unclaimed = await teacher.post(
            f"/api/v1/submissions/{unclaimed_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert unclaimed.status_code == 409
        assert unclaimed.json()["code"] == "ACTIVE_REVIEW_CLAIM_REQUIRED"

        runner_disabled_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        settings.runner_url = ""
        runner_disabled = await teacher.post(
            f"/api/v1/submissions/{runner_disabled_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert runner_disabled.status_code == 503
        assert runner_disabled.json()["code"] == "RUNNER_DISABLED"

        mock_runner_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        settings.runner_mock_enabled = True
        mock_runner = await teacher.post(
            f"/api/v1/submissions/{mock_runner_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert mock_runner.status_code == 503
        assert mock_runner.json()["code"] == "EVIDENCE_REQUIRES_REAL_RUNNER"

    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(EvidenceReport)) == 0


async def test_unrestricted_runner_response_can_complete_evidence_report(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_url = "http://runner.test"

    async def unsafe_dispatch(_adapter, **_kwargs):
        return RunnerResult(
            external_job_id="unsafe",
            status=RunStatus.COMPLETED.value,
            exit_code=0,
            exit_reason="SUCCESS",
            stdout="OK\n",
            stderr="",
            diagnostics=[],
            metrics={},
            executor_version="unsafe",
            filesystem_policy_version="unrestricted-container-v1",
            filesystem_isolated=False,
            network_enabled=True,
        )

    monkeypatch.setattr("app.api.evidence.RunnerAdapter.dispatch", unsafe_dispatch)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as student,
    ):
        _session, headers = await _login(teacher, "TEACHER")
        await _login(student, "STUDENT")
        submission_id, assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        response = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert response.status_code == 201, response.text
        report = response.json()
        assert report["status"] == "COMPLETED"
        assert report["failure_code"] == ""
        assert report["passed_cases"] == 1
        assert report["total_cases"] == 2
        assert len(report["outcomes"]) == 2
        assert report["outcomes"][0]["status"] == "PASSED"
        assert report["outcomes"][0]["filesystem_isolated"] is False
        assert report["outcomes"][0]["network_enabled"] is True
        assert report["outcomes"][1]["status"] == "FAILED"

        queue = await teacher.get(f"/api/v1/assessments/{assessment_id}/submissions")
        assert queue.status_code == 200
        assert (queue.json()[0]["tests_passed"], queue.json()[0]["tests_total"]) == (1, 2)

    async with session_factory() as db:
        run = await db.scalar(
            select(RunRequest).where(RunRequest.evidence_report_id == uuid.UUID(report["id"]))
        )
        assert run is not None
        result = await db.scalar(select(RunResult).where(RunResult.run_id == run.id))
        assert result is not None
        assert result.exit_reason == "SUCCESS"
        assert result.filesystem_isolated is False
        assert result.network_enabled is True


async def test_running_report_conflict_stale_recovery_and_wall_timeout(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_url = "http://runner.test"
    settings.evidence_case_timeout_seconds = 0.01

    async def slow_dispatch(_adapter, **_kwargs):
        import asyncio

        await asyncio.sleep(0.05)
        raise AssertionError("outer evidence timeout should cancel the runner dispatch")

    monkeypatch.setattr("app.api.evidence.RunnerAdapter.dispatch", slow_dispatch)
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as student,
    ):
        _session, headers = await _login(teacher, "TEACHER")
        await _login(student, "STUDENT")
        submission_id, _assessment_id, teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        async with session_factory() as db:
            submission = await db.get(Submission, submission_id)
            assert submission is not None
            attempt = await db.get(Attempt, submission.attempt_id)
            snapshot = await db.get(Snapshot, submission.snapshot_id)
            assert attempt is not None and snapshot is not None
            version = await db.get(TaskVersion, attempt.assigned_task_version_id)
            assert version is not None
            active = EvidenceReport(
                submission_id=submission.id,
                snapshot_id=snapshot.id,
                task_version_id=version.id,
                requested_by_id=teacher_id,
                hidden_test_manifest_hash=canonical_hash(_manifest()),
                task_content_hash=version.content_hash,
                snapshot_manifest_hash=snapshot.manifest_hash,
                status="RUNNING",
                total_cases=2,
            )
            db.add(active)
            await db.commit()
            active_id = active.id

        conflict = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "EVIDENCE_RUN_IN_PROGRESS"
        assert conflict.json()["details"]["report_id"] == str(active_id)

        async with session_factory() as db:
            active = await db.get(EvidenceReport, active_id)
            assert active is not None
            old = utcnow() - timedelta(seconds=settings.evidence_running_stale_seconds + 1)
            active.created_at = old
            active.updated_at = old
            stale_run = RunRequest(
                origin=RunOrigin.IMMUTABLE_SUBMISSION.value,
                attempt_id=attempt.id,
                submission_id=submission_id,
                evidence_report_id=active.id,
                evidence_case_index=0,
                requested_by_id=teacher_id,
                revision=snapshot.revision,
                mode="TEST",
                build_profile=version.build_profile,
                filesystem_profile="FILESYSTEM_ONLY",
                network_enabled=False,
                status=RunStatus.RUNNING.value,
            )
            db.add(stale_run)
            await db.commit()
            stale_run_id = stale_run.id

        timed_out = await teacher.post(
            f"/api/v1/submissions/{submission_id}/evidence-runs",
            headers={**headers, "Idempotency-Key": "after-stale-recovery"},
            json={},
        )
        assert timed_out.status_code == 201, timed_out.text
        assert timed_out.json()["status"] == "FAILED"
        assert timed_out.json()["failure_code"] == "TIMEOUT"
        assert len(timed_out.json()["outcomes"]) == 1

    async with session_factory() as db:
        recovered = await db.get(EvidenceReport, active_id)
        recovered_run = await db.get(RunRequest, stale_run_id)
        recovered_result = await db.scalar(
            select(RunResult).where(RunResult.run_id == stale_run_id)
        )
        assert recovered is not None and recovered.status == "FAILED"
        assert recovered.failure_code == "STALE_EVIDENCE_RECOVERED"
        assert recovered_run is not None and recovered_run.status == RunStatus.FAILED.value
        assert recovered_result is not None
        assert recovered_result.exit_reason == "STALE_EVIDENCE_RECOVERED"


async def test_evidence_has_separate_teacher_concurrency_and_rate_budgets(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_url = "http://runner.test"
    settings.evidence_max_concurrent_reports_per_teacher = 1
    settings.evidence_rate_limit_reports = 1
    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as student,
    ):
        _session, headers = await _login(teacher, "TEACHER")
        await _login(student, "STUDENT")
        first_id, _assessment_id, teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        second_id, _assessment_id, _teacher_id, _student_id = await _seed_submission(
            session_factory,
            manifest=_manifest(),
        )
        async with session_factory() as db:
            first = await db.get(Submission, first_id)
            assert first is not None
            attempt = await db.get(Attempt, first.attempt_id)
            snapshot = await db.get(Snapshot, first.snapshot_id)
            assert attempt is not None and snapshot is not None
            version = await db.get(TaskVersion, attempt.assigned_task_version_id)
            assert version is not None
            active = EvidenceReport(
                submission_id=first.id,
                snapshot_id=snapshot.id,
                task_version_id=version.id,
                requested_by_id=teacher_id,
                hidden_test_manifest_hash=canonical_hash(_manifest()),
                task_content_hash=version.content_hash,
                snapshot_manifest_hash=snapshot.manifest_hash,
                status="RUNNING",
                total_cases=2,
            )
            db.add(active)
            await db.commit()
            active_id = active.id

        concurrency = await teacher.post(
            f"/api/v1/submissions/{second_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert concurrency.status_code == 429
        assert concurrency.json()["code"] == "EVIDENCE_CONCURRENCY_LIMIT"

        async with session_factory() as db:
            active = await db.get(EvidenceReport, active_id)
            assert active is not None
            active.status = "COMPLETED"
            active.completed_at = utcnow()
            await db.commit()

        rate = await teacher.post(
            f"/api/v1/submissions/{second_id}/evidence-runs",
            headers=headers,
            json={},
        )
        assert rate.status_code == 429
        assert rate.json()["code"] == "EVIDENCE_RATE_LIMIT"

    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(RunRequest)) == 0
