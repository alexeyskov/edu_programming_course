from __future__ import annotations

import math
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .profiles import ResourceLimits


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    exit_code: int | None
    duration_ms: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool = False
    output_limited: bool = False
    workspace_limited: bool = False
    launch_error: str | None = None


class _OutputBudget:
    def __init__(self, limit: int) -> None:
        self._remaining = limit
        self._lock = threading.Lock()
        self.exceeded = threading.Event()

    def take(self, data: bytes) -> tuple[bytes, bool]:
        with self._lock:
            accepted = data[: self._remaining]
            self._remaining -= len(accepted)
            truncated = len(accepted) != len(data)
            if truncated:
                self.exceeded.set()
            return accepted, truncated


class _StreamCapture:
    def __init__(self, budget: _OutputBudget) -> None:
        self.data = bytearray()
        self.truncated = False
        self._budget = budget

    def read(self, stream: object) -> None:
        reader = stream
        try:
            while True:
                # ``BufferedReader.read(n)`` may wait until all ``n`` bytes are
                # available.  On platforms with an 8 KiB pipe that can deadlock
                # a writer while we request 16 KiB, so neither live output nor
                # the output budget advances.  ``read1`` consumes whatever the
                # pipe currently has and is also what an interactive terminal
                # needs for prompt-sized output.
                read1 = getattr(reader, "read1", None)
                chunk = (
                    read1(16 * 1024) if callable(read1) else reader.read(16 * 1024)  # type: ignore[attr-defined]
                )
                if not chunk:
                    return
                accepted, truncated = self._budget.take(chunk)
                self.data.extend(accepted)
                self.truncated = self.truncated or truncated
        except (BrokenPipeError, OSError, ValueError):
            return
        finally:
            try:
                reader.close()  # type: ignore[attr-defined]
            except (OSError, ValueError):
                pass


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()


def _limited_command(command: Sequence[str], limits: ResourceLimits) -> list[str]:
    limiter = str(Path(__file__).with_name("_limit_exec.py"))
    return [
        sys.executable,
        limiter,
        str(limits.cpu_seconds),
        str(limits.memory_bytes),
        str(limits.process_count),
        str(limits.file_bytes),
        str(limits.open_files),
        "--",
        *command,
    ]


def _workspace_usage(paths: Sequence[Path], limit: int) -> int:
    total = 0
    for root in paths:
        for current, directories, files in os.walk(root, followlinks=False):
            # Symlinked directories must not be traversed by a usage monitor.
            directories[:] = [
                name for name in directories if not Path(current, name).is_symlink()
            ]
            for name in files:
                try:
                    total += Path(current, name).lstat().st_size
                except FileNotFoundError:
                    continue
                if total > limit:
                    return total
    return total


def run_process(
    command: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None,
    environment: Mapping[str, str],
    stdin: bytes,
    limits: ResourceLimits,
    monitored_paths: Sequence[Path] = (),
) -> ProcessOutcome:
    started = time.monotonic()
    wrapped_command = _limited_command(command, limits)
    try:
        process = subprocess.Popen(
            wrapped_command,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProcessOutcome(
            exit_code=None,
            duration_ms=max(0, math.ceil((time.monotonic() - started) * 1000)),
            stdout=b"",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            launch_error=f"{type(exc).__name__}: {exc}",
        )

    assert process.stdout is not None
    assert process.stderr is not None
    budget = _OutputBudget(limits.output_bytes)
    stdout = _StreamCapture(budget)
    stderr = _StreamCapture(budget)
    readers = [
        threading.Thread(target=stdout.read, args=(process.stdout,), daemon=True),
        threading.Thread(target=stderr.read, args=(process.stderr,), daemon=True),
    ]
    for thread in readers:
        thread.start()

    def write_input() -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(stdin)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError):
            pass
        finally:
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError, ValueError):
                pass

    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    deadline = started + limits.wall_seconds
    timed_out = False
    output_limited = False
    workspace_limited = False
    next_workspace_check = started
    while process.poll() is None:
        if budget.exceeded.is_set():
            output_limited = True
            _kill_process_tree(process)
            break
        if monitored_paths and time.monotonic() >= next_workspace_check:
            if _workspace_usage(monitored_paths, limits.file_bytes) > limits.file_bytes:
                workspace_limited = True
                _kill_process_tree(process)
                break
            next_workspace_check = time.monotonic() + 0.05
        if time.monotonic() >= deadline:
            timed_out = True
            _kill_process_tree(process)
            break
        time.sleep(0.01)

    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        _kill_process_tree(process)
        process.wait(timeout=1)
    # A submitted process may exit after forking a child. Kill the process group
    # even after the group leader has returned so that no orphan survives the job.
    _kill_process_tree(process)
    writer.join(timeout=0.2)
    for thread in readers:
        thread.join(timeout=1)
    # A reader can observe the final oversized chunk while the monitor is
    # concurrently enforcing the wall deadline.  Preserve the more specific
    # output-limit evidence instead of making the classification scheduler-dependent.
    output_limited = output_limited or budget.exceeded.is_set()
    elapsed = max(0, math.ceil((time.monotonic() - started) * 1000))
    return ProcessOutcome(
        exit_code=process.returncode,
        duration_ms=elapsed,
        stdout=bytes(stdout.data),
        stderr=bytes(stderr.data),
        stdout_truncated=stdout.truncated,
        stderr_truncated=stderr.truncated,
        timed_out=timed_out,
        output_limited=output_limited,
        workspace_limited=workspace_limited,
    )


