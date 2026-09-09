from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal

import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.api import ai as ai_api
from app.api import integrity as integrity_api
from app.api import system as system_api
from app.auth.context import AuthContext, require_auth
from app.integrations.ai import AIAnswer
from app.integrations.errors import IntegrationProtocolError
from app.models.analysis import (
    AuthorshipAnalysisJob,
    PlagiarismCase,
    SimilarityAnalysis,
    SimilarityMatch,
)
from app.models.attempts import Attempt, Snapshot, Submission, Workspace, WorkspaceFile
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
)
from app.models.enums import AnalysisState, AttemptState, SyncOutboxState
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import SyncOutbox
from app.models.review import ChatMessage, ReviewDecision
from app.models.tasks import Assessment, TaskBankItem, TaskVersion
from app.services.common import canonical_hash, sha256_text


@dataclass(frozen=True, slots=True)
class Seed:
    connection_id: uuid.UUID
    course_id: uuid.UUID
    other_course_id: uuid.UUID
    teacher_id: uuid.UUID
    other_teacher_id: uuid.UUID
    unassigned_course_teacher_id: uuid.UUID
    student_id: uuid.UUID
    admin_id: uuid.UUID
    assessment_id: uuid.UUID
    attempt_id: uuid.UUID
    submission_id: uuid.UUID
    similarity_id: uuid.UUID
    checkpoint_outbox_id: uuid.UUID
    grade_outbox_id: uuid.UUID


def _context(
    principal_id: uuid.UUID,
    *,
    roles: tuple[str, ...] = (),
    capabilities: tuple[str, ...] = (),
) -> AuthContext:
    return AuthContext(
        principal_id=principal_id,
        display_name="API test principal",
        session_id=uuid.uuid4(),
        session_key="test-session",
        roles=roles,
        capabilities=capabilities,
    )


async def _grant_teacher(db, principal: ExternalPrincipal) -> None:
    token = TeacherAccessToken(
        public_id=principal.id.hex[:16],
        label=f"Test grant: {principal.display_name}",
        secret_hash="$argon2id$test-fixture-not-used-for-login",
        created_by_id=principal.id,
    )
    db.add(token)
    await db.flush()
    db.add(TeacherTokenGrant(token_id=token.id, principal_id=principal.id))
    await db.flush()


