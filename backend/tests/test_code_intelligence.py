from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from contextlib import ExitStack
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select

from app.api.code_intelligence import _Admission
from app.auth.context import require_auth
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.code_intelligence import CodeIntelligenceAdapter
from app.integrations.errors import (
    IntegrationConfigurationError,
    IntegrationProtocolError,
    IntegrationResponseTooLarge,
    IntegrationTimeout,
)
from app.models.attempts import Attempt, EditEvent, RunRequest, Workspace, WorkspaceFile
from app.models.courses import CourseMembership
from app.models.review import ReviewDecision, TeacherExperiment, TeacherExperimentFile
from app.schemas.code_intelligence import MAX_SOURCE_BYTES, CodeIntelligenceQuery
from app.services.common import DomainError, sha256_text
from app.services.workspace import replace_file_content
from tests.test_api_integrity_ai_system import _context, _csrf, _seed

ENDPOINT = "/api/v1/code-intelligence/query"


def _query(identifier, *, kind="attempt", **changes):
    return {
        "scope": {"kind": kind, "id": str(identifier)},
        "method": "completion",
        "path": "main.cpp",
        "position": {"line": 0, "character": 3},
        **changes,
    }


@pytest_asyncio.fixture
async def intelligence_bundle(app_bundle, monkeypatch):
    app, sessions, settings = app_bundle
    seed = await _seed(sessions)
    settings.code_intelligence_enabled = True
    settings.code_intelligence_url = "http://code-intelligence:8084"
    settings.code_intelligence_shared_secret = "fixture-code-intelligence-secret-long-enough"
    holder = {"auth": _context(seed.student_id, roles=("STUDENT",)), "calls": []}

    async def auth():
        return holder["auth"]

    async def database():
        async with sessions() as db:
            holder["db"] = db
            yield db

    async def query(_self, payload):
        assert not holder["db"].in_transaction(), "Do not hold a DB connection during indexing"
        holder["calls"].append(payload)
        if holder.get("error"):
            raise holder["error"]
        return {"result": [{"label": "size", "kind": 2}], "diagnostics": []}

    app.dependency_overrides[require_auth] = auth
    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(CodeIntelligenceAdapter, "query", query)
    yield app, sessions, settings, seed, holder
    app.dependency_overrides.clear()


async def test_attempt_draft_is_ephemeral_and_server_owns_build_and_identity(intelligence_bundle):
    app, sessions, _settings, seed, holder = intelligence_bundle
    draft = "#include <vector>\nint main() { std::vector<int> v; v. }\n"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        result = await client.post(
            ENDPOINT,
            headers=headers,
            json=_query(
                seed.attempt_id,
                files=[{"path": "main.cpp", "content": draft}],
            ),
        )
        assert result.status_code == 200, result.text
        sent = holder["calls"][0]
        assert sent["files"] == [{"path": "main.cpp", "content": draft}]
        assert sent["build_profile"] == "cpp-gcc-c++20-single"
        assert (
            sent["workspace_id"]
            == hashlib.sha256(
                f"{seed.student_id}:attempt:{seed.attempt_id}".encode("ascii")
            ).hexdigest()
        )
    async with sessions() as db:
        assert (await db.scalar(select(WorkspaceFile))).content == "int main() { return 0; }\n"
        assert (await db.scalar(select(Workspace))).current_revision == 0
        assert await db.scalar(select(func.count(EditEvent.id))) == 0
        assert await db.scalar(select(func.count(RunRequest.id))) == 0


async def test_immutable_submission_uses_snapshot_and_rejects_client_override(intelligence_bundle):
    app, sessions, _, seed, holder = intelligence_bundle
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
    async with sessions() as db, db.begin():
        (await db.scalar(select(WorkspaceFile))).content = "// unrelated live workspace changes"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        result = await client.post(
            ENDPOINT, headers=headers, json=_query(seed.submission_id, kind="submission")
        )
        assert result.status_code == 200, result.text
        assert holder["calls"][0]["files"][0]["content"] == "int main() { return 0; }\n"
        result = await client.post(
            ENDPOINT,
            headers=headers,
            json=_query(
                seed.submission_id,
                kind="submission",
                files=[{"path": "main.cpp", "content": "tampered"}],
            ),
        )
        assert result.status_code == 422
        assert len(holder["calls"]) == 1