class InteractiveProcess:
    """A bounded subprocess whose stdin stays open between HTTP requests."""

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: str | os.PathLike[str] | None,
        environment: Mapping[str, str],
        limits: ResourceLimits,
        monitored_paths: Sequence[Path] = (),
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self.started = time.monotonic()
        self.limits = limits
        self.monitored_paths = tuple(monitored_paths)
        self._lock = threading.Lock()
        self._input_lock = threading.Lock()
        self._input_closed = False
        self._budget = _OutputBudget(limits.output_bytes)
        self._stdout = _StreamCapture(self._budget)
        self._stderr = _StreamCapture(self._budget)
        self._finished = threading.Event()
        self._stopped = False
        self._timed_out = False
        self._output_limited = False
        self._workspace_limited = False
        self._launch_error: str | None = None
        self._ended: float | None = None
        self._on_finish = on_finish
        try:
            self.process = subprocess.Popen(
                _limited_command(command, limits),
                cwd=cwd,
                env=dict(environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self.process = None
            self._launch_error = f"{type(exc).__name__}: {exc}"
            self._ended = time.monotonic()
            self._finished.set()
            if self._on_finish is not None:
                self._on_finish()
            return

        assert self.process.stdout is not None
        assert self.process.stderr is not None
        self._readers = [
            threading.Thread(
                target=self._stdout.read, args=(self.process.stdout,), daemon=True
            ),
            threading.Thread(
                target=self._stderr.read, args=(self.process.stderr,), daemon=True
            ),
        ]
        for thread in self._readers:
            thread.start()
        self._monitor = threading.Thread(target=self._watch, daemon=True)
        self._monitor.start()

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    def send_line(self, text: str) -> bool:
        data = f"{text}\n".encode("utf-8")
        with self._input_lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.write(data)
                process.stdin.flush()
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def close_input(self) -> bool:
        """Deliver EOF without terminating the process itself.

        Closing the pipe is deliberately separate from ``stop``: programs that
        read until EOF must be able to finish normally and flush their output.
        The input lock serializes EOF with concurrent line submissions.
        """

        with self._input_lock:
            process = self.process
            if process is None or process.poll() is not None or process.stdin is None:
                return False
            try:
                process.stdin.close()
                self._input_closed = True
                return True
            except (BrokenPipeError, OSError, ValueError):
                return False

    def stop(self) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        with self._lock:
            self._stopped = True
        _kill_process_tree(process)

    def wait(self, timeout: float | None = None) -> bool:
        return self._finished.wait(timeout)

    def snapshot(self) -> ProcessOutcome:
        process = self.process
        with self._lock:
            observed_at = self._ended or time.monotonic()
            return ProcessOutcome(
                exit_code=process.returncode if process is not None else None,
                duration_ms=max(0, math.ceil((observed_at - self.started) * 1000)),
                stdout=bytes(self._stdout.data),
                stderr=bytes(self._stderr.data),
                stdout_truncated=self._stdout.truncated,
                stderr_truncated=self._stderr.truncated,
                timed_out=self._timed_out,
                output_limited=self._output_limited or self._budget.exceeded.is_set(),
                workspace_limited=self._workspace_limited,
                launch_error=self._launch_error,
            )

    @property
    def stopped_by_user(self) -> bool:
        with self._lock:
            return self._stopped

    @property
    def input_closed(self) -> bool:
        with self._input_lock:
            return self._input_closed

    def _watch(self) -> None:
        process = self.process
        assert process is not None
        deadline = self.started + self.limits.wall_seconds
        next_workspace_check = self.started
        try:
            while process.poll() is None:
                if self._budget.exceeded.is_set():
                    with self._lock:
                        self._output_limited = True
                    _kill_process_tree(process)
                    break
                if self.monitored_paths and time.monotonic() >= next_workspace_check:
                    if (
                        _workspace_usage(self.monitored_paths, self.limits.file_bytes)
                        > self.limits.file_bytes
                    ):
                        with self._lock:
                            self._workspace_limited = True
                        _kill_process_tree(process)
                        break
                    next_workspace_check = time.monotonic() + 0.05
                if time.monotonic() >= deadline:
                    with self._lock:
                        self._timed_out = True
                    _kill_process_tree(process)
                    break
                time.sleep(0.01)
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process)
                process.wait(timeout=1)
            _kill_process_tree(process)
            with self._input_lock:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                        self._input_closed = True
                    except (BrokenPipeError, OSError, ValueError):
                        pass
            for thread in self._readers:
                thread.join(timeout=1)
            with self._lock:
                self._output_limited = (
                    self._output_limited or self._budget.exceeded.is_set()
                )
        finally:
            with self._lock:
                self._ended = time.monotonic()
            self._finished.set()
            if self._on_finish is not None:
                self._on_finish()
