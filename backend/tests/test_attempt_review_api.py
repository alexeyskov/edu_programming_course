from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.api.attempts import _record_interactive_response
from app.db.base import utcnow
from app.models.attempts import Attempt, RunRequest, RunResult, Submission
from app.models.courses import CourseMembership
from app.models.enums import SyncOutboxState
from app.models.integration import SyncOutbox
from app.schemas.runs import InteractiveRunRead


async def _dev_login(client: AsyncClient, role: str) -> tuple[dict, dict[str, str]]:
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


async def _publish_assessment(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    course_id: str,
    slug: str,
    title: str,
) -> str:
    item = await client.post(
        "/api/v1/task-bank/items",
        headers=headers,
        json={
            "scope": "COURSE",
            "course": course_id,
            "slug": slug,
            "category": "API lifecycle",
            "tags": ["cpp", "e2e"],
        },
    )
    assert item.status_code == 201, item.text

    main_source = "int main() {\n    return 0;\n}\n"
    helper_source = "constexpr int copied_value = 7;\n"
    version = await client.post(
        f"/api/v1/task-bank/items/{item.json()['id']}/versions",
        headers=headers,
        json={
            "title": title,
            "statement": "Compile the program and submit it for a human review.",
            "language": "CPP",
            "language_standard": "C++20",
            "multi_file": True,
            "starter_files": [
                {"path": "main.cpp", "content": main_source},
                {"path": "value.hpp", "content": helper_source},
            ],
            "build_profile": "cpp-gcc-c++20-multi",
            "public_examples": [],
            "hidden_test_manifest": {},
            "max_score": "10.00",
            "difficulty": "introductory",
            "ai_policy": {},
        },
    )
    assert version.status_code == 201, version.text
    version_id = version.json()["id"]

    validation = await client.post(
        f"/api/v1/task-versions/{version_id}/validate",
        headers=headers,
        json={},
    )
    assert validation.status_code == 200, validation.text
    assert validation.json()["valid"] is True
    published_version = await client.post(
        f"/api/v1/task-versions/{version_id}/publish",
        headers=headers,
        json={},
    )
    assert published_version.status_code == 200, published_version.text

    assessment = await client.post(
        f"/api/v1/courses/{course_id}/assessments",
        headers=headers,
        json={
            "type": "CONTROL",
            "title": title,
            "instructions": "Use only text copied inside this workspace.",
            "attempt_limit": 1,
            "max_score": "10.00",
            "paste_policy": "INTERNAL_ONLY",
            "student_ai_enabled": False,
            "teacher_ai_enabled": True,
            "autosubmit": True,
            "multi_file": True,
        },
    )
    assert assessment.status_code == 201, assessment.text
    assessment_id = assessment.json()["id"]
    attached = await client.post(
        f"/api/v1/assessments/{assessment_id}/items",
        headers=headers,
        json={"task_version": version_id, "position": 0, "points": "10.00"},
    )
    assert attached.status_code == 201, attached.text
    published_assessment = await client.post(
        f"/api/v1/assessments/{assessment_id}/publish",
        headers=headers,
        json={},
    )
    assert published_assessment.status_code == 200, published_assessment.text
    return assessment_id


async def _start_attempt(
    client: AsyncClient,
    *,
    headers: dict[str, str],
    assessment_id: str,
) -> tuple[dict, dict]:
    started = await client.post(
        f"/api/v1/assessments/{assessment_id}/attempts",
        headers=headers,
        json={},
    )
    assert started.status_code == 201, started.text
    attempt = started.json()
    workspace = await client.get(f"/api/v1/attempts/{attempt['id']}/workspace")
    assert workspace.status_code == 200, workspace.text
    return attempt, workspace.json()