@pytest.mark.parametrize(
    "identity,kind,status",
    [
        ("teacher_id", "attempt", 404),
        ("other_teacher_id", "attempt", 404),
        ("other_teacher_id", "submission", 403),
        ("unassigned_course_teacher_id", "submission", 403),
        ("student_id", "submission", 403),
    ],
)
async def test_scope_is_checked_for_every_query(intelligence_bundle, identity, kind, status):
    app, _, _, seed, holder = intelligence_bundle
    holder["auth"] = _context(getattr(seed, identity), roles=("TEACHER", "STUDENT"))
    identifier = seed.attempt_id if kind == "attempt" else seed.submission_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            ENDPOINT, headers=await _csrf(client), json=_query(identifier, kind=kind)
        )
        assert result.status_code == status, result.text
    assert not holder["calls"]


@pytest.mark.parametrize(
    "closure", ["SUBMITTED", "VOID", "deadline", "MOODLE_DELETED", "MOODLE_FINALIZED", "membership"]
)
async def test_closed_or_unauthorized_attempt_has_no_language_session(intelligence_bundle, closure):
    app, sessions, _, seed, holder = intelligence_bundle
    async with sessions() as db, db.begin():
        attempt = await db.get(Attempt, seed.attempt_id)
        if closure in {"SUBMITTED", "VOID"}:
            attempt.state = closure
        elif closure == "deadline":
            attempt.deadline_at = utcnow() - timedelta(seconds=1)
        elif closure == "membership":
            membership = await db.scalar(
                select(CourseMembership).where(
                    CourseMembership.principal_id == seed.student_id,
                )
            )
            membership.active = False
        else:
            attempt.submission_source = closure
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            ENDPOINT, headers=await _csrf(client), json=_query(seed.attempt_id)
        )
        assert result.status_code in {403, 409, 423}, result.text
    assert not holder["calls"]


async def test_experiment_is_private_and_checks_expiration_and_current_review_access(
    intelligence_bundle,
):
    app, sessions, _, seed, holder = intelligence_bundle
    async with sessions() as db, db.begin():
        experiment = TeacherExperiment(
            submission_id=seed.submission_id,
            owner_id=seed.teacher_id,
            base_snapshot_hash="a" * 64,
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(experiment)
        await db.flush()
        experiment_id = experiment.id
        db.add(
            TeacherExperimentFile(
                experiment_id=experiment.id,
                path="main.cpp",
                content="int main() {}",
                content_hash=sha256_text("int main() {}"),
            )
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        headers = await _csrf(client)
        holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))
        result = await client.post(
            ENDPOINT, headers=headers, json=_query(experiment_id, kind="experiment")
        )
        assert result.status_code == 404
        holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
        result = await client.post(
            ENDPOINT, headers=headers, json=_query(experiment_id, kind="experiment")
        )
        assert result.status_code == 200, result.text
        assert holder["calls"][0]["files"] == [{"path": "main.cpp", "content": "int main() {}"}]
        async with sessions() as db, db.begin():
            (await db.get(TeacherExperiment, experiment_id)).expires_at = utcnow() - timedelta(
                seconds=1
            )
        result = await client.post(
            ENDPOINT, headers=headers, json=_query(experiment_id, kind="experiment")
        )
        assert result.status_code == 404
    assert len(holder["calls"]) == 1


async def test_source_paths_must_already_belong_to_the_workspace(intelligence_bundle):
    app, _, _, seed, holder = intelligence_bundle
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            ENDPOINT,
            headers=await _csrf(client),
            json=_query(
                seed.attempt_id,
                files=[{"path": "another-student.cpp", "content": ""}],
            ),
        )
        assert result.status_code == 403
    assert not holder["calls"]