async def _seed(session_factory) -> Seed:
    async with session_factory() as db, db.begin():
        connection = LMSConnection(
            name="Mock LMS",
            provider="MOCK",
            base_url="https://lms.test",
        )
        db.add(connection)
        await db.flush()
        teacher = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="teacher-1",
            display_name="Teacher One",
        )
        other_teacher = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="teacher-2",
            display_name="Teacher Two",
        )
        unassigned_course_teacher = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="teacher-3",
            display_name="Teacher Three",
        )
        student = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="student-1",
            display_name="Student One",
        )
        admin = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="admin-1",
            display_name="Administrator",
        )
        course = Course(
            connection_id=connection.id,
            external_id="course-1",
            title="C++ course",
            catalog_enabled=True,
        )
        other_course = Course(
            connection_id=connection.id,
            external_id="course-2",
            title="Other course",
            catalog_enabled=True,
        )
        db.add_all(
            [
                teacher,
                other_teacher,
                unassigned_course_teacher,
                student,
                admin,
                course,
                other_course,
            ]
        )
        await db.flush()
        teacher_membership = CourseMembership(
            course_id=course.id,
            principal_id=teacher.id,
            role="TEACHER",
        )
        student_membership = CourseMembership(
            course_id=course.id,
            principal_id=student.id,
            role="STUDENT",
        )
        other_teacher_membership = CourseMembership(
            course_id=other_course.id,
            principal_id=other_teacher.id,
            role="TEACHER",
        )
        unassigned_course_teacher_membership = CourseMembership(
            course_id=course.id,
            principal_id=unassigned_course_teacher.id,
            role="TEACHER",
        )
        review_group = CourseGroup(
            course_id=course.id,
            external_id="teacher-one-group",
            name="1.1 Teacher One",
        )
        db.add_all(
            [
                teacher_membership,
                student_membership,
                other_teacher_membership,
                unassigned_course_teacher_membership,
                review_group,
            ]
        )
        await db.flush()
        db.add_all(
            [
                CourseMembershipGroup(
                    coursemembership_id=teacher_membership.id,
                    coursegroup_id=review_group.id,
                ),
                CourseMembershipGroup(
                    coursemembership_id=student_membership.id,
                    coursegroup_id=review_group.id,
                ),
            ]
        )
        await _grant_teacher(db, teacher)
        await _grant_teacher(db, other_teacher)
        await _grant_teacher(db, unassigned_course_teacher)
        task_item = TaskBankItem(
            scope="COURSE",
            course_id=course.id,
            slug="sum",
            created_by_id=teacher.id,
        )
        db.add(task_item)
        await db.flush()
        source = "int main() { return 0; }\n"
        task_version = TaskVersion(
            item_id=task_item.id,
            number=1,
            title="Sum",
            statement="Return zero for the fixture",
            language="CPP",
            language_standard="C++20",
            multi_file=False,
            starter_files=[{"path": "main.cpp", "content": ""}],
            build_profile="cpp-gcc-c++20-single",
            public_examples=[],
            hidden_test_manifest={},
            max_score=Decimal("10.00"),
            ai_policy={},
            content_hash=sha256_text("task-v1"),
            status="PUBLISHED",
            authored_by_id=teacher.id,
        )
        assessment = Assessment(
            course_id=course.id,
            type="LAB",
            title="Lab one",
            instructions="Explain diagnostics, do not write the solution.",
            max_score=Decimal("10.00"),
            student_ai_enabled=True,
            teacher_ai_enabled=True,
            status="PUBLISHED",
            created_by_id=teacher.id,
        )
        db.add_all([task_version, assessment])
        await db.flush()
        attempt = Attempt(
            assessment_id=assessment.id,
            assigned_task_version_id=task_version.id,
            principal_id=student.id,
            state=AttemptState.ACTIVE.value,
            current_revision=0,
        )
        db.add(attempt)
        await db.flush()
        workspace = Workspace(
            attempt_id=attempt.id,
            current_revision=0,
            current_hash=sha256_text(source),
            event_chain_head=sha256_text("event-chain"),
            aggregate_size=len(source.encode()),
        )
        db.add(workspace)
        await db.flush()
        source_file = WorkspaceFile(
            workspace_id=workspace.id,
            path="main.cpp",
            language="CPP",
            content=source,
            content_hash=sha256_text(source),
        )
        db.add(source_file)
        await db.flush()
        files = [
            {
                "id": str(source_file.id),
                "path": source_file.path,
                "language": source_file.language,
                "content": source_file.content,
                "content_hash": source_file.content_hash,
            }
        ]
        snapshot = Snapshot(
            workspace_id=workspace.id,
            revision=0,
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
            source="STUDENT",
        )
        db.add(submission)
        await db.flush()
        similarity = SimilarityAnalysis(
            assessment_id=assessment.id,
            task_version_id=task_version.id,
            requested_by_id=teacher.id,
            state=AnalysisState.COMPLETED.value,
            submission_count=1,
            comparison_count=0,
            match_count=0,
        )
        db.add(similarity)
        old_decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=teacher.id,
            revision=1,
            grade=Decimal("5.00"),
            status="SUPERSEDED",
            lms_export_state="SUPERSEDED",
        )
        db.add(old_decision)
        await db.flush()
        new_decision = ReviewDecision(
            submission_id=submission.id,
            reviewer_id=teacher.id,
            revision=2,
            grade=Decimal("8.00"),
            supersedes_id=old_decision.id,
        )
        db.add(new_decision)
        await db.flush()
        checkpoint = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            attempt_id=attempt.id,
            event_type="attempt.checkpoint",
            aggregate_type="Snapshot",
            aggregate_id=snapshot.id,
            payload={"snapshot_ref": str(snapshot.id)},
            idempotency_key="checkpoint-test",
            state=SyncOutboxState.FAILED.value,
            last_error="temporary failure",
        )
        grade = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            event_type="review.decision",
            aggregate_type="ReviewDecision",
            aggregate_id=old_decision.id,
            payload={"grade": "5.00"},
            idempotency_key="grade-test",
            state=SyncOutboxState.FAILED.value,
            last_error="temporary failure",
        )
        db.add_all([checkpoint, grade])
        await db.flush()
        return Seed(
            connection_id=connection.id,
            course_id=course.id,
            other_course_id=other_course.id,
            teacher_id=teacher.id,
            other_teacher_id=other_teacher.id,
            unassigned_course_teacher_id=unassigned_course_teacher.id,
            student_id=student.id,
            admin_id=admin.id,
            assessment_id=assessment.id,
            attempt_id=attempt.id,
            submission_id=submission.id,
            similarity_id=similarity.id,
            checkpoint_outbox_id=checkpoint.id,
            grade_outbox_id=grade.id,
        )