async def test_student_interactive_start_is_audited_and_rejects_duplicate_session(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_mock_enabled = True
    live = InteractiveRunRead(
        session_id="d" * 32,
        status="RUNNING",
        terminal=False,
        duration_ms=1,
        stdout="input> ",
        stderr="",
        output_truncated=False,
        diagnostics=[],
    )
    monkeypatch.setattr("app.api.attempts._mock_student_interactive", lambda: live)

    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(
            transport=ASGITransport(app=app, client=("203.0.113.55", 43210)),
            base_url="http://testserver",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/140.0.0.0 Safari/537.36"
                )
            },
        ) as student,
    ):
        teacher_session, teacher_headers = await _dev_login(teacher, "TEACHER")
        assessment_id = await _publish_assessment(
            teacher,
            headers=teacher_headers,
            course_id=teacher_session["memberships"][0]["course_id"],
            slug="interactive-session-audit",
            title="Interactive session audit",
        )
        _student_session, student_headers = await _dev_login(student, "STUDENT")
        attempt, workspace = await _start_attempt(
            student, headers=student_headers, assessment_id=assessment_id
        )

        starts = await asyncio.gather(
            *(
                student.post(
                    f"/api/v1/attempts/{attempt['id']}/interactive-sessions",
                    headers=student_headers,
                    json={"revision": workspace["current_revision"]},
                )
                for _ in range(2)
            )
        )
        first = next(response for response in starts if response.status_code == 201)
        duplicate = next(response for response in starts if response.status_code == 409)
        assert first.json()["terminal"] is False
        assert duplicate.status_code == 409, duplicate.text
        assert duplicate.json()["code"] == "INTERACTIVE_SESSION_ACTIVE"

        async with session_factory() as db:
            stored_attempt = await db.get(Attempt, uuid.UUID(attempt["id"]))
            assert stored_attempt is not None
            assert stored_attempt.client_context == {
                "ip_address": "203.0.113.55",
                "browser": "Google Chrome",
                "browser_version": "140.0.0.0",
                "operating_system": "Linux",
                "device_type": "DESKTOP",
            }
            runs = list(
                (
                    await db.scalars(
                        select(RunRequest).where(
                            RunRequest.attempt_id == uuid.UUID(attempt["id"]),
                            RunRequest.mode == "INTERACTIVE",
                        )
                    )
                ).all()
            )
            assert len(runs) == 1
            assert runs[0].status == "RUNNING"
            assert runs[0].external_job_id == "d" * 32
            assert runs[0].client_context == stored_attempt.client_context
            assert await db.scalar(select(RunResult).where(RunResult.run_id == runs[0].id)) is None
            run_id = runs[0].id

        history = await student.get(f"/api/v1/attempts/{attempt['id']}/history")
        assert history.status_code == 200, history.text
        run_event = next(event for event in history.json() if event["id"] == str(run_id))
        assert run_event["client"]["ip_address"] == "203.0.113.55"
        assert run_event["client"]["browser"] == "Google Chrome"

        terminal = live.model_copy(update={"status": "SUCCESS", "terminal": True, "exit_code": 0})
        async with session_factory() as db:
            await _record_interactive_response(db, run_id=run_id, raw=terminal)
        async with session_factory() as db:
            await _record_interactive_response(db, run_id=run_id, raw=terminal)
        async with session_factory() as db:
            results = list(
                (await db.scalars(select(RunResult).where(RunResult.run_id == run_id))).all()
            )
            assert len(results) == 1
            assert (await db.get(RunRequest, run_id)).status == "COMPLETED"

        # Interactive starts share the ordinary per-user run quota.  The
        # completed request above must remain in the recent audit window and
        # prevent a second start when the configured budget is one.
        settings.runner_rate_limit_runs = 1
        rate_limited = await student.post(
            f"/api/v1/attempts/{attempt['id']}/interactive-sessions",
            headers=student_headers,
            json={"revision": workspace["current_revision"]},
        )
        assert rate_limited.status_code == 429, rate_limited.text
        assert rate_limited.json()["code"] == "RUN_RATE_LIMIT"


