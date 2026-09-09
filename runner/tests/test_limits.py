from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from edu_runner.config import Settings
from edu_runner.executor import LocalExecutor
from edu_runner.models import ExecutionLimits, ExecutionRequest, JobStatus
from edu_runner.profiles import DEFAULT_PROFILES, ResourceLimits
from edu_runner.service import RunnerService

from conftest import require_clang


def _limited_service(tmp_path: Path, *, wall_seconds: float) -> RunnerService:
    require_clang()
    base = DEFAULT_PROFILES["cpp-clang-c++20-single"]
    run_limits = ResourceLimits(
        wall_seconds=wall_seconds,
        cpu_seconds=1,
        memory_bytes=256 * 1024 * 1024,
        process_count=16,
        output_bytes=1024,
        file_bytes=1024 * 1024,
    )
    profile = replace(base, id="test-limited", run_limits=run_limits)
    settings = Settings(
        shared_secret=b"test-runner-secret-that-is-long-enough-0001",
        work_root=tmp_path / "jobs",
    )
    return RunnerService(
        settings, profiles={profile.id: profile}, executor=LocalExecutor()
    )


@pytest.fixture
def limited_service(tmp_path: Path) -> RunnerService:
    return _limited_service(tmp_path, wall_seconds=0.35)


@pytest.fixture
def output_limited_service(tmp_path: Path) -> RunnerService:
    # This test isolates the output limit from the wall limit.  Starting the
    # Python rlimit trampoline can consume a meaningful part of 350 ms on a
    # loaded CI host; in that case TIME_LIMIT is the correct production result
    # even though the source would eventually print forever.
    return _limited_service(tmp_path, wall_seconds=2.0)


def _request(code: str) -> ExecutionRequest:
    return ExecutionRequest(
        request_id="limits",
        profile_id="test-limited",
        files=[{"path": "main.cpp", "content": code}],
    )


def test_dynamic_limits_only_reduce_profile_run_limits() -> None:
    profile_limits = DEFAULT_PROFILES["cpp-clang-c++20-single"].run_limits

    reduced = RunnerService._effective_run_limits(
        profile_limits,
        ExecutionLimits(cpu_seconds=1, memory_mb=64),
    )
    assert reduced.cpu_seconds == 1
    assert reduced.memory_bytes == 64 * 1024 * 1024
    assert reduced.wall_seconds == profile_limits.wall_seconds
    assert reduced.process_count == profile_limits.process_count
    assert reduced.output_bytes == profile_limits.output_bytes
    assert reduced.file_bytes == profile_limits.file_bytes
    assert reduced.open_files == profile_limits.open_files

    requested_increase = RunnerService._effective_run_limits(
        profile_limits,
        ExecutionLimits(cpu_seconds=300, memory_mb=65_536),
    )
    assert requested_increase == profile_limits


def test_wall_timeout_kills_process_tree(limited_service: RunnerService) -> None:
    result = limited_service.execute(_request("int main(){for(;;){} }"))
    assert result.status == JobStatus.TIME_LIMIT
    assert result.execution is not None
    assert result.execution.status == "TIME_LIMIT"


def test_output_is_bounded_and_process_is_killed(
    output_limited_service: RunnerService,
) -> None:
    code = (
        "#include <unistd.h>\n"
        "int main(){static const char x[16384]{}; "
        "for(;;){write(1,x,sizeof(x));}}"
    )
    result = output_limited_service.execute(_request(code))
    assert result.status == JobStatus.OUTPUT_LIMIT
    assert result.execution is not None
    assert result.execution.stdout.bytes_captured <= 1024
    assert result.execution.stdout.truncated is True