@pytest_asyncio.fixture
async def api_bundle(app_bundle):
    app, session_factory, settings = app_bundle
    app.include_router(integrity_api.router, prefix=settings.api_prefix)
    app.include_router(ai_api.router, prefix=settings.api_prefix)
    app.include_router(system_api.router, prefix=settings.api_prefix)
    seed = await _seed(session_factory)
    settings.ai_mock_enabled = True
    settings.runner_mock_enabled = True
    holder = {
        "auth": _context(seed.teacher_id, roles=("TEACHER",)),
    }

    async def current_auth() -> AuthContext:
        return holder["auth"]

    app.dependency_overrides[require_auth] = current_auth
    yield app, session_factory, settings, seed, holder
    app.dependency_overrides.clear()


async def _csrf(client: AsyncClient) -> dict[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    return {"X-CSRFToken": response.json()["csrf_token"]}


class ProtocolAuthorshipTransport:
    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.observed_state: str | None = None

    async def analyze(self, _payload, *, manifest_hash: str):
        assert len(manifest_hash) == 64
        async with self.session_factory() as db:
            self.observed_state = await db.scalar(
                select(AuthorshipAnalysisJob.state).order_by(
                    AuthorshipAnalysisJob.created_at.desc()
                )
            )
        raise IntegrationProtocolError("Analyzer returned an invalid schema")


async def test_authorship_commits_running_and_never_fakes_probability(api_bundle):
    app, session_factory, _settings, seed, holder = api_bundle
    transport = ProtocolAuthorshipTransport(session_factory)
    app.state.authorship_transport = transport
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        created = await client.post(
            f"/api/v1/submissions/{seed.submission_id}/authorship-analyses",
            json={},
            headers=headers,
        )

        assert created.status_code == 201, created.text
        assert created.json()["state"] == "INVALID"
        assert created.json()["result"] is None
        assert "probability" not in created.json()
        assert transport.observed_state == "RUNNING"

        holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))
        isolated = await client.get(f"/api/v1/submissions/{seed.submission_id}/authorship-analyses")
        global_list = await client.get("/api/v1/authorship-analyses")
        similarity_list = await client.get("/api/v1/similarity-analyses")

    assert isolated.status_code == 403
    assert global_list.status_code == 200
    assert global_list.json() == []
    assert similarity_list.status_code == 200
    assert similarity_list.json() == []