async def test_student_attempt_and_teacher_review_lifecycle_via_api(
    app_bundle,
    monkeypatch,
) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.runner_mock_enabled = True
    live_teacher_session = InteractiveRunRead(
        session_id="e" * 32,
        status="RUNNING",
        terminal=False,
        duration_ms=1,
        stdout="input> ",
        stderr="",
        output_truncated=False,
        diagnostics=[],
    )
    monkeypatch.setattr(
        "app.api.reviews._mock_interactive",
        lambda: live_teacher_session,
    )

    async with (
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as teacher_client,
        AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as student_client,
    ):
        teacher_session, teacher_headers = await _dev_login(teacher_client, "TEACHER")
        course_id = teacher_session["memberships"][0]["course_id"]
        assessment_id = await _publish_assessment(
            teacher_client,
            headers=teacher_headers,
            course_id=course_id,
            slug="full-attempt-review-lifecycle",
            title="C++ API lifecycle",
        )

        student_session, student_headers = await _dev_login(student_client, "STUDENT")
        assert student_session["roles"] == ["STUDENT"]
        assert student_session["capabilities"] == []
        attempt, workspace = await _start_attempt(
            student_client,
            headers=student_headers,
            assessment_id=assessment_id,
        )
        attempt_id = attempt["id"]
        listed_active = await student_client.get(f"/api/v1/courses/{course_id}/assessments")
        assert listed_active.status_code == 200, listed_active.text
        active_projection = next(row for row in listed_active.json() if row["id"] == assessment_id)
        assert active_projection["attempt_id"] == attempt_id
        assert active_projection["progress"] == 0
        assert workspace["current_revision"] == 0
        files = {file["path"]: file for file in workspace["files"]}
        main = files["main.cpp"]
        helper = files["value.hpp"]

        receipt = await student_client.post(
            f"/api/v1/attempts/{attempt_id}/clipboard-receipts",
            headers=student_headers,
            json={
                "file_id": helper["id"],
                "text": helper["content"],
                "revision": 0,
            },
        )
        assert receipt.status_code == 201, receipt.text
        pasted = await student_client.patch(
            f"/api/v1/attempts/{attempt_id}/workspace/files/{main['id']}",
            headers={**student_headers, "If-Match": '"0"'},
            json={
                "content": main["content"] + helper["content"],
                "source": "INTERNAL_PASTE",
                "receipt_id": receipt.json()["id"],
                "client_id": "browser-e2e",
                "client_request_id": "internal-paste-1",
            },
        )
        assert pasted.status_code == 200, pasted.text
        assert pasted.json()["revision"] == 1

        student_run = await student_client.post(
            f"/api/v1/attempts/{attempt_id}/runs",
            headers=student_headers,
            json={"revision": 1, "stdin": "", "mode": "RUN"},
        )
        assert student_run.status_code == 201, student_run.text
        student_run_id = student_run.json()["id"]
        assert student_run.json()["status"] == "COMPLETED"
        assert student_run.json()["result"]["exit_reason"] == "MOCK"
        assert "filesystem_profile" not in student_run.json()

        interactive_student = await student_client.post(
            f"/api/v1/attempts/{attempt_id}/interactive-sessions",
            headers=student_headers,
            json={"revision": 1},
        )
        assert interactive_student.status_code == 201, interactive_student.text
        assert interactive_student.json()["terminal"] is True
        assert interactive_student.json()["status"] == "SUCCESS"
        async with session_factory() as db:
            interactive_audit = await db.scalar(
                select(RunRequest).where(
                    RunRequest.attempt_id == uuid.UUID(attempt_id),
                    RunRequest.mode == "INTERACTIVE",
                )
            )
            assert interactive_audit is not None
            assert interactive_audit.status == "COMPLETED"
            assert interactive_audit.external_job_id == interactive_student.json()["session_id"]
            interactive_result = await db.scalar(
                select(RunResult).where(RunResult.run_id == interactive_audit.id)
            )
            assert interactive_result is not None
            assert interactive_result.exit_reason == "SUCCESS"

        stale_interactive = await student_client.post(
            f"/api/v1/attempts/{attempt_id}/interactive-sessions",
            headers=student_headers,
            json={"revision": 0},
        )
        assert stale_interactive.status_code == 409
        assert stale_interactive.json()["code"] == "REVISION_CONFLICT"

        hidden_student_interactive = await teacher_client.post(
            f"/api/v1/attempts/{attempt_id}/interactive-sessions",
            headers=teacher_headers,
            json={"revision": 1},
        )
        assert hidden_student_interactive.status_code == 404

        submitted = await student_client.post(
            f"/api/v1/attempts/{attempt_id}/submit",
            headers=student_headers,
            json={"revision": 1},
        )
        assert submitted.status_code == 201, submitted.text
        submission_id = submitted.json()["submission_id"]

        # A completed attempt is still available through its direct URL for
        # read-only history, but it must not be advertised as the resumable
        # attempt of the assessment.  The assessment page can consequently
        # call the start endpoint and let Moodle prepare a later retry.
        listed_finished = await student_client.get(f"/api/v1/courses/{course_id}/assessments")
        assert listed_finished.status_code == 200, listed_finished.text
        finished_projection = next(
            row for row in listed_finished.json() if row["id"] == assessment_id
        )
        assert finished_projection["attempt_id"] is None
        assert finished_projection["progress"] is None

        interactive_after_submit = await student_client.post(
            f"/api/v1/attempts/{attempt_id}/interactive-sessions",
            headers=student_headers,
            json={"revision": 1},
        )
        assert interactive_after_submit.status_code == 409
        assert interactive_after_submit.json()["code"] == "ATTEMPT_READ_ONLY"

        locked_workspace = await student_client.get(f"/api/v1/attempts/{attempt_id}/workspace")
        assert locked_workspace.status_code == 200, locked_workspace.text
        assert all(file["read_only"] for file in locked_workspace.json()["files"])
        edit_after_submit = await student_client.patch(
            f"/api/v1/attempts/{attempt_id}/workspace/files/{main['id']}",
            headers={**student_headers, "If-Match": '"1"'},
            json={
                "content": "int main() { return 1; }\n",
                "source": "TYPING",
                "client_request_id": "after-submit",
            },
        )
        assert edit_after_submit.status_code == 409
        assert edit_after_submit.json()["code"] == "ATTEMPT_READ_ONLY"

        queue = await teacher_client.get(f"/api/v1/assessments/{assessment_id}/submissions")
        assert queue.status_code == 200, queue.text
        assert [row["id"] for row in queue.json()] == [submission_id]
        assert queue.json()[0]["status"] == "UNGRADED"

        # A private experiment must not bypass the review claim before the
        # first official decision.  Only already-decided work may be opened
        # later as a claim-free teacher sandbox.
        unclaimed_experiment = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/teacher-experiments",
            headers=teacher_headers,
            json={},
        )
        assert unclaimed_experiment.status_code == 409, unclaimed_experiment.text
        assert unclaimed_experiment.json()["code"] == "ACTIVE_REVIEW_CLAIM_REQUIRED"

        claimed = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/claims",
            headers=teacher_headers,
            json={},
        )
        assert claimed.status_code == 201, claimed.text
        assert claimed.json()["mine"] is True

        detail = await teacher_client.get(f"/api/v1/submissions/{submission_id}")
        assert detail.status_code == 200, detail.text
        detail_body = detail.json()
        assert all(file["read_only"] for file in detail_body["files"])
        history_types = [event["type"] for event in detail_body["history"]]
        assert "internal_paste" in history_types
        assert "run" in history_types
        assert "submit" in history_types
        assert student_run_id in [
            event["id"] for event in detail_body["history"] if event["type"] == "run"
        ]

        experiment = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/teacher-experiments",
            headers=teacher_headers,
            json={},
        )
        assert experiment.status_code == 201, experiment.text
        experiment_body = experiment.json()
        experiment_id = experiment_body["id"]
        experiment_main = next(
            file for file in experiment_body["files"] if file["path"] == "main.cpp"
        )
        changed = await teacher_client.patch(
            f"/api/v1/teacher-experiments/{experiment_id}/files/{experiment_main['id']}",
            headers={**teacher_headers, "If-Match": '"0"'},
            json={"content": "int main() { return copied_value; }\n"},
        )
        assert changed.status_code == 200, changed.text
        assert changed.json()["revision"] == 1

        interactive_starts = await asyncio.gather(
            *(
                teacher_client.post(
                    f"/api/v1/teacher-experiments/{experiment_id}/interactive-sessions",
                    headers=teacher_headers,
                    json={"revision": 1},
                )
                for _ in range(2)
            )
        )
        interactive = next(
            response for response in interactive_starts if response.status_code == 201
        )
        duplicate_interactive = next(
            response for response in interactive_starts if response.status_code == 409
        )
        assert interactive.status_code == 201, interactive.text
        assert interactive.json()["terminal"] is False
        assert interactive.json()["status"] == "RUNNING"
        assert len(interactive.json()["session_id"]) == 32
        assert duplicate_interactive.json()["code"] == "INTERACTIVE_SESSION_ACTIVE"

        hidden_interactive = await student_client.post(
            f"/api/v1/teacher-experiments/{experiment_id}/interactive-sessions",
            headers=student_headers,
            json={"revision": 1},
        )
        assert hidden_interactive.status_code == 404

        private_run = await teacher_client.post(
            f"/api/v1/teacher-experiments/{experiment_id}/runs",
            headers=teacher_headers,
            json={"revision": 1, "stdin": "", "mode": "RUN"},
        )
        assert private_run.status_code == 201, private_run.text
        private_run_body = private_run.json()
        private_run_id = private_run_body["id"]
        assert private_run_body["origin"] == "TEACHER_EXPERIMENT"
        assert private_run_body["actual_filesystem_isolated"] is True
        assert private_run_body["actual_network_enabled"] is False

        hidden_private_run = await student_client.get(f"/api/v1/runs/{private_run_id}")
        assert hidden_private_run.status_code == 404
        assert hidden_private_run.json()["code"] == "RUN_NOT_FOUND"
        student_history = await student_client.get(f"/api/v1/attempts/{attempt_id}/history")
        assert student_history.status_code == 200, student_history.text
        student_history_ids = {event["id"] for event in student_history.json()}
        assert student_run_id in student_history_ids
        assert private_run_id not in student_history_ids

        removed_experiment = await teacher_client.delete(
            f"/api/v1/teacher-experiments/{experiment_id}",
            headers=teacher_headers,
        )
        assert removed_experiment.status_code == 204, removed_experiment.text

        draft = await teacher_client.put(
            f"/api/v1/submissions/{submission_id}/review-draft",
            headers=teacher_headers,
            json={
                "grade": "7.50",
                "comment": "Private draft from the teacher.",
                "criterion_scores": {"correctness": "7.50"},
            },
        )
        assert draft.status_code == 200, draft.text
        assert draft.json()["grade"] == "7.50"

        decision = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/review-decisions",
            headers={**teacher_headers, "Idempotency-Key": "human-final-decision-1"},
            json={
                "grade": "8.50",
                "comment": "Final decision made by the teacher.",
                "criterion_scores": {"correctness": "8.50"},
                "evidence_ids": [detail_body["snapshot_id"]],
            },
        )
        assert decision.status_code == 201, decision.text
        assert decision.json()["reviewer_id"] == teacher_session["principal"]["id"]
        assert decision.json()["grade"] == "8.50"
        assert decision.json()["status"] == "APPLIED"

        graded_queue = await teacher_client.get(f"/api/v1/assessments/{assessment_id}/submissions")
        assert graded_queue.status_code == 200, graded_queue.text
        assert graded_queue.json()[0]["status"] == "GRADED"
        assert graded_queue.json()[0]["score"] == "8.50"

        # Running a private copy of an already decided submission is not an
        # official re-check and therefore does not require a new claim.
        reviewed_experiment = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/teacher-experiments",
            headers=teacher_headers,
            json={},
        )
        assert reviewed_experiment.status_code == 201, reviewed_experiment.text
        reviewed_experiment_body = reviewed_experiment.json()
        reviewed_run = await teacher_client.post(
            f"/api/v1/teacher-experiments/{reviewed_experiment_body['id']}/runs",
            headers=teacher_headers,
            json={"revision": 0, "stdin": "", "mode": "RUN"},
        )
        assert reviewed_run.status_code == 201, reviewed_run.text
        assert reviewed_run.json()["origin"] == "TEACHER_EXPERIMENT"

        unchanged_decision = await teacher_client.get(f"/api/v1/submissions/{submission_id}")
        assert unchanged_decision.status_code == 200, unchanged_decision.text
        assert unchanged_decision.json()["latest_decision"]["revision"] == 1
        assert unchanged_decision.json()["latest_decision"]["grade"] == "8.50"

        reclaimed = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/claims",
            headers=teacher_headers,
            json={},
        )
        assert reclaimed.status_code == 201, reclaimed.text
        revised = await teacher_client.post(
            f"/api/v1/submissions/{submission_id}/review-decisions",
            headers={**teacher_headers, "Idempotency-Key": "human-final-decision-2"},
            json={
                "grade": "9.00",
                "comment": "Rechecked after inspecting the completed work.",
                "criterion_scores": {"correctness": "9.00"},
                "evidence_ids": [detail_body["snapshot_id"]],
            },
        )
        assert revised.status_code == 201, revised.text
        assert revised.json()["revision"] == 2
        assert revised.json()["supersedes_id"] == decision.json()["id"]

        checked_detail = await teacher_client.get(f"/api/v1/submissions/{submission_id}")
        assert checked_detail.status_code == 200, checked_detail.text
        checked_body = checked_detail.json()
        assert checked_body["latest_decision"]["revision"] == 2
        assert checked_body["latest_decision"]["grade"] == "9.00"
        assert checked_body["latest_decision"]["comment"].startswith("Rechecked")
        assert checked_body["latest_decision"]["reviewer_name"] == "Dev преподаватель"
        assert [row["revision"] for row in checked_body["decision_history"]] == [2, 1]
        assert [row["status"] for row in checked_body["decision_history"]] == [
            "APPLIED",
            "SUPERSEDED",
        ]


