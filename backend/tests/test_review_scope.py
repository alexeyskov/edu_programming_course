from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select

from app.db.base import utcnow
from app.models.attempts import (
    Attempt,
    RunRequest,
    RunResult,
    Snapshot,
    Submission,
    Workspace,
)
from app.models.courses import Course, CourseGroup, CourseMembership, CourseMembershipGroup
from app.models.evidence import EvidenceReport
from app.models.identity import ExternalPrincipal
from app.models.tasks import Assessment, TaskBankItem, TaskVersion
from app.services.common import canonical_hash, sha256_text
from app.services.policy import legacy_subgroup_is_assigned_to_teacher


async def _login(
    client: AsyncClient,
    role: str,
    *,
    admin_token: str | None = None,
) -> tuple[dict, dict[str, str]]:
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    token = csrf.json()["csrf_token"]
    payload = {"role": role}
    if admin_token is not None:
        payload["admin_token"] = admin_token
    response = await client.post(
        "/api/v1/auth/dev-login",
        headers={"X-CSRFToken": token},
        json=payload,
    )
    assert response.status_code == 201, response.text
    return response.json(), {"X-CSRFToken": token}


async def _submission(
    db,
    *,
    assessment: Assessment,
    version: TaskVersion,
    student: ExternalPrincipal,
    source: str,
) -> Submission:
    attempt = Attempt(
        assessment_id=assessment.id,
        assigned_task_version_id=version.id,
        principal_id=student.id,
        state="SUBMITTED",
        current_revision=1,
        submitted_at=utcnow(),
    )
    db.add(attempt)
    await db.flush()
    workspace = Workspace(
        attempt_id=attempt.id,
        current_revision=1,
        current_hash=sha256_text(source),
        event_chain_head=sha256_text(f"events:{student.id}"),
    )
    db.add(workspace)
    await db.flush()
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
        revision=1,
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
        revision=1,
        source="MANUAL",
        lms_export_state="NOT_REQUIRED",
    )
    db.add(submission)
    await db.flush()
    return submission