async def test_similarity_comparison_discloses_peer_only_for_evidence_backed_pair(api_bundle):
    app, session_factory, _settings, seed, holder = api_bundle
    peer_source = "int main() { return 1; }\n"
    async with session_factory() as db, db.begin():
        course = await db.get(Course, seed.course_id)
        analysis = await db.get(SimilarityAnalysis, seed.similarity_id)
        original = await db.get(Submission, seed.submission_id)
        original_snapshot = await db.get(Snapshot, original.snapshot_id) if original else None
        assert course is not None and analysis is not None and original_snapshot is not None
        peer = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject="student-peer",
            display_name="Peer Student",
        )
        peer_group = CourseGroup(
            course_id=course.id,
            external_id="unassigned-peer-group",
            name="2.2 подгруппа Teacher Two",
        )
        db.add_all([peer, peer_group])
        await db.flush()
        peer_membership = CourseMembership(
            course_id=course.id,
            principal_id=peer.id,
            role="STUDENT",
        )
        peer_attempt = Attempt(
            assessment_id=seed.assessment_id,
            assigned_task_version_id=analysis.task_version_id,
            principal_id=peer.id,
            state=AttemptState.SUBMITTED.value,
            current_revision=0,
        )
        db.add_all([peer_membership, peer_attempt])
        await db.flush()
        db.add(
            CourseMembershipGroup(
                coursemembership_id=peer_membership.id,
                coursegroup_id=peer_group.id,
            )
        )
        peer_workspace = Workspace(
            attempt_id=peer_attempt.id,
            current_revision=0,
            current_hash=sha256_text(peer_source),
            event_chain_head=sha256_text("peer-event-chain"),
            aggregate_size=len(peer_source.encode()),
        )
        db.add(peer_workspace)
        await db.flush()
        peer_files = [
            {
                "id": str(uuid.uuid4()),
                "path": "main.cpp",
                "language": "CPP",
                "content": peer_source,
                "content_hash": sha256_text(peer_source),
            }
        ]
        peer_snapshot = Snapshot(
            workspace_id=peer_workspace.id,
            revision=0,
            event_chain_head=peer_workspace.event_chain_head,
            manifest_hash=canonical_hash(peer_files),
            files=peer_files,
            reason="SUBMISSION",
        )
        db.add(peer_snapshot)
        await db.flush()
        peer_submission = Submission(
            attempt_id=peer_attempt.id,
            snapshot_id=peer_snapshot.id,
            revision=1,
            source="STUDENT",
        )
        db.add(peer_submission)
        await db.flush()
        match = SimilarityMatch(
            analysis_id=analysis.id,
            submission_a_id=original.id,
            submission_b_id=peer_submission.id,
            manifest_hash_a=original_snapshot.manifest_hash,
            manifest_hash_b=peer_snapshot.manifest_hash,
            score=Decimal("0.750000"),
            fingerprint_count_a=4,
            fingerprint_count_b=4,
            shared_fingerprint_count=3,
            evidence=[
                {
                    "a": {
                        "path": "main.cpp",
                        "start_line": 1,
                        "end_line": 1,
                        "fragment": "return 0;",
                    },
                    "b": {
                        "path": "main.cpp",
                        "start_line": 1,
                        "end_line": 1,
                        "fragment": "return 1;",
                    },
                    "normalized_tokens": ["return", "number"],
                }
            ],
        )
        db.add(match)
        await db.flush()
        db.add(PlagiarismCase(match_id=match.id))
        analysis.submission_count = 2
        analysis.comparison_count = 1
        analysis.match_count = 1
        match_id = match.id

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
        comparison = await client.get(f"/api/v1/similarity-matches/{match_id}/comparison")
        scoped_analysis = await client.get(
            f"/api/v1/assessments/{seed.assessment_id}/similarity-analyses"
        )
        assert comparison.status_code == 200, comparison.text
        assert comparison.json()["right"]["student_name"] == "Peer Student"
        assert comparison.json()["right"]["files"][0]["content"] == peer_source
        assert comparison.json()["match"]["evidence"][0]["start_line_a"] == 1
        assert scoped_analysis.json()[0]["match_count"] == 1

        holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))
        hidden = await client.get(f"/api/v1/similarity-matches/{match_id}/comparison")
        assert hidden.status_code == 404

        holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
        admin_view = await client.get(f"/api/v1/similarity-matches/{match_id}/comparison")
        assert admin_view.status_code == 200, admin_view.text


