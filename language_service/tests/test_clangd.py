from __future__ import annotations

import asyncio
import shutil

import pytest

from edu_language.config import Settings
from edu_language.models import Query
from edu_language.service import Busy, LanguageService, QueryTimeout, Session


CLANGD = shutil.which("clangd")


def query(code, method="completion", *, character=21, workspace="a", files=None):
    return Query(
        workspace_id=workspace * 64,
        build_profile="cpp-gcc-c++20-single",
        files=files or [{"path": "main.cpp", "content": code}],
        method=method,
        path="main.cpp",
        position={"line": 1, "character": character}
        if method
        in {"completion", "hover", "signatureHelp", "definition", "references"}
        else None,
    )


def make_service(tmp_path, **kwargs):
    return LanguageService(
        Settings(
            shared_secret=b"s" * 32,
            work_root=tmp_path,
            clangd=CLANGD or "/missing",
            test_without_sandbox=True,
            **kwargs,
        )
    )


@pytest.mark.skipif(not CLANGD, reason="clangd is not installed")
@pytest.mark.asyncio
async def test_real_members_hover_and_updated_snapshot(tmp_path):
    service = make_service(tmp_path)
    await service.start()
    try:
        code = "struct Foo { int bar; void method(); };\nint main(){ Foo f; f. }\n"
        result = await service.query(query(code))
        items = result["result"]["items"]
        assert {item["filterText"] for item in items} >= {"bar", "method"}
        assert (
            next(item for item in items if item["filterText"] == "method")["kind"] == 2
        )
        original = next(iter(service.sessions.values()))
        changed = code.replace("bar", "updated")
        result = await service.query(query(changed))
        assert "updated" in {item["filterText"] for item in result["result"]["items"]}
        assert "bar" not in {item["filterText"] for item in result["result"]["items"]}
        assert next(iter(service.sessions.values())) is original
        result = await service.query(
            query(
                "int value = 1;\nint main(){ return value; }\n", "hover", character=20
            )
        )
        assert "int" in str(result["result"])
    finally:
        await service.close()
    assert not list(tmp_path.iterdir())


@pytest.mark.skipif(not CLANGD, reason="clangd is not installed")
@pytest.mark.asyncio
async def test_real_header_completion_definition_diagnostics_formatting(tmp_path):
    service = make_service(tmp_path)
    await service.start()
    try:
        files = [
            {
                "path": "main.cpp",
                "content": '#include "helper.hpp"\nint main(){ return answer(); }\n',
            },
            {"path": "helper.hpp", "content": "inline int answer() { return 42; }\n"},
        ]
        result = await service.query(query("", "definition", character=21, files=files))
        assert "helper.hpp" in str(result["result"])
        assert "file://" not in str(result)
        diagnostics = await service.query(query("int bad = ;\n", "diagnostics"))
        assert any(item["diagnostics"] for item in diagnostics["diagnostics"])
        formatted = await service.query(query("int main(){return 0;}\n", "formatting"))
        assert isinstance(formatted["result"], list)
        files[0]["content"] = '#include "hel\nint main() {}\n'
        payload = Query(
            workspace_id="a" * 64,
            build_profile="cpp-gcc-c++20-single",
            files=files,
            method="completion",
            path="main.cpp",
            position={"line": 0, "character": 13},
        )
        result = await service.query(payload)
        assert "helper.hpp" in str(result["result"])
    finally:
        await service.close()


@pytest.mark.skipif(not CLANGD, reason="clangd is not installed")
@pytest.mark.asyncio
async def test_real_signature_help(tmp_path):
    service = make_service(tmp_path)
    await service.start()
    try:
        code = "int add(int left, int right);\nint main(){ add(1, ); }\n"
        result = await service.query(query(code, "signatureHelp", character=18))
        assert result["result"]["signatures"]
        assert "right" in str(result["result"])
    finally:
        await service.close()


