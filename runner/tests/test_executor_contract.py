from __future__ import annotations

from pathlib import Path

from edu_runner.config import Settings
from edu_runner.executor import JobLayout, LocalExecutor, select_executor
from edu_runner.models import ExecutionRequest, JobStatus
from edu_runner.process import ProcessOutcome
from edu_runner.profiles import DEFAULT_RUN
from edu_runner.service import RunnerService

from conftest import require_clang


def test_local_executor_runs_directly_in_job_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    layout = JobLayout(
        root=tmp_path,
        source=tmp_path / "source",
        build=tmp_path / "build",
        compile_tmp=tmp_path / "compile-tmp",
        runtime_tmp=tmp_path / "runtime-tmp",
        runtime_output=tmp_path / "runtime-output",
    )
    for directory in (
        layout.source,
        layout.build,
        layout.compile_tmp,
        layout.runtime_tmp,
        layout.runtime_output,
    ):
        directory.mkdir()
    (layout.build / "program").write_bytes(b"program")
    captured: dict[str, object] = {}

    def fake_run_process(command, **kwargs):
        captured["command"] = list(command)
        captured.update(kwargs)
        return ProcessOutcome(
            exit_code=0,
            duration_ms=1,
            stdout=b"",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
        )

    monkeypatch.setattr("edu_runner.executor.run_process", fake_run_process)
    executor = LocalExecutor()
    executor.run(
        layout=layout,
        local_command=[str(layout.build / "program")],
        sandbox_command=["/ignored/sandbox/program"],
        stdin=b"",
        limits=DEFAULT_RUN,
    )
    command = captured["command"]
    assert isinstance(command, list)
    assert command == [str(layout.build / "program")]
    assert captured["cwd"] == layout.runtime_output
    assert captured["monitored_paths"] == (
        layout.runtime_output,
        layout.runtime_tmp,
    )
    assert executor.isolation.filesystem_isolated is False
    assert executor.isolation.network == "host"


def test_default_service_uses_local_executor_and_runs_program(tmp_path: Path) -> None:
    require_clang()
    settings = Settings(
        shared_secret=b"test-runner-secret-that-is-long-enough-0001",
        work_root=tmp_path / "jobs",
    )
    executor = select_executor(settings)
    assert isinstance(executor, LocalExecutor)
    service = RunnerService(settings, executor=executor)
    result = service.execute(
        ExecutionRequest(
            request_id="local-container-contract",
            profile_id="cpp-clang-c++20-single",
            files=[
                {
                    "path": "main.cpp",
                    "content": (
                        "#include <iostream>\n"
                        'int main(){std::cout << "ordinary subprocess";}\n'
                    ),
                }
            ],
        )
    )

    assert result.status == JobStatus.SUCCESS
    assert result.execution is not None
    assert result.execution.exit_code == 0
    assert result.execution.stdout.text == "ordinary subprocess"
    assert result.isolation.filesystem_isolated is False
    assert result.isolation.network == "host"