async def test_revoked_lms_membership_blocks_workspace_edit_via_api(app_bundle) -> None:
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
        teacher_session, teacher_headers = await _dev_login(teacher_client, "TEACHER")
        course_id = teacher_session["memberships"][0]["course_id"]
        assessment_id = await _publish_assessment(
            teacher_client,
            headers=teacher_headers,
            course_id=course_id,
            slug="membership-revocation",
            title="Revoked membership",
        )
        student_session, student_headers = await _dev_login(student_client, "STUDENT")
        attempt, workspace = await _start_attempt(
            student_client,
            headers=student_headers,
            assessment_id=assessment_id,
        )
        main = next(file for file in workspace["files"] if file["path"] == "main.cpp")

        async with session_factory() as db:
            membership = await db.scalar(
                select(CourseMembership).where(
                    CourseMembership.course_id == uuid.UUID(course_id),
                    CourseMembership.principal_id == uuid.UUID(student_session["principal"]["id"]),
                    CourseMembership.role == "STUDENT",
                )
            )
            assert membership is not None
            membership.active = False
            await db.commit()

        rejected = await student_client.patch(
            f"/api/v1/attempts/{attempt['id']}/workspace/files/{main['id']}",
            headers={**student_headers, "If-Match": '"0"'},
            json={
                "content": "int main() { return 2; }\n",
                "source": "TYPING",
                "client_request_id": "edit-after-membership-revocation",
            },
        )
        assert rejected.status_code == 403
        assert rejected.json()["code"] == "COURSE_MEMBERSHIP_REQUIRED"