class RecordingAIProvider:
    def __init__(self):
        self.calls: list[dict] = []

    async def answer(self, *, mode, question, context, history):
        self.calls.append(
            {
                "mode": mode,
                "question": question,
                "context": context,
                "history": list(history),
            }
        )
        return AIAnswer(
            content=f"Explanation {len(self.calls)}",
            citations=[
                {
                    "title": "cppreference · statements",
                    "url": "https://en.cppreference.com/w/cpp/language/statements",
                }
            ],
            model="recording-v1",
        )


async def test_ai_uses_exact_context_and_bounded_persisted_history(api_bundle):
    app, session_factory, _settings, seed, holder = api_bundle
    provider = RecordingAIProvider()
    app.state.ai_provider = provider
    holder["auth"] = _context(seed.student_id, roles=("STUDENT",))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        thread_response = await client.post(
            "/api/v1/ai/student-threads",
            json={
                "attempt": str(seed.attempt_id),
                "course": str(seed.course_id),
                "revision": 0,
            },
            headers=headers,
        )
        assert thread_response.status_code == 201, thread_response.text
        thread_id = thread_response.json()["id"]

        first = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "Explain the first diagnostic"},
            headers=headers,
        )
        second = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "What should I inspect next?"},
            headers=headers,
        )
        messages = await client.get(f"/api/v1/ai/threads/{thread_id}/messages")

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert messages.status_code == 200
        assert [item["role"] for item in messages.json()] == [
            "USER",
            "ASSISTANT",
            "USER",
            "ASSISTANT",
        ]
        assert provider.calls[0]["history"] == []
        assert provider.calls[0]["context"]["attempt"]["revision"] == 0
        assert provider.calls[0]["context"]["files"][0]["content"].startswith("int main")
        assert provider.calls[1]["history"] == [
            {"role": "user", "content": "Explain the first diagnostic"},
            {"role": "assistant", "content": "Explanation 1"},
        ]

        holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))
        hidden = await client.get(f"/api/v1/ai/threads/{thread_id}")

    assert hidden.status_code == 404
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 4
        assert await db.scalar(select(func.count()).select_from(ReviewDecision)) == 2


async def test_student_ai_message_budget_is_server_enforced(api_bundle):
    app, _session_factory, settings, seed, holder = api_bundle
    settings.ai_student_rate_limit_messages = 1
    settings.ai_rate_limit_window_seconds = 60
    app.state.ai_provider = RecordingAIProvider()
    holder["auth"] = _context(seed.student_id, roles=("STUDENT",))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        thread_response = await client.post(
            "/api/v1/ai/student-threads",
            json={
                "attempt": str(seed.attempt_id),
                "course": str(seed.course_id),
                "revision": 0,
            },
            headers=headers,
        )
        assert thread_response.status_code == 201, thread_response.text
        thread_id = thread_response.json()["id"]
        accepted = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "Explain this diagnostic"},
            headers=headers,
        )
        limited = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "Explain the next step"},
            headers=headers,
        )

    assert accepted.status_code == 200, accepted.text
    assert limited.status_code == 429, limited.text
    assert limited.json()["code"] == "AI_RATE_LIMIT"
    assert limited.json()["details"] == {"limit": 1, "window_seconds": 60}