async def _seed_scoped_submissions(
    session_factory,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    async with session_factory() as db:
        course = await db.scalar(select(Course).where(Course.external_id == "dev-cpp"))
        teacher = await db.scalar(
            select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "dev-teacher")
        )
        own_student = await db.scalar(
            select(ExternalPrincipal).where(ExternalPrincipal.external_subject == "dev-student")
        )
        assert course is not None and teacher is not None and own_student is not None

        other_student = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject="review-scope-other-student",
            display_name="Other student",
            active=True,
        )
        db.add(other_student)
        await db.flush()
        other_membership = CourseMembership(
            course_id=course.id,
            principal_id=other_student.id,
            role="STUDENT",
            active=True,
        )
        other_group = CourseGroup(
            course_id=course.id,
            external_id="review-scope-other-group",
            name="9.9",
            active=True,
        )
        db.add_all([other_membership, other_group])
        await db.flush()
        db.add(
            CourseMembershipGroup(
                coursemembership_id=other_membership.id,
                coursegroup_id=other_group.id,
            )
        )

        item = TaskBankItem(
            course_id=course.id,
            slug=f"review-scope-{uuid.uuid4().hex[:10]}",
            created_by_id=teacher.id,
        )
        db.add(item)
        await db.flush()
        version = TaskVersion(
            item_id=item.id,
            number=1,
            title="Review scope",
            statement="Return zero.",
            starter_files=[{"path": "main.cpp", "content": ""}],
            build_profile="cpp-gcc-c++20-single",
            max_score=10,
            content_hash=sha256_text("review-scope-task"),
            status="PUBLISHED",
            authored_by_id=teacher.id,
            published_at=utcnow(),
        )
        db.add(version)
        await db.flush()
        assessment = Assessment(
            course_id=course.id,
            title="Scoped review",
            status="PUBLISHED",
            review_required=True,
            max_score=10,
            created_by_id=teacher.id,
        )
        db.add(assessment)
        await db.flush()
        own = await _submission(
            db,
            assessment=assessment,
            version=version,
            student=own_student,
            source="int main(){return 0;}\n",
        )
        other = await _submission(
            db,
            assessment=assessment,
            version=version,
            student=other_student,
            source="int main(){return 1;}\n",
        )
        # Import provenance is audit data, not a teacher-to-student assignment.
        other.source = "MOODLE_IMPORT"
        other.external_receipt = {"actor_external_subjects": [teacher.external_subject]}
        other_snapshot = await db.get(Snapshot, other.snapshot_id)
        assert other_snapshot is not None
        report = EvidenceReport(
            submission_id=other.id,
            snapshot_id=other_snapshot.id,
            task_version_id=version.id,
            requested_by_id=teacher.id,
            hidden_test_manifest_hash=sha256_text("hidden-tests"),
            task_content_hash=version.content_hash,
            snapshot_manifest_hash=other_snapshot.manifest_hash,
            status="COMPLETED",
            passed_cases=1,
            total_cases=1,
            outcomes=[],
            findings=[],
            completed_at=utcnow(),
        )
        db.add(report)
        await db.flush()
        run = RunRequest(
            origin="IMMUTABLE_SUBMISSION",
            attempt_id=other.attempt_id,
            submission_id=other.id,
            evidence_report_id=report.id,
            evidence_case_index=0,
            requested_by_id=teacher.id,
            revision=other_snapshot.revision,
            mode="TEST",
            build_profile=version.build_profile,
            filesystem_profile="UNRESTRICTED_CONTAINER",
            network_enabled=False,
            status="COMPLETED",
        )
        db.add(run)
        await db.flush()
        db.add(
            RunResult(
                run_id=run.id,
                exit_code=0,
                exit_reason="SUCCESS",
                stdout="out-of-scope output",
                stderr="",
                diagnostics=[],
                metrics={},
                filesystem_isolated=True,
                network_enabled=False,
            )
        )
        await db.commit()
        return assessment.id, own.id, other.id, report.id, run.id


def test_legacy_subgroup_matching_requires_exact_surname_and_initials() -> None:
    assert legacy_subgroup_is_assigned_to_teacher(
        "2.4 подгруппа Коваленко А.С.", "Коваленко Алексей Сергеевич"
    )
    assert legacy_subgroup_is_assigned_to_teacher("2.4 подгруппа Коваленко А.", "Алексей Коваленко")
    assert legacy_subgroup_is_assigned_to_teacher(
        "2.4 подгруппа Коваленко А.С.", "Коваленко Алексей"
    )
    assert not legacy_subgroup_is_assigned_to_teacher(
        "2.4 подгруппа Ковален А.", "Алексей Коваленко"
    )
    assert not legacy_subgroup_is_assigned_to_teacher(
        "2.4 подгруппа Коваленко И.", "Алексей Коваленко"
    )
    assert not legacy_subgroup_is_assigned_to_teacher(
        "2.4 подгруппа Коваленко И.С.", "Коваленко Алексей"
    )
    assert not legacy_subgroup_is_assigned_to_teacher(
        "2.4 подгруппа Коваленко", "Алексей Коваленко"
    )