async def test_manual_submission_after_deadline_is_blocked_via_api(app_bundle) -> None:
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
        teacher_session, teacher_headers = await _dev_login(teacher_client, "TEACHER")
        course_id = teacher_session["memberships"][0]["course_id"]
        assessment_id = await _publish_assessment(
            teacher_client,
            headers=teacher_headers,
            course_id=course_id,
            slug="deadline-enforcement",
            title="Deadline enforcement",
        )
        _student_session, student_headers = await _dev_login(student_client, "STUDENT")
        attempt, _workspace = await _start_attempt(
            student_client,
            headers=student_headers,
            assessment_id=assessment_id,
        )

        async with session_factory() as db:
            row = await db.get(Attempt, uuid.UUID(attempt["id"]))
            assert row is not None
            row.deadline_at = utcnow() - timedelta(seconds=1)
            await db.commit()

        rejected = await student_client.post(
            f"/api/v1/attempts/{attempt['id']}/submit",
            headers=student_headers,
            json={"revision": 0},
        )
        assert rejected.status_code == 409
        assert rejected.json()["code"] == "DEADLINE_PASSED"

        async with session_factory() as db:
            submission = await db.scalar(
                select(Submission).where(Submission.attempt_id == uuid.UUID(attempt["id"]))
            )
            assert submission is None