async def test_system_settings_can_audit_teacher_chat_but_cannot_mutate_it(api_bundle):
    app, session_factory, _settings, seed, holder = api_bundle
    provider = RecordingAIProvider()
    app.state.ai_provider = provider
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        created = await client.post(
            "/api/v1/ai/teacher-threads",
            json={
                "submission": str(seed.submission_id),
                "course": str(seed.course_id),
                "title": "Review audit chat",
            },
            headers=headers,
        )
        assert created.status_code == 201, created.text
        thread_id = created.json()["id"]
        message = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "What should I verify manually?"},
            headers=headers,
        )
        assert message.status_code == 200, message.text

        holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
        thread = await client.get(f"/api/v1/ai/threads/{thread_id}")
        messages = await client.get(f"/api/v1/ai/threads/{thread_id}/messages")
        threads = await client.get(f"/api/v1/ai/threads?course_id={seed.course_id}&mode=TEACHER")
        assert thread.status_code == messages.status_code == threads.status_code == 200
        assert [row["id"] for row in threads.json()] == [thread_id]
        assert [row["role"] for row in messages.json()] == ["USER", "ASSISTANT"]

        denied_message = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "Change this review"},
            headers=headers,
        )
        denied_close = await client.post(
            f"/api/v1/ai/threads/{thread_id}/close",
            json={},
            headers=headers,
        )

    assert denied_message.status_code == denied_close.status_code == 404
    assert len(provider.calls) == 1
    assert provider.calls[0]["mode"] == "TEACHER"
    assert provider.calls[0]["question"] == "What should I verify manually?"
    async with session_factory() as db:
        decisions = list(
            (
                await db.scalars(
                    select(ReviewDecision)
                    .where(ReviewDecision.submission_id == seed.submission_id)
                    .order_by(ReviewDecision.revision)
                )
            ).all()
        )
    assert [(row.revision, row.grade, row.status) for row in decisions] == [
        (1, Decimal("5.00"), "SUPERSEDED"),
        (2, Decimal("8.00"), "APPLIED"),
    ]


async def test_disabled_decision_support_blocks_new_analysis_and_teacher_ai_only(api_bundle):
    app, session_factory, _settings, seed, holder = api_bundle
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        existing_thread = await client.post(
            "/api/v1/ai/teacher-threads",
            json={
                "submission": str(seed.submission_id),
                "course": str(seed.course_id),
                "title": "Existing audit chat",
            },
            headers=headers,
        )
        assert existing_thread.status_code == 201, existing_thread.text
        thread_id = existing_thread.json()["id"]

        async with session_factory() as db, db.begin():
            assessment = await db.get(Assessment, seed.assessment_id)
            snapshot = await db.scalar(
                select(Snapshot)
                .join(Submission, Submission.snapshot_id == Snapshot.id)
                .where(Submission.id == seed.submission_id)
            )
            assert assessment is not None and snapshot is not None
            assessment.decision_support_enabled = False
            old_authorship = AuthorshipAnalysisJob(
                submission_id=seed.submission_id,
                requested_by_id=seed.teacher_id,
                manifest_hash=snapshot.manifest_hash,
                payload_hash=canonical_hash({"legacy": True}),
                state="PENDING",
            )
            db.add(old_authorship)
            await db.flush()
            old_authorship_id = old_authorship.id

        old_authorships = await client.get(
            f"/api/v1/submissions/{seed.submission_id}/authorship-analyses"
        )
        old_similarities = await client.get(
            f"/api/v1/assessments/{seed.assessment_id}/similarity-analyses"
        )
        old_thread = await client.get(f"/api/v1/ai/threads/{thread_id}")
        old_messages = await client.get(f"/api/v1/ai/threads/{thread_id}/messages")
        assert old_authorships.status_code == 200
        assert [row["id"] for row in old_authorships.json()] == [str(old_authorship_id)]
        assert old_similarities.status_code == 200
        assert [row["id"] for row in old_similarities.json()] == [str(seed.similarity_id)]
        assert old_thread.status_code == 200
        assert old_messages.status_code == 200
        assert old_messages.json() == []

        authorship = await client.post(
            f"/api/v1/submissions/{seed.submission_id}/authorship-analyses",
            json={},
            headers=headers,
        )
        similarity = await client.post(
            f"/api/v1/assessments/{seed.assessment_id}/similarity-analyses",
            json={},
            headers=headers,
        )
        teacher_thread = await client.post(
            "/api/v1/ai/teacher-threads",
            json={
                "submission": str(seed.submission_id),
                "course": str(seed.course_id),
            },
            headers=headers,
        )
        teacher_message = await client.post(
            f"/api/v1/ai/threads/{thread_id}/messages",
            json={"content": "Generate a new assessment hint"},
            headers=headers,
        )

    for response in (authorship, similarity, teacher_thread, teacher_message):
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "DECISION_SUPPORT_DISABLED"
    async with session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(AuthorshipAnalysisJob)) == 1
        assert await db.scalar(select(func.count()).select_from(SimilarityAnalysis)) == 1
        assert await db.scalar(select(func.count()).select_from(ChatMessage)) == 0