async def test_provider_failure_does_not_change_or_block_saving(intelligence_bundle):
    app, sessions, _, seed, holder = intelligence_bundle
    holder["error"] = IntegrationTimeout("DO_NOT_EXPOSE_SOURCE_OR_SECRETS")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            ENDPOINT, headers=await _csrf(client), json=_query(seed.attempt_id)
        )
        assert result.status_code == 503
        assert "DO_NOT_EXPOSE" not in result.text
    assert not app.state.code_intelligence_admission.active
    async with sessions() as db:
        source = await db.scalar(select(WorkspaceFile))
        await replace_file_content(
            db,
            attempt_id=seed.attempt_id,
            principal_id=seed.student_id,
            file_id=source.id,
            content="int main() { return 1; }",
            expected_revision=0,
            client_request_id="save-after-language-server-failure",
        )
        await db.commit()
        assert source.content == "int main() { return 1; }"
        assert await db.scalar(select(func.count(RunRequest.id))) == 0


async def test_csrf_auth_and_bounded_request_body(intelligence_bundle):
    app, _, settings, seed, holder = intelligence_bundle
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(ENDPOINT, json=_query(seed.attempt_id))
        assert result.status_code == 403
        assert result.json()["code"] == "CSRF_FAILED"
        headers = await _csrf(client)
        settings.code_intelligence_request_body_max_bytes = 1024
        result = await client.post(ENDPOINT, headers=headers, content=" " * 1025)
        assert result.status_code == 413
        app.dependency_overrides.pop(require_auth)
        result = await client.post(ENDPOINT, headers=headers, json=_query(seed.attempt_id))
        assert result.status_code == 401
    assert not holder["calls"]


@pytest.mark.parametrize(
    "path",
    [
        "../main.cpp",
        "/tmp/main.cpp",
        "nested/../main.cpp",
        "nested//main.cpp",
        "C:/main.cpp",
        "nested\\main.cpp",
        ".clangd",
        "compile_commands.json",
        ".hidden/main.cpp",
        "main.cpp\x00",
        "main\n.cpp",
        "main.txt",
    ],
)
def test_protocol_rejects_paths_and_configuration_files(path):
    with pytest.raises(ValidationError):
        CodeIntelligenceQuery.model_validate(_query(uuid.uuid4(), path=path))


