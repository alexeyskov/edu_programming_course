from __future__ import annotations

import os
import signal
import sys
import time

import pytest

from edu_runner.process import InteractiveProcess
from edu_runner.profiles import ResourceLimits


@pytest.mark.parametrize("error_type", [PermissionError, RuntimeError])
def test_unexpected_monitor_error_reaps_process_and_closes_pipes_before_callback(
    tmp_path,
    monkeypatch,
    error_type,
):
    ready = tmp_path / "child-ready"
    calls = []

    def failing_monitor(*_args):
        # Wait for an actual descendant sharing stdout/stderr; killing only
        # the leader would leave these pipes open and the readers blocked.
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.005)
        raise error_type("A private workspace path must not leak")

    monkeypatch.setattr("edu_runner.process._workspace_usage", failing_monitor)
    # No compiler/container dependency; launch only these two test Python
    # processes and exercise the real OS process-group and pipe cleanup.
    monkeypatch.setattr(
        "edu_runner.process._limited_command", lambda command, limits: command
    )
    process = InteractiveProcess(
        [
            sys.executable,
            "-c",
            (
                "import pathlib, subprocess, sys, time; "
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
            ),
            str(ready),
        ],
        cwd=tmp_path,
        environment=os.environ.copy(),
        limits=ResourceLimits(
            wall_seconds=5,
            cpu_seconds=5,
            memory_bytes=512 * 1024 * 1024,
            process_count=8,
            output_bytes=1024,
            file_bytes=1024,
        ),
        monitored_paths=(tmp_path,),
        on_finish=lambda: calls.append("finished"),
    )
    try:
        assert process.wait(timeout=5)
        process._monitor.join(timeout=1)
        assert ready.exists(), "Fixture child was not started"
        assert process.process.poll() is not None
        assert process.process.stdin.closed and process.input_closed
        assert process.process.stdout.closed and process.process.stderr.closed
        assert all(not reader.is_alive() for reader in process._readers)
        assert process._pending_input == b""
        assert (
            process.snapshot().launch_error
            == f"Interactive monitor failed: {error_type.__name__}"
        )
        assert calls == ["finished"]
    finally:
        # Also reap the owned process if any assertion detects a regression.
        try:
            os.killpg(process.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.process.wait(timeout=2)