async def test_reopening_pending_submission_resumes_delivery_and_can_retry(app_bundle) -> None:
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
        teacher_session, teacher_headers = await _dev_login(teacher_client, "TEACHER")
        assessment_id = await _publish_assessment(
            teacher_client,
            headers=teacher_headers,
            course_id=teacher_session["memberships"][0]["course_id"],
            slug="pending-final-delivery",
            title="Pending final delivery",
        )
        _student_session, student_headers = await _dev_login(student_client, "STUDENT")
        attempt, _workspace = await _start_attempt(
            student_client,
            headers=student_headers,
            assessment_id=assessment_id,
        )
        submitted = await student_client.post(
            f"/api/v1/attempts/{attempt['id']}/submit",
            headers=student_headers,
            json={"revision": 0},
        )
        assert submitted.status_code == 201, submitted.text

        async with session_factory() as db:
            events = list(
                (
                    await db.scalars(
                        select(SyncOutbox)
                        .where(
                            SyncOutbox.attempt_id == uuid.UUID(attempt["id"]),
                            SyncOutbox.event_type == "attempt.checkpoint",
                        )
                        .order_by(SyncOutbox.created_at.desc())
                    )
                ).all()
            )
            terminal = next(
                row for row in events if row.payload.get("reason") in {"SUBMISSION", "DEADLINE"}
            )
            terminal.state = SyncOutboxState.FAILED.value
            terminal.last_error = "Moodle timeout"
            await db.commit()

        reopened = await student_client.post(
            f"/api/v1/assessments/{assessment_id}/attempts",
            headers=student_headers,
            json={},
        )
        assert reopened.status_code == 201, reopened.text
        assert reopened.json()["id"] == attempt["id"]
        assert reopened.json()["checkpoint_status"] == "ERROR"

        retried = await student_client.post(
            f"/api/v1/attempts/{attempt['id']}/submit/retry",
            headers=student_headers,
            json={},
        )
        assert retried.status_code == 200, retried.text
        assert retried.json()["revision"] == 0

        async with session_factory() as db:
            refreshed = await db.get(SyncOutbox, terminal.id)
            attempts = list(
                (
                    await db.scalars(
                        select(Attempt).where(Attempt.assessment_id == uuid.UUID(assessment_id))
                    )
                ).all()
            )
        assert refreshed is not None
        assert refreshed.state == SyncOutboxState.RETRY.value
        assert len(attempts) == 1