async def test_system_revision_and_scoped_outbox_retry(api_bundle):
    app, session_factory, _settings, seed, holder = api_bundle
    async with session_factory() as db, db.begin():
        checkpoint = await db.get(SyncOutbox, seed.checkpoint_outbox_id)
        assert checkpoint is not None
        checkpoint.attempts = 8
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
        forbidden_settings = await client.get("/api/v1/system/settings")
        assert forbidden_settings.status_code == 403

        holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
        initial = await client.get("/api/v1/system/settings")
        assert initial.status_code == 200
        assert initial.json()["revision"] == 1
        updated = await client.patch(
            "/api/v1/system/settings",
            json={"revision": 1, "student_ai_enabled": False},
            headers=headers,
        )
        stale = await client.patch(
            "/api/v1/system/settings",
            json={"revision": 1, "student_ai_enabled": True},
            headers=headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["revision"] == 2
        assert updated.json()["student_ai_enabled"] is False
        assert stale.status_code == 409

        admin_outbox = await client.get("/api/v1/integrations/lms/outbox")
        admin_event_ids = {row["id"] for row in admin_outbox.json()}
        assert str(seed.checkpoint_outbox_id) in admin_event_ids
        assert str(seed.grade_outbox_id) in admin_event_ids

        holder["auth"] = _context(
            seed.unassigned_course_teacher_id,
            roles=("TEACHER",),
        )
        unassigned_list = await client.get("/api/v1/integrations/lms/outbox")
        unassigned_checkpoint = await client.get(
            f"/api/v1/integrations/lms/outbox/{seed.checkpoint_outbox_id}"
        )
        unassigned_grade = await client.get(
            f"/api/v1/integrations/lms/outbox/{seed.grade_outbox_id}"
        )
        unassigned_retry = await client.post(
            f"/api/v1/integrations/lms/outbox/{seed.checkpoint_outbox_id}/retry",
            json={"reason": "Must not retry another group's source"},
            headers=headers,
        )
        unassigned_event_ids = {row["id"] for row in unassigned_list.json()}
        assert str(seed.checkpoint_outbox_id) not in unassigned_event_ids
        assert str(seed.grade_outbox_id) not in unassigned_event_ids
        assert unassigned_checkpoint.status_code == 404
        assert unassigned_grade.status_code == 404
        assert unassigned_retry.status_code == 404

        holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))
        isolated_list = await client.get("/api/v1/integrations/lms/outbox")
        isolated_detail = await client.get(
            f"/api/v1/integrations/lms/outbox/{seed.checkpoint_outbox_id}"
        )
        assert isolated_list.status_code == 200
        assert isolated_list.json() == []
        assert isolated_detail.status_code == 404

        holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
        retried = await client.post(
            f"/api/v1/integrations/lms/outbox/{seed.checkpoint_outbox_id}/retry",
            json={"reason": "Moodle is available again"},
            headers=headers,
        )
        superseded = await client.post(
            f"/api/v1/integrations/lms/outbox/{seed.grade_outbox_id}/retry",
            json={"reason": "Do not export stale grade"},
            headers=headers,
        )

    assert retried.status_code == 200, retried.text
    assert retried.json()["state"] == "RETRY"
    assert retried.json()["attempts"] == 0
    assert superseded.status_code == 409
    assert superseded.json()["code"] == "GRADE_DECISION_SUPERSEDED"