@pytest.mark.skipif(not CLANGD, reason="clangd is not installed")
@pytest.mark.asyncio
async def test_real_references_symbols_and_c_profile(tmp_path):
    service = make_service(tmp_path)
    await service.start()
    try:
        code = "int value = 1;\nint main(){ return value; }\n"
        references = await service.query(query(code, "references", character=20))
        assert len(references["result"]) >= 2
        assert all(item["uri"] == "main.cpp" for item in references["result"])
        symbols = await service.query(query(code, "documentSymbol"))
        assert {item["name"] for item in symbols["result"]} >= {"value", "main"}
        request = Query(
            workspace_id="c" * 64,
            build_profile="c-gcc-c17-single",
            files=[
                {"path": "main.c", "content": "int main(void) { return missing; }\n"}
            ],
            method="diagnostics",
            path="main.c",
        )
        result = await service.query(request)
        assert any(item["diagnostics"] for item in result["diagnostics"])
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_pool_lru_eviction_and_busy_capacity(tmp_path, monkeypatch):
    async def fake_start(self):
        pass

    async def fake_query(self, payload):
        return {"result": None, "diagnostics": []}

    monkeypatch.setattr(Session, "start", fake_start)
    monkeypatch.setattr(Session, "query", fake_query)
    service = make_service(tmp_path, max_sessions=1)
    service.ready = True
    code = "int main(){}\n"
    await service.query(query(code, "diagnostics"))
    original = next(iter(service.sessions.values()))
    await original.lock.acquire()
    original.users = 4
    try:
        with pytest.raises(Busy):
            await service.query(query(code, "diagnostics", workspace="b"))
        with pytest.raises(Busy):
            await service.query(query(code, "diagnostics"))
    finally:
        original.users = 0
        original.lock.release()
    await service.query(query(code, "diagnostics", workspace="b"))
    assert original.closed and not original.root.exists()
    assert len(service.sessions) == 1
    await service.close()


@pytest.mark.asyncio
async def test_timeout_includes_initialization_and_cleans_workspace(
    tmp_path, monkeypatch
):
    async def slow_start(self):
        await asyncio.sleep(10)

    monkeypatch.setattr(Session, "start", slow_start)
    service = make_service(tmp_path, request_seconds=1)
    service.ready = True
    with pytest.raises(QueryTimeout):
        await service.query(query("int main(){}\n", "diagnostics"))
    assert not service.sessions
    assert not list(tmp_path.iterdir())
    await service.close()


@pytest.mark.asyncio
async def test_cancellation_cleans_workspace(tmp_path, monkeypatch):
    started = asyncio.Event()

    async def slow_start(self):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(Session, "start", slow_start)
    service = make_service(tmp_path)
    service.ready = True
    request = asyncio.create_task(service.query(query("int main(){}\n", "diagnostics")))
    await started.wait()
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not service.sessions and not list(tmp_path.iterdir())
    await service.close()


@pytest.mark.asyncio
async def test_missing_sandbox_fails_closed(tmp_path):
    service = LanguageService(
        Settings(
            shared_secret=b"s" * 32,
            work_root=tmp_path,
            sandbox_launcher="/not-installed",
        )
    )
    await service.start()
    assert not service.ready
    await service.close()


@pytest.mark.parametrize("mode", ["container", "landlock"])
@pytest.mark.asyncio
async def test_readiness_probes_requested_mode_without_fallback(
    tmp_path, monkeypatch, mode
):
    launches = []

    class FailedProbe:
        async def wait(self):
            return 125

    async def launch(*command, **kwargs):
        launches.append(command)
        assert kwargs["env"] == {"PATH": "/usr/bin:/bin"}
        return FailedProbe()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
    settings = Settings(shared_secret=b"s" * 32, work_root=tmp_path, isolation=mode)
    service = LanguageService(settings)
    await service.start()
    try:
        assert not service.ready
        assert launches == [(settings.sandbox_launcher, "--isolation", mode, "--probe")]
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_same_workspace_requests_serialize_without_eviction(
    tmp_path, monkeypatch
):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = []

    async def fake_start(self):
        pass

    async def fake_query(self, payload):
        calls.append(payload.files[0].content)
        if len(calls) == 1:
            started.set()
            await finish.wait()
        return {"result": None, "diagnostics": []}

    monkeypatch.setattr(Session, "start", fake_start)
    monkeypatch.setattr(Session, "query", fake_query)
    service = make_service(tmp_path)
    service.ready = True
    first = asyncio.create_task(service.query(query("int first;\n", "diagnostics")))
    await started.wait()
    second = asyncio.create_task(service.query(query("int second;\n", "diagnostics")))
    await asyncio.sleep(0)
    assert calls == ["int first;\n"]
    assert next(iter(service.sessions.values())).users == 2
    finish.set()
    await asyncio.gather(first, second)
    assert calls == ["int first;\n", "int second;\n"]
    assert next(iter(service.sessions.values())).users == 0
    await service.close()
