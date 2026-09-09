from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import Settings
from app.integrations.ai import AIProvider
from app.integrations.authorship import AuthorshipTransport
from app.integrations.errors import (
    IntegrationProtocolError,
    IntegrationResponseTooLarge,
    IntegrationTimeout,
)
from app.integrations.moodle import MoodleBridge
from app.integrations.runner import RunnerAdapter


def configured_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "debug": True,
        "secret_key": "test-secret-" + "x" * 40,
        "moodle_base_url": "https://moodle.example.edu",
        "moodle_service_token": "moodle-token",
        "runner_url": "http://runner:8081",
        "runner_shared_secret": "r" * 32,
        "ai_enabled": True,
        "ai_api_key": "ai-test-key",
        "ai_base_url": "https://ai.example.test/v1",
        "ai_model": "test-model",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.mark.asyncio
async def test_moodle_discovery_uses_only_narrow_bridge_functions() -> None:
    calls: list[dict[str, list[str]]] = []
    course_payload = {
        "course": {
            "id": 549,
            "shortname": "CPP",
            "fullname": "C++",
            "startdate": 0,
            "enddate": 0,
        },
        "sections": [{"id": 10, "number": 1, "name": "Lab", "visible": True, "activities": []}],
    }
    roster_payload = {
        "members": [
            {
                "user_id": 7,
                "username": "teacher",
                "role": "TEACHER",
                "suspended": False,
                "groups": [{"id": 3, "name": "2.1"}],
            }
        ]
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        calls.append(form)
        function = form["wsfunction"][0]
        payload = course_payload if function.endswith("get_course_snapshot") else roster_payload
        revision = "a" * 64 if function.endswith("get_course_snapshot") else "b" * 64
        return httpx.Response(200, json={"revision": revision, "payload": json.dumps(payload)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = MoodleBridge(configured_settings(), client)
        result = await bridge.discover_course("549", "7")

    assert result.preview["title"] == "C++"
    assert result.preview["groups"] == [{"external_id": "3", "name": "2.1"}]
    assert [call["wsfunction"][0] for call in calls] == [
        "local_programming_bridge_get_course_snapshot",
        "local_programming_bridge_get_membership_snapshot",
    ]
    assert all(call["wstoken"] == ["moodle-token"] for call in calls)


@pytest.mark.asyncio
async def test_moodle_course_url_is_exact_origin_and_route() -> None:
    settings = configured_settings()
    async with httpx.AsyncClient() as client:
        bridge = MoodleBridge(settings, client)
        assert (
            bridge.recognize_course_url("https://moodle.example.edu/course/view.php?id=549")
            == "549"
        )
        with pytest.raises(IntegrationProtocolError):
            bridge.recognize_course_url(
                "https://moodle.example.edu.evil.test/course/view.php?id=549"
            )
        with pytest.raises(IntegrationProtocolError):
            bridge.recognize_course_url("https://moodle.example.edu/user/profile.php?id=549")


@pytest.mark.asyncio
async def test_moodle_grade_and_checkpoint_contracts() -> None:
    functions: list[str] = []
    manifest = '[{"content":"int main(){}","path":"main.cpp"}]'
    manifest_hash = hashlib.sha256(manifest.encode()).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        function = form["wsfunction"][0]
        functions.append(function)
        if function.endswith("get_latest_checkpoint"):
            return httpx.Response(
                200,
                json={
                    "status": "FOUND",
                    "snapshotref": "snapshot-1",
                    "snapshotsha256": manifest_hash,
                    "eventchainhead": "",
                    "epoch": 1,
                    "workspacerevision": 0,
                    "manifestjson": manifest,
                },
            )
        return httpx.Response(200, json={"receiptid": len(functions)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = MoodleBridge(configured_settings(), client)
        await bridge.push_grade(
            {"course_id": "549", "cmid": 12, "user_id": "7", "grade": "9.5"},
            "grade-idempotency-1",
        )
        await bridge.store_checkpoint(
            {
                "course_id": "549",
                "user_id": "7",
                "attempt_ref": "attempt-1",
                "snapshot_ref": "snapshot-1",
                "snapshot_sha256": "a" * 64,
                "event_chain_head": "",
                "epoch": 1,
                "workspace_revision": 0,
                "reason": "CADENCE",
                "manifest_json": "[]",
            },
            "checkpoint-idempotency-1",
        )
        checkpoint = await bridge.get_latest_checkpoint(
            course_id="549", user_id="7", attempt_ref="attempt-1"
        )

    assert checkpoint == {
        "status": "FOUND",
        "snapshotref": "snapshot-1",
        "snapshotsha256": manifest_hash,
        "eventchainhead": "",
        "epoch": 1,
        "workspacerevision": 0,
        "manifestjson": manifest,
    }
    assert functions == [
        "local_programming_bridge_push_grade",
        "local_programming_bridge_store_checkpoint",
        "local_programming_bridge_get_latest_checkpoint",
    ]


@pytest.mark.asyncio
async def test_moodle_checkpoint_rejects_unverified_manifest() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "FOUND",
                "snapshotsha256": "0" * 64,
                "manifestjson": "[]",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationProtocolError):
            await MoodleBridge(configured_settings(), client).get_latest_checkpoint(
                course_id="549", user_id="7", attempt_ref="attempt-1"
            )


@pytest.mark.asyncio
async def test_moodle_task_definition_uses_narrow_idempotent_contract() -> None:
    captured: dict[str, list[str]] = {}
    definition = {
        "content_hash": "a" * 64,
        "schema_version": 1,
        "task_ref": "task-1",
        "version": 3,
    }
    definition_json = json.dumps(
        definition,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    definition_hash = hashlib.sha256(definition_json.encode()).hexdigest()

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(parse_qs(request.content.decode()))
        return httpx.Response(
            200,
            json={"status": "MIRRORED", "mirrorid": 91, "timecreated": 1, "timemodified": 1},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await MoodleBridge(configured_settings(), client).upsert_task_definition(
            {
                "course_id": "549",
                "task_ref": "task-1",
                "version": 3,
                "content_hash": "a" * 64,
                "definition_sha256": definition_hash,
                "definition_json": definition_json,
                "status": "PUBLISHED",
            },
            "task-idempotency-1",
        )

    assert result["receipt"]["mirrorid"] == 91
    assert captured == {
        "wstoken": ["moodle-token"],
        "moodlewsrestformat": ["json"],
        "wsfunction": ["local_programming_bridge_upsert_task_definition"],
        "courseid": ["549"],
        "taskref": ["task-1"],
        "versionnum": ["3"],
        "contenthash": ["a" * 64],
        "definitionhash": [definition_hash],
        "definitionjson": [definition_json],
        "status": ["PUBLISHED"],
        "idempotencykey": ["task-idempotency-1"],
    }


@pytest.mark.asyncio
async def test_runner_hmac_and_response_normalization() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "job_id": "job-1",
                "status": "SUCCESS",
                "compilation": {
                    "status": "SUCCEEDED",
                    "exit_code": 0,
                    "duration_ms": 12,
                    "stderr": {"text": "compiler note"},
                },
                "execution": {
                    "status": "SUCCEEDED",
                    "exit_code": 0,
                    "duration_ms": 20,
                    "stdout": {"text": "42\n"},
                    "stderr": {"text": ""},
                },
                "diagnostics": [
                    {
                        "file": "main.cpp",
                        "range": {"start_line": 2, "start_column": 3},
                        "severity": "WARNING",
                        "code": "unused",
                        "message": "unused value",
                        "related": [],
                        "fix_its": [],
                    }
                ],
                "profile": {"compiler_family": "gcc", "compiler_version": "14"},
                "isolation": {
                    "executor": "runner-v1",
                    "policy_version": "filesystem-only-v1",
                    "filesystem_isolated": True,
                    "network": "denied",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RunnerAdapter(
            configured_settings(),
            client,
            clock=lambda: 1_700_000_000,
            nonce_factory=lambda: "fixed-nonce",
        )
        result = await adapter.dispatch(
            request_id="run-1",
            profile_id="cpp-gcc-c++20-single",
            files=[{"path": "main.cpp", "content": "int main(){}"}],
            limits={"cpu_seconds": 3, "memory_mb": 192},
        )

    body = captured["body"]
    assert isinstance(body, bytes)
    assert json.loads(body)["limits"] == {"cpu_seconds": 3, "memory_mb": 192}
    canonical = f"1700000000\nfixed-nonce\n{hashlib.sha256(body).hexdigest()}".encode()
    expected = hmac.new(b"r" * 32, canonical, hashlib.sha256).hexdigest()
    headers = captured["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["x-runner-signature"] == f"v1={expected}"
    assert result.status == "COMPLETED"
    assert result.stdout == "42\n"
    assert result.stderr == "compiler note"
    assert result.diagnostics[0]["range"]["start_line"] == 2
    assert result.network_enabled is False
    assert result.filesystem_isolated is True


@pytest.mark.asyncio
async def test_runner_interactive_session_uses_signed_bounded_commands() -> None:
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        payload = json.loads(request.content)
        if request.url.path.endswith("/interactive-sessions"):
            assert payload["owner_key"] == "experiment-owner-00000001"
        elif request.url.path.endswith("/input"):
            assert payload == {
                "owner_key": "experiment-owner-00000001",
                "text": "42",
            }
        return httpx.Response(
            200,
            json={
                "session_id": "a" * 32,
                "status": "RUNNING" if not request.url.path.endswith("/stop") else "STOPPED",
                "terminal": request.url.path.endswith("/stop"),
                "exit_code": None,
                "duration_ms": 12,
                "stdout": "number> ",
                "stderr": "",
                "output_truncated": False,
                "input_closed": request.url.path.endswith("/eof"),
                "diagnostics": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RunnerAdapter(
            configured_settings(),
            client,
            clock=lambda: 1_700_000_000,
            nonce_factory=iter(["nonce-1", "nonce-2", "nonce-3", "nonce-4"]).__next__,
        )
        started = await adapter.start_interactive(
            request_id="interactive-1",
            owner_key="experiment-owner-00000001",
            profile_id="cpp-gcc-c++20-single",
            files=[{"path": "main.cpp", "content": "int main(){}"}],
            limits={"cpu_seconds": 2, "memory_mb": 256},
        )
        sent = await adapter.interactive_input(
            session_id=started["session_id"],
            owner_key="experiment-owner-00000001",
            text="42",
        )
        eof = await adapter.interactive_eof(
            session_id=started["session_id"],
            owner_key="experiment-owner-00000001",
        )
        stopped = await adapter.interactive_stop(
            session_id=started["session_id"],
            owner_key="experiment-owner-00000001",
        )

    assert sent["stdout"] == "number> "
    assert eof["input_closed"] is True
    assert stopped["status"] == "STOPPED"
    assert [request.url.path for request in calls] == [
        "/v1/interactive-sessions",
        f"/v1/interactive-sessions/{'a' * 32}/input",
        f"/v1/interactive-sessions/{'a' * 32}/eof",
        f"/v1/interactive-sessions/{'a' * 32}/stop",
    ]
    for request in calls:
        assert request.headers["x-runner-signature"].startswith("v1=")


@pytest.mark.asyncio
async def test_runner_preserves_unrestricted_container_policy() -> None:
    async with httpx.AsyncClient() as client:
        adapter = RunnerAdapter(configured_settings(), client)
        common = {
            "job_id": "job-policy",
            "status": "SUCCESS",
            "compilation": {"exit_code": 0, "duration_ms": 1, "stderr": {"text": ""}},
            "execution": {"exit_code": 0, "duration_ms": 1, "stdout": {"text": ""}},
            "diagnostics": [],
            "profile": {},
        }
        result = adapter.normalize(
            {
                **common,
                "isolation": {
                    "executor": "local",
                    "filesystem_isolated": False,
                    "network": "host",
                    "policy_version": "unrestricted-container-v1",
                },
            }
        )
        assert result.filesystem_isolated is False
        assert result.network_enabled is True
        assert result.filesystem_policy_version == "unrestricted-container-v1"

        with pytest.raises(IntegrationProtocolError, match="invalid shape"):
            adapter.normalize(
                {
                    **common,
                    "isolation": {
                        "filesystem_isolated": "false",
                        "network": "host",
                        "policy_version": "invalid",
                    },
                }
            )


@pytest.mark.asyncio
async def test_runner_normalizes_schema_hostile_diagnostics_before_storage() -> None:
    async with httpx.AsyncClient() as client:
        adapter = RunnerAdapter(configured_settings(), client)
        result = adapter.normalize(
            {
                "job_id": "j" * 500,
                "status": "COMPILE_ERROR",
                "compilation": {
                    "exit_code": 1,
                    "duration_ms": -10,
                    "stderr": {"text": "failed"},
                },
                "diagnostics": [
                    {
                        "file": None,
                        "range": {
                            "start_line": 0,
                            "start_column": -2,
                            "end_line": 1,
                            "end_column": 0,
                        },
                        "severity": "fatal",
                        "code": 42,
                        "message": "   ",
                        "notes": ["n", 10],
                        "related": ["invalid", {"message": "related"}],
                        "fix_its": [None, {"replacement": ";"}],
                    }
                ],
                "profile": {},
                "isolation": {
                    "filesystem_isolated": True,
                    "network": "denied",
                    "policy_version": "filesystem-only-v1",
                },
            }
        )

    diagnostic = result.diagnostics[0]
    assert result.status == "FAILED"
    assert len(result.external_job_id) == 255
    assert result.metrics["compilation_duration_ms"] is None
    assert diagnostic["severity"] == "error"
    assert diagnostic["message"]
    assert diagnostic["range"]["start_line"] is None
    assert diagnostic["code"] is None
    assert diagnostic["notes"] == ["n"]
    assert diagnostic["related"] == [{"message": "related"}]
    assert diagnostic["fix_its"] == [{"replacement": ";"}]


@pytest.mark.asyncio
async def test_runner_rejects_malformed_limits_before_external_call() -> None:
    called = False

    async def must_not_call(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(must_not_call)) as client:
        adapter = RunnerAdapter(configured_settings(), client)
        with pytest.raises(IntegrationProtocolError):
            await adapter.dispatch(
                request_id="bad-limits",
                profile_id="cpp",
                files=[{"path": "a.cpp", "content": "int main(){}"}],
                limits={"cpu_seconds": 1},
            )
    assert called is False


@pytest.mark.asyncio
async def test_runner_caps_response_and_maps_timeout() -> None:
    settings = configured_settings(runner_response_body_max_bytes=1024)

    async def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'{"value":"' + b"x" * 2000 + b'"}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(oversized)) as client:
        with pytest.raises(IntegrationResponseTooLarge):
            await RunnerAdapter(settings, client).dispatch(
                request_id="r1", profile_id="cpp", files=[{"path": "a.cpp", "content": ""}]
            )

    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret upstream detail", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(IntegrationTimeout) as caught:
            await RunnerAdapter(settings, client).dispatch(
                request_id="r2", profile_id="cpp", files=[{"path": "a.cpp", "content": ""}]
            )
    assert "secret upstream detail" not in str(caught.value)

    request_limited = configured_settings(runner_request_body_max_bytes=1024)
    called = False

    async def must_not_call(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(must_not_call)) as client:
        with pytest.raises(IntegrationProtocolError):
            await RunnerAdapter(request_limited, client).dispatch(
                request_id="r3",
                profile_id="cpp",
                files=[{"path": "a.cpp", "content": "x" * 2000}],
            )
    assert called is False


@pytest.mark.asyncio
async def test_student_input_gate_blocks_solution_without_external_call() -> None:
    called = False

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await AIProvider(configured_settings(), client).answer(
            mode="STUDENT", question="Реши задачу и напиши полный код", context={}
        )
    assert answer.safety_outcome == "BLOCKED_INPUT"
    assert called is False


@pytest.mark.asyncio
async def test_ai_bounds_history_and_filters_citations() -> None:
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "provider-model",
                "output_text": (
                    "Смотрите https://en.cppreference.com/w/cpp/container/vector и "
                    "https://evil.test/fake"
                ),
                "citations": [
                    {
                        "title": "vector",
                        "url": "https://en.cppreference.com/w/cpp/container/vector",
                    },
                    {"title": "untrusted", "url": "https://evil.test/fake"},
                ],
            },
        )

    settings = configured_settings(ai_history_messages=2)
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "middle"},
        {"role": "user", "content": "new"},
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await AIProvider(settings, client).answer(
            mode="TEACHER", question="Что проверить?", context={"revision": 3}, history=history
        )

    assert [item["content"] for item in captured["input"][:-1]] == ["middle", "new"]
    assert answer.model == "provider-model"
    assert answer.citations == [
        {"title": "vector", "url": "https://en.cppreference.com/w/cpp/container/vector"}
    ]


@pytest.mark.asyncio
async def test_student_answer_requires_allowlisted_documentation() -> None:
    responses = iter(
        [
            {"output_text": "Проверьте тип итератора."},
            {
                "output_text": (
                    "Проверьте требования к итератору: https://en.cppreference.com/w/cpp/iterator"
                )
            },
        ]
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=next(responses))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AIProvider(configured_settings(), client)
        with pytest.raises(IntegrationProtocolError):
            await provider.answer(mode="STUDENT", question="Что проверить?", context={})
        answer = await provider.answer(mode="STUDENT", question="Что проверить?", context={})
    assert answer.citations == [
        {"title": "cppreference", "url": "https://en.cppreference.com/w/cpp/iterator"}
    ]


@pytest.mark.asyncio
async def test_student_output_gate_replaces_complete_program() -> None:
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "output_text": (
                    "```cpp\n#include <iostream>\nint main() { int a = 1; int b = 2; "
                    "std::cout << a + b; return 0; }\n```"
                )
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await AIProvider(configured_settings(), client).answer(
            mode="STUDENT", question="Почему возникает ошибка?", context={}
        )
    assert answer.safety_outcome == "BLOCKED_SOLUTION"
    assert "#include" not in answer.content


@pytest.mark.asyncio
async def test_student_gate_blocks_inline_function_and_split_history() -> None:
    responses = iter(
        [
            "int solve(int x) { return x + 1; }",
            "return x + 1; }",
        ]
    )

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output_text": next(responses)})

    settings = configured_settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = AIProvider(settings, client)
        inline = await provider.answer(mode="STUDENT", question="Объясни подход", context={})
        split = await provider.answer(
            mode="STUDENT",
            question="А что дальше?",
            context={},
            history=[{"role": "assistant", "content": "int solve(int x) {"}],
        )
    assert inline.safety_outcome == "BLOCKED_SOLUTION"
    assert split.safety_outcome == "BLOCKED_SOLUTION"


@pytest.mark.asyncio
async def test_ai_chat_completions_contract() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/completions")
        payload = json.loads(request.content)
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][-1]["role"] == "user"
        return httpx.Response(
            200,
            json={
                "model": "chat-model",
                "choices": [{"message": {"content": "Проверьте граничные случаи."}}],
            },
        )

    settings = configured_settings(ai_api_style="chat_completions")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await AIProvider(settings, client).answer(
            mode="TEACHER", question="Что проверить?", context={}
        )
    assert answer.content == "Проверьте граничные случаи."
    assert answer.model == "chat-model"


@pytest.mark.asyncio
async def test_authorship_hmac_and_schema_validation() -> None:
    manifest = "a" * 64
    captured: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(
            200,
            json={
                "manifest_hash": manifest,
                "probability": 0.8,
                "confidence": 0.7,
                "uncertainty": 0.1,
                "analyzer": "history-analyzer",
                "model": "v2",
                "calibration": {"version": "2026-08"},
                "features": {"event_count": 20},
                "warnings": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthorshipTransport(
            configured_settings(),
            client,
            endpoint="https://analyzer.example.test/v1/analyze",
            shared_secret="shared-secret",
            clock=lambda: 1_700_000_000,
            nonce_factory=lambda: "fixed-nonce",
        )
        result = await transport.analyze(
            {"schema_version": "1.0", "submission": {"manifest_hash": manifest}},
            manifest_hash=manifest,
        )

    body = captured["body"]
    assert isinstance(body, bytes)
    canonical = f"1700000000\nfixed-nonce\n{hashlib.sha256(body).hexdigest()}".encode()
    expected = hmac.new(b"shared-secret", canonical, hashlib.sha256).hexdigest()
    headers = captured["headers"]
    assert isinstance(headers, httpx.Headers)
    assert headers["x-authorship-signature"] == f"v1={expected}"
    assert result.probability == Decimal("0.800000")
    assert result.response_hash


@pytest.mark.asyncio
async def test_authorship_bearer_and_manifest_mismatch_rejected() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer analyzer-token"
        return httpx.Response(
            200,
            json={
                "manifest_hash": "wrong",
                "probability": 0.5,
                "confidence": 0.5,
                "uncertainty": 0.5,
                "analyzer": "a",
                "model": "m",
                "calibration": {},
                "features": {},
                "warnings": [],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = AuthorshipTransport(
            configured_settings(),
            client,
            endpoint="https://analyzer.example.test/analyze",
            bearer_token="analyzer-token",
        )
        with pytest.raises(IntegrationProtocolError):
            await transport.analyze({}, manifest_hash="expected")


@pytest.mark.asyncio
async def test_authorship_requires_versioned_calibration() -> None:
    async with httpx.AsyncClient() as client:
        transport = AuthorshipTransport(
            configured_settings(),
            client,
            endpoint="https://analyzer.example.test/analyze",
            bearer_token="token",
        )
        with pytest.raises(IntegrationProtocolError):
            transport.validate(
                {
                    "manifest_hash": "a" * 64,
                    "probability": 0.5,
                    "confidence": 0.5,
                    "uncertainty": 0.5,
                    "analyzer": "a",
                    "model": "m",
                    "calibration": {},
                    "features": {},
                    "warnings": [],
                },
                manifest_hash="a" * 64,
            )