async def test_review_scope_filters_teacher_but_system_settings_can_read_all(app_bundle) -> None:
    app, session_factory, settings = app_bundle
    settings.dev_auth_enabled = True
    settings.admin_token = SecretStr("review-scope-admin-token")

    async with (
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as teacher,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as student,
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as admin,
    ):
        _teacher_session, teacher_headers = await _login(teacher, "TEACHER")
        await _login(student, "STUDENT")
        (
            assessment_id,
            own_id,
            other_id,
            other_report_id,
            other_run_id,
        ) = await _seed_scoped_submissions(session_factory)

        teacher_queue = await teacher.get(f"/api/v1/assessments/{assessment_id}/submissions")
        assert teacher_queue.status_code == 200, teacher_queue.text
        assert {row["id"] for row in teacher_queue.json()} == {str(own_id)}
        assert (await teacher.get(f"/api/v1/submissions/{own_id}")).status_code == 200
        forbidden = await teacher.get(f"/api/v1/submissions/{other_id}")
        assert forbidden.status_code == 403
        assert forbidden.json()["code"] == "SUBMISSION_REVIEW_SCOPE_REQUIRED"
        assert (await teacher.get(f"/api/v1/submissions/{own_id}/evidence-runs")).status_code == 200
        hidden_evidence = await teacher.get(f"/api/v1/submissions/{other_id}/evidence-runs")
        hidden_evidence_detail = await teacher.get(f"/api/v1/evidence-runs/{other_report_id}")
        assert hidden_evidence.status_code == hidden_evidence_detail.status_code == 403
        hidden_run = await teacher.get(f"/api/v1/runs/{other_run_id}")
        assert hidden_run.status_code == 404
        assert hidden_run.json()["code"] == "RUN_NOT_FOUND"
        forbidden_claim = await teacher.post(
            f"/api/v1/submissions/{other_id}/claims",
            headers=teacher_headers,
            json={},
        )
        assert forbidden_claim.status_code == 403
        assert forbidden_claim.json()["code"] == "SUBMISSION_REVIEW_SCOPE_REQUIRED"

        _admin_session, admin_headers = await _login(
            admin,
            "STUDENT",
            admin_token="review-scope-admin-token",
        )
        admin_queue = await admin.get(f"/api/v1/assessments/{assessment_id}/submissions")
        assert admin_queue.status_code == 200, admin_queue.text
        assert {row["id"] for row in admin_queue.json()} == {str(own_id), str(other_id)}
        assert all(row["can_review"] is True for row in admin_queue.json())
        admin_submission = await admin.get(f"/api/v1/submissions/{other_id}")
        assert admin_submission.status_code == 200
        assert admin_submission.json()["can_review"] is True
        admin_evidence = await admin.get(f"/api/v1/submissions/{other_id}/evidence-runs")
        admin_evidence_detail = await admin.get(f"/api/v1/evidence-runs/{other_report_id}")
        assert admin_evidence.status_code == admin_evidence_detail.status_code == 200
        assert [row["id"] for row in admin_evidence.json()] == [str(other_report_id)]
        admin_run = await admin.get(f"/api/v1/runs/{other_run_id}")
        assert admin_run.status_code == 200
        assert admin_run.json()["origin"] == "IMMUTABLE_SUBMISSION"

        # SYSTEM_SETTINGS is the deliberately global administrative override:
        # administrators can claim, run evidence, regrade and edit feedback
        # even when they are not assigned to the student's Moodle subgroup.
        admin_claim = await admin.post(
            f"/api/v1/submissions/{other_id}/claims",
            headers=admin_headers,
            json={},
        )
        assert admin_claim.status_code == 201, admin_claim.text
        admin_evidence_create = await admin.post(
            f"/api/v1/submissions/{other_id}/evidence-runs",
            headers=admin_headers,
            json={},
        )
        # The request crossed the global review boundary successfully. This
        # fixture deliberately has no runner URL, so execution stops only at
        # the infrastructure gate rather than with a scope denial.
        assert admin_evidence_create.status_code == 503, admin_evidence_create.text
        assert admin_evidence_create.json()["code"] == "RUNNER_DISABLED"
        admin_draft = await admin.put(
            f"/api/v1/submissions/{other_id}/review-draft",
            headers=admin_headers,
            json={"grade": "9.25", "comment": "Повторная проверка администратором"},
        )
        assert admin_draft.status_code == 200, admin_draft.text
        admin_decision = await admin.post(
            f"/api/v1/submissions/{other_id}/review-decisions",
            headers={**admin_headers, "Idempotency-Key": "admin-global-regrade"},
            json={"grade": "9.25", "comment": "Повторная проверка администратором"},
        )
        assert admin_decision.status_code == 201, admin_decision.text
        assert admin_decision.json()["grade"] == "9.25"