@pytest.mark.parametrize(
    "changes",
    [
        {"method": "workspace/executeCommand"},
        {"build_profile": "attacker-controlled"},
        {"workspace_id": "someone-else"},
        {"context": {"command": "rm"}},
        {"position": {"line": -1, "character": 0}},
        {"position": {"line": True, "character": 0}},
        {"position": None},
        {"files": [{"path": "main.cpp", "content": ""}] * 2},
        {"files": [{"path": "main.cpp", "content": "я" * (MAX_SOURCE_BYTES // 2 + 1)}]},
    ],
)
def test_protocol_is_allowlisted_and_utf8_bounded(changes):
    with pytest.raises(ValidationError):
        CodeIntelligenceQuery.model_validate(_query(uuid.uuid4(), **changes))


def test_admission_is_bounded_and_released_on_failure():
    admission = _Admission()
    principal = uuid.uuid4()
    with ExitStack() as contexts:
        for _ in range(4):
            contexts.enter_context(admission.acquire(principal))
        with pytest.raises(DomainError) as error:
            with admission.acquire(principal):
                pass
        assert error.value.status_code == 429
    assert not admission.active
    with pytest.raises(RuntimeError), admission.acquire(principal):
        raise RuntimeError("cancelled")
    assert not admission.active
    admission.rates[principal] = (0, time.monotonic())
    with pytest.raises(DomainError):
        with admission.acquire(principal):
            pass


def test_admission_has_global_concurrency_and_bounded_rate_state():
    admission = _Admission()
    with ExitStack() as contexts:
        for _ in range(32):
            contexts.enter_context(admission.acquire(uuid.uuid4()))
        with pytest.raises(DomainError):
            with admission.acquire(uuid.uuid4()):
                pass
    assert not admission.active
    for _ in range(2050):
        with admission.acquire(uuid.uuid4()):
            pass
    assert len(admission.rates) == 2048


async def test_experiment_revoked_review_claim_disables_queries(intelligence_bundle):
    app, sessions, _, seed, holder = intelligence_bundle
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
    async with sessions() as db, db.begin():
        for decision in (await db.scalars(select(ReviewDecision))).all():
            decision.status = "SUPERSEDED"
        experiment = TeacherExperiment(
            submission_id=seed.submission_id,
            owner_id=seed.teacher_id,
            base_snapshot_hash="a" * 64,
            expires_at=utcnow() + timedelta(hours=1),
        )
        db.add(experiment)
        await db.flush()
        experiment_id = experiment.id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        result = await client.post(
            ENDPOINT,
            headers=await _csrf(client),
            json=_query(experiment_id, kind="experiment"),
        )
        assert result.status_code == 409
        assert result.json()["code"] == "ACTIVE_REVIEW_CLAIM_REQUIRED"
    assert not holder["calls"]


def _upstream():
    return {
        "workspace_id": "a" * 64,
        "build_profile": "cpp-gcc-c++20-single",
        "files": [{"path": "main.cpp", "content": "int main() {}"}],
        "method": "definition",
        "path": "main.cpp",
        "position": {"line": 0, "character": 5},
    }


async def test_adapter_signs_exact_bounded_body_without_proxy_or_user_credentials(app_bundle):
    settings = app_bundle[2]
    settings.code_intelligence_url = "http://code-intelligence:8084"
    settings.code_intelligence_shared_secret = "secret-for-code-intelligence-32-characters"
    received = []

    async def handler(request):
        received.append(request)
        canonical = f"123456\nnonce\nPOST\n/v1/query\n{hashlib.sha256(request.content).hexdigest()}"
        expected = hmac.new(
            b"secret-for-code-intelligence-32-characters",
            canonical.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        assert request.headers["X-Code-Intelligence-Signature"] == f"v1={expected}"
        assert request.headers["X-Code-Intelligence-Timestamp"] == "123456"
        assert request.headers["X-Code-Intelligence-Nonce"] == "nonce"
        assert json.loads(request.content) == _upstream()
        assert "authorization" not in request.headers and "cookie" not in request.headers
        return httpx.Response(200, json={"result": [{"uri": "main.cpp"}], "diagnostics": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False) as client:
        result = await CodeIntelligenceAdapter(
            settings, client, clock=lambda: 123456, nonce_factory=lambda: "nonce"
        ).query(_upstream())
    assert result["result"][0]["uri"] == "main.cpp"
    assert len(received) == 1


@pytest.mark.parametrize(
    "response,exception",
    [
        ({"result": [{"uri": "file:///etc/passwd"}], "diagnostics": []}, IntegrationProtocolError),
        ({"result": [{"uri": "other-student.cpp"}], "diagnostics": []}, IntegrationProtocolError),
        (
            {"result": None, "diagnostics": [{"path": "secret.cpp", "diagnostics": []}]},
            IntegrationProtocolError,
        ),
        ({"result": "x" * 2048, "diagnostics": []}, IntegrationResponseTooLarge),
    ],
)
async def test_adapter_rejects_external_paths_and_oversize_responses(
    app_bundle, response, exception
):
    settings = app_bundle[2]
    settings.code_intelligence_url = "http://code-intelligence:8084"
    settings.code_intelligence_shared_secret = "secret-for-code-intelligence-32-characters"
    settings.code_intelligence_response_body_max_bytes = 1024
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=response))
    ) as client:
        with pytest.raises(exception):
            await CodeIntelligenceAdapter(settings, client).query(_upstream())


@pytest.mark.parametrize(
    "url", ["", "file:///etc/passwd", "http://user:secret@localhost", "http://localhost?next=x"]
)
async def test_adapter_rejects_invalid_configuration(app_bundle, url):
    settings = app_bundle[2]
    settings.code_intelligence_url = url
    settings.code_intelligence_shared_secret = "secret-for-code-intelligence-32-characters"
    async with httpx.AsyncClient() as client:
        with pytest.raises(IntegrationConfigurationError):
            await CodeIntelligenceAdapter(settings, client).query(_upstream())
