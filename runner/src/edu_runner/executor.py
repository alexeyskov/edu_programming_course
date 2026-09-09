from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from .config import Settings
from .models import IsolationSummary
from .process import ProcessOutcome, run_process
from .profiles import ResourceLimits


CLEAN_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "HOME": "/nonexistent",
    "TMPDIR": "/tmp",
}


@dataclass(frozen=True, slots=True)
class JobLayout:
    root: Path
    source: Path
    build: Path
    compile_tmp: Path
    runtime_tmp: Path
    runtime_output: Path


class ExecutorUnavailable(RuntimeError):
    pass


class Executor:
    name = "local"
    available: bool = True
    unavailable_reason: str | None = None

    @property
    def isolation(self) -> IsolationSummary:
        raise NotImplementedError

    def compile(
        self,
        *,
        layout: JobLayout,
        local_command: Sequence[str],
        sandbox_command: Sequence[str],
        limits: ResourceLimits,
    ) -> ProcessOutcome:
        raise NotImplementedError

    def run(
        self,
        *,
        layout: JobLayout,
        local_command: Sequence[str],
        sandbox_command: Sequence[str],
        stdin: bytes,
        limits: ResourceLimits,
    ) -> ProcessOutcome:
        raise NotImplementedError


class LocalExecutor(Executor):
    name = "local"

    @property
    def isolation(self) -> IsolationSummary:
        return IsolationSummary(
            policy="UNRESTRICTED_CONTAINER",
            policy_version="unrestricted-container-v1",
            executor="local",
            filesystem_isolated=False,
            network="host",
            warning=(
                "UNRESTRICTED EXECUTION: the program is an ordinary subprocess "
                "inside the runner container; only deployment/container and resource "
                "limits remain"
            ),
        )

    def compile(
        self,
        *,
        layout: JobLayout,
        local_command: Sequence[str],
        sandbox_command: Sequence[str],
        limits: ResourceLimits,
    ) -> ProcessOutcome:
        del sandbox_command
        return run_process(
            local_command,
            cwd=layout.build,
            environment=CLEAN_ENVIRONMENT,
            stdin=b"",
            limits=limits,
            monitored_paths=(layout.build, layout.compile_tmp),
        )

    def run(
        self,
        *,
        layout: JobLayout,
        local_command: Sequence[str],
        sandbox_command: Sequence[str],
        stdin: bytes,
        limits: ResourceLimits,
    ) -> ProcessOutcome:
        del sandbox_command
        return run_process(
            local_command,
            cwd=layout.runtime_output,
            environment=CLEAN_ENVIRONMENT,
            stdin=stdin,
            limits=limits,
            monitored_paths=(layout.runtime_output, layout.runtime_tmp),
        )


def select_executor(settings: Settings) -> Executor:
    # Settings validates the single supported mode.  Keeping selection as a
    # function preserves the service construction seam used by tests.
    del settings
    return LocalExecutor()
