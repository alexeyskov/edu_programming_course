from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Mapping

from .config import Settings
from .diagnostics import PathMapper, parse_compiler_diagnostics
from .executor import Executor, ExecutorUnavailable, JobLayout, select_executor
from .models import (
    Action,
    BoundedText,
    ExecutionRequest,
    ExecutionResponse,
    ExecutionLimits,
    JobStatus,
    InteractiveSessionCreateRequest,
    InteractiveSessionResponse,
    PhaseResult,
    PhaseStatus,
    ProfileResponse,
    ProfileSummary,
)
from .paths import normalize_relative_path, write_regular_file
from .process import InteractiveProcess, ProcessOutcome
from .profiles import BuildProfile, DEFAULT_PROFILES, ResourceLimits


class InvalidManifestError(ValueError):
    pass


class ProfileUnavailableError(RuntimeError):
    pass


class RunnerBusyError(RuntimeError):
    pass


class InteractiveSessionNotFoundError(LookupError):
    pass


class InteractiveSessionStateError(RuntimeError):
    pass


@dataclass(slots=True)
class _InteractiveSession:
    id: str
    owner_key: str
    layout: JobLayout | None
    process: InteractiveProcess | None
    mapper: PathMapper
    compilation: PhaseResult
    diagnostics: list
    compile_status: JobStatus
    created_at: float
    terminal_at: float | None = None


class RunnerService:
    def __init__(
        self,
        settings: Settings,
        *,
        profiles: Mapping[str, BuildProfile] = DEFAULT_PROFILES,
        executor: Executor | None = None,
    ) -> None:
        self.settings = settings
        self.profiles = dict(profiles)
        self.executor = executor or select_executor(settings)
        self._slots = threading.BoundedSemaphore(settings.max_concurrent_jobs)
        self._compiler_paths: dict[str, str | None] = {}
        self._compiler_versions: dict[str, str | None] = {}
        self._interactive_lock = threading.RLock()
        self._interactive_sessions: dict[str, _InteractiveSession] = {}
        self._prepare_work_root()
        for profile in self.profiles.values():
            name = profile.compiler_name
            if name not in self._compiler_paths:
                executable = shutil.which(name)
                self._compiler_paths[name] = executable
                self._compiler_versions[name] = self._read_compiler_version(executable)

    def _prepare_work_root(self) -> None:
        root = self.settings.work_root
        if root.exists() and root.is_symlink():
            raise RuntimeError("RUNNER_WORK_ROOT must not be a symlink")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        if not root.is_dir():
            raise RuntimeError("RUNNER_WORK_ROOT is not a directory")

    @staticmethod
    def _read_compiler_version(executable: str | None) -> str | None:
        if executable is None:
            return None
        try:
            completed = subprocess.run(
                [executable, "--version"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=2,
                check=False,
                env={
                    "PATH": "/usr/local/bin:/usr/bin:/bin",
                    "LANG": "C",
                    "HOME": "/tmp",
                    "TMPDIR": "/tmp",
                },
            )
        except (OSError, subprocess.SubprocessError):
            return None
        lines = completed.stdout.decode("utf-8", errors="replace").splitlines()
        version_line = next(
            (line for line in lines if "version" in line.lower()),
            lines[0] if lines else None,
        )
        return version_line[:200] if version_line else None

    def readiness(self) -> dict[str, object]:
        available_profiles = [
            item.id for item in self.list_profiles() if item.available
        ]
        return {
            "ready": self.executor.available and bool(available_profiles),
            "executor": self.executor.name
            if self.executor.available
            else "unavailable",
            "executor_error": self.executor.unavailable_reason,
            "available_profiles": available_profiles,
            "policy": "UNRESTRICTED_CONTAINER",
            "policy_version": "unrestricted-container-v1",
            "warning": (
                "Student programs run as ordinary subprocesses inside the runner "
                "container without per-job filesystem or network isolation."
            ),
        }

    def _profile_is_available(self, profile: BuildProfile) -> bool:
        executable = self._compiler_paths.get(profile.compiler_name)
        version = (self._compiler_versions.get(profile.compiler_name) or "").lower()
        if executable is None:
            return False
        if profile.compiler_family == "clang":
            return "clang" in version
        return "clang" not in version

    def list_profiles(self) -> list[ProfileResponse]:
        return [
            ProfileResponse(
                id=profile.id,
                language=profile.language,
                compiler_family=profile.compiler_family,
                standard=profile.standard,
                mode=profile.mode,
                available=self._profile_is_available(profile),
            )
            for profile in sorted(self.profiles.values(), key=lambda item: item.id)
        ]

    def _validate_request(
        self, request: ExecutionRequest
    ) -> tuple[BuildProfile, list[str], list[str]]:
        profile = self.profiles.get(request.profile_id)
        if profile is None:
            raise InvalidManifestError("unknown or unapproved profile_id")
        total_bytes = sum(len(item.content.encode("utf-8")) for item in request.files)
        if total_bytes > profile.max_source_bytes:
            raise InvalidManifestError("profile source byte limit exceeded")
        if len(request.stdin.encode("utf-8")) > profile.max_stdin_bytes:
            raise InvalidManifestError("profile stdin byte limit exceeded")

        allowed_headers = {".h", ".hh", ".hpp", ".hxx", ".inc"}
        source_paths: list[str] = []
        runtime_data_paths: list[str] = []
        compilation_file_count = 0
        for item in request.files:
            path = normalize_relative_path(item.path)
            suffix = Path(path.name).suffix.lower()
            if suffix in profile.source_extensions:
                source_paths.append(str(path))
                compilation_file_count += 1
            elif suffix in allowed_headers:
                compilation_file_count += 1
            elif suffix == ".txt":
                runtime_data_paths.append(str(path))
            else:
                raise InvalidManifestError(
                    f"file type {suffix or '<none>'} is not allowed by this profile"
                )
        if compilation_file_count > profile.max_file_count:
            raise InvalidManifestError("profile file count limit exceeded")
        if not source_paths:
            raise InvalidManifestError("manifest contains no compilation source")
        if profile.mode == "single" and len(source_paths) != 1:
            raise InvalidManifestError(
                "single profile requires exactly one compilation source"
            )
        compiler = self._compiler_paths.get(profile.compiler_name)
        if compiler is None or not self._profile_is_available(profile):
            raise ProfileUnavailableError(
                f"approved compiler {profile.compiler_name!r} is not installed or has "
                "an incompatible compiler family"
            )
        if not self.executor.available:
            raise ExecutorUnavailable(
                self.executor.unavailable_reason or "configured executor is unavailable"
            )
        return profile, sorted(source_paths), sorted(runtime_data_paths)

    @staticmethod
    def _manifest_hash(request: ExecutionRequest) -> str:
        manifest = {
            "profile_id": request.profile_id,
            "action": request.action.value,
            "files": [
                {
                    "path": item.path,
                    "sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                    "bytes": len(item.content.encode("utf-8")),
                }
                for item in sorted(request.files, key=lambda value: value.path)
            ],
            "stdin_sha256": hashlib.sha256(request.stdin.encode("utf-8")).hexdigest(),
            "limits": request.limits.model_dump()
            if request.limits is not None
            else None,
        }
        encoded = json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _effective_run_limits(
        profile_limits: ResourceLimits,
        requested: ExecutionLimits | None,
    ) -> ResourceLimits:
        if requested is None:
            return profile_limits
        return replace(
            profile_limits,
            cpu_seconds=min(profile_limits.cpu_seconds, requested.cpu_seconds),
            memory_bytes=min(
                profile_limits.memory_bytes,
                requested.memory_mb * 1024 * 1024,
            ),
        )

    def _create_layout(self, job_id: str) -> JobLayout:
        root = Path(
            tempfile.mkdtemp(prefix=f"job-{job_id}-", dir=self.settings.work_root)
        )
        os.chmod(root, 0o700)
        directories = {
            "source": root / "source",
            "build": root / "build",
            "compile_tmp": root / "compile-tmp",
            "runtime_tmp": root / "runtime-tmp",
            "runtime_output": root / "runtime-output",
        }
        for directory in directories.values():
            directory.mkdir(mode=0o700)
        return JobLayout(root=root, **directories)

    @staticmethod
    def _stage_runtime_binary(layout: JobLayout, compiled_binary: Path) -> Path:
        runtime_binary = layout.runtime_output / "program"
        shutil.copyfile(compiled_binary, runtime_binary, follow_symlinks=False)
        os.chmod(runtime_binary, 0o500)
        runtime_stat = runtime_binary.lstat()
        if not stat.S_ISREG(runtime_stat.st_mode) or runtime_binary.is_symlink():
            raise OSError("runtime executable is not a regular file")
        return runtime_binary

    @staticmethod
    def _bounded(raw: bytes, truncated: bool, text: str) -> BoundedText:
        return BoundedText(
            text=text,
            bytes_captured=len(raw),
            truncated=truncated,
        )

    @staticmethod
    def _phase_status(outcome: ProcessOutcome) -> PhaseStatus:
        if outcome.launch_error:
            return PhaseStatus.INFRA_ERROR
        if outcome.stderr.lstrip().startswith(b"runner-limit-exec:"):
            return PhaseStatus.INFRA_ERROR
        if outcome.output_limited:
            return PhaseStatus.OUTPUT_LIMIT
        if outcome.timed_out or outcome.exit_code == -getattr(signal, "SIGXCPU", 24):
            return PhaseStatus.TIME_LIMIT
        if outcome.workspace_limited:
            return PhaseStatus.WORKSPACE_LIMIT
        if outcome.exit_code == -getattr(signal, "SIGXFSZ", 25):
            return PhaseStatus.WORKSPACE_LIMIT
        if outcome.exit_code == 0:
            return PhaseStatus.SUCCEEDED
        stderr = outcome.stderr.decode("utf-8", errors="ignore").lower()
        if (
            "bad_alloc" in stderr
            or "cannot allocate memory" in stderr
            or "out of memory" in stderr
        ):
            return PhaseStatus.MEMORY_LIMIT
        return PhaseStatus.FAILED

    @staticmethod
    def _overall_status(outcome: ProcessOutcome, *, compilation: bool) -> JobStatus:
        if outcome.launch_error:
            return JobStatus.INFRA_ERROR
        stderr = outcome.stderr.decode("utf-8", errors="ignore")
        if outcome.exit_code not in {0, None} and stderr.lstrip().startswith(
            ("runner-limit-exec:",)
        ):
            return JobStatus.INFRA_ERROR
        if outcome.output_limited:
            return JobStatus.OUTPUT_LIMIT
        if outcome.timed_out or outcome.exit_code == -getattr(signal, "SIGXCPU", 24):
            return JobStatus.TIME_LIMIT
        if outcome.workspace_limited:
            return JobStatus.WORKSPACE_LIMIT
        if outcome.exit_code == -getattr(signal, "SIGXFSZ", 25):
            return JobStatus.WORKSPACE_LIMIT
        lowered = stderr.lower()
        if (
            "bad_alloc" in lowered
            or "cannot allocate memory" in lowered
            or "out of memory" in lowered
        ):
            return JobStatus.MEMORY_LIMIT
        if outcome.exit_code != 0:
            return JobStatus.COMPILE_ERROR if compilation else JobStatus.RUNTIME_ERROR
        return JobStatus.COMPILED if compilation else JobStatus.SUCCESS

    def _phase_result(self, outcome: ProcessOutcome, mapper: PathMapper) -> PhaseResult:
        stdout_text = mapper.sanitize_output(outcome.stdout)
        if outcome.launch_error:
            stderr_raw = outcome.launch_error.encode("utf-8", errors="replace")
            stderr_text = "executor launch failed"
        else:
            stderr_raw = outcome.stderr
            stderr_text = mapper.sanitize_output(outcome.stderr)
        return PhaseResult(
            status=self._phase_status(outcome),
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            stdout=self._bounded(outcome.stdout, outcome.stdout_truncated, stdout_text),
            stderr=self._bounded(stderr_raw, outcome.stderr_truncated, stderr_text),
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResponse:
        acquired = self._slots.acquire(timeout=self.settings.queue_wait_seconds)
        if not acquired:
            raise RunnerBusyError("runner concurrency limit reached")
        layout: JobLayout | None = None
        try:
            profile, source_paths, runtime_data_paths = self._validate_request(request)
            job_id = uuid.uuid4().hex
            manifest_hash = self._manifest_hash(request)
            layout = self._create_layout(job_id)
            runtime_data = set(runtime_data_paths)
            for item in request.files:
                relative = normalize_relative_path(item.path)
                write_regular_file(
                    layout.runtime_output
                    if str(relative) in runtime_data
                    else layout.source,
                    relative,
                    item.content.encode("utf-8"),
                    writable=str(relative) in runtime_data,
                )

            compiler = self._compiler_paths[profile.compiler_name]
            assert compiler is not None
            local_sources = [
                str(layout.source.joinpath(*Path(path).parts)) for path in source_paths
            ]
            sandbox_sources = [f"/workspace/source/{path}" for path in source_paths]
            local_binary = layout.build / "program"
            local_command = [
                compiler,
                *profile.fixed_flags,
                *local_sources,
                "-o",
                str(local_binary),
            ]
            sandbox_command = [
                compiler,
                *profile.fixed_flags,
                *sandbox_sources,
                "-o",
                "/workspace/build/program",
            ]
            mapper = PathMapper(layout.source, layout.build, layout.root)
            compilation = self.executor.compile(
                layout=layout,
                local_command=local_command,
                sandbox_command=sandbox_command,
                limits=profile.compile_limits,
            )
            diagnostics = parse_compiler_diagnostics(
                compilation.stderr,
                compiler_family=profile.compiler_family,
                compiler_version=self._compiler_versions.get(profile.compiler_name),
                mapper=mapper,
            )
            compilation_result = self._phase_result(compilation, mapper)
            compile_status = self._overall_status(compilation, compilation=True)
            profile_summary = ProfileSummary(
                id=profile.id,
                language=profile.language,
                compiler_family=profile.compiler_family,
                compiler_version=self._compiler_versions.get(profile.compiler_name),
                standard=profile.standard,
                mode=profile.mode,
            )
            base = dict(
                request_id=request.request_id,
                job_id=job_id,
                manifest_sha256=manifest_hash,
                profile=profile_summary,
                isolation=self.executor.isolation,
                compilation=compilation_result,
                diagnostics=diagnostics,
            )
            if compile_status != JobStatus.COMPILED:
                return ExecutionResponse(status=compile_status, **base)

            try:
                binary_stat = local_binary.lstat()
            except OSError:
                return ExecutionResponse(status=JobStatus.INFRA_ERROR, **base)
            if not stat.S_ISREG(binary_stat.st_mode) or local_binary.is_symlink():
                return ExecutionResponse(status=JobStatus.INFRA_ERROR, **base)
            executable_hash = hashlib.sha256(local_binary.read_bytes()).hexdigest()
            if request.action == Action.COMPILE:
                return ExecutionResponse(
                    status=JobStatus.COMPILED,
                    executable_sha256=executable_hash,
                    **base,
                )

            try:
                runtime_binary = self._stage_runtime_binary(layout, local_binary)
            except OSError:
                return ExecutionResponse(status=JobStatus.INFRA_ERROR, **base)

            execution = self.executor.run(
                layout=layout,
                local_command=[str(runtime_binary)],
                sandbox_command=["/workspace/program"],
                stdin=request.stdin.encode("utf-8"),
                limits=self._effective_run_limits(profile.run_limits, request.limits),
            )
            return ExecutionResponse(
                status=self._overall_status(execution, compilation=False),
                executable_sha256=executable_hash,
                execution=self._phase_result(execution, mapper),
                **base,
            )
        finally:
            if layout is not None:
                shutil.rmtree(layout.root, ignore_errors=True)
            self._slots.release()

    def start_interactive(
        self, request: InteractiveSessionCreateRequest
    ) -> InteractiveSessionResponse:
        """Compile once, then retain a bounded process for line-oriented stdin."""

        self._prune_interactive_sessions()
        acquired = self._slots.acquire(timeout=self.settings.queue_wait_seconds)
        if not acquired:
            raise RunnerBusyError("runner concurrency limit reached")
        layout: JobLayout | None = None
        slot_owned = True
        try:
            manifest = ExecutionRequest(
                request_id=request.request_id,
                profile_id=request.profile_id,
                action=Action.COMPILE_AND_RUN,
                files=request.files,
                stdin="",
                limits=request.limits,
            )
            profile, source_paths, runtime_data_paths = self._validate_request(manifest)
            session_id = uuid.uuid4().hex
            layout = self._create_layout(session_id)
            runtime_data = set(runtime_data_paths)
            for item in request.files:
                relative = normalize_relative_path(item.path)
                write_regular_file(
                    layout.runtime_output
                    if str(relative) in runtime_data
                    else layout.source,
                    relative,
                    item.content.encode("utf-8"),
                    writable=str(relative) in runtime_data,
                )

            compiler = self._compiler_paths[profile.compiler_name]
            assert compiler is not None
            local_sources = [
                str(layout.source.joinpath(*Path(path).parts)) for path in source_paths
            ]
            local_binary = layout.build / "program"
            local_command = [
                compiler,
                *profile.fixed_flags,
                *local_sources,
                "-o",
                str(local_binary),
            ]
            sandbox_sources = [f"/workspace/source/{path}" for path in source_paths]
            sandbox_command = [
                compiler,
                *profile.fixed_flags,
                *sandbox_sources,
                "-o",
                "/workspace/build/program",
            ]
            mapper = PathMapper(layout.source, layout.build, layout.root)
            compilation_outcome = self.executor.compile(
                layout=layout,
                local_command=local_command,
                sandbox_command=sandbox_command,
                limits=profile.compile_limits,
            )
            compilation = self._phase_result(compilation_outcome, mapper)
            diagnostics = parse_compiler_diagnostics(
                compilation_outcome.stderr,
                compiler_family=profile.compiler_family,
                compiler_version=self._compiler_versions.get(profile.compiler_name),
                mapper=mapper,
            )
            compile_status = self._overall_status(compilation_outcome, compilation=True)
            process: InteractiveProcess | None = None
            runtime_binary: Path | None = None
            if compile_status == JobStatus.COMPILED:
                try:
                    binary_stat = local_binary.lstat()
                except OSError:
                    compile_status = JobStatus.INFRA_ERROR
                else:
                    if (
                        not stat.S_ISREG(binary_stat.st_mode)
                        or local_binary.is_symlink()
                    ):
                        compile_status = JobStatus.INFRA_ERROR
            if compile_status == JobStatus.COMPILED:
                try:
                    runtime_binary = self._stage_runtime_binary(layout, local_binary)
                except OSError:
                    compile_status = JobStatus.INFRA_ERROR
            if compile_status == JobStatus.COMPILED:
                assert runtime_binary is not None
                run_limits = self._effective_run_limits(
                    profile.run_limits, request.limits
                )
                run_limits = replace(
                    run_limits,
                    wall_seconds=float(self.settings.interactive_wall_seconds),
                )
                process = InteractiveProcess(
                    [str(runtime_binary)],
                    cwd=layout.runtime_output,
                    environment={
                        "PATH": "/usr/local/bin:/usr/bin:/bin",
                        "LANG": "C.UTF-8",
                        "LC_ALL": "C.UTF-8",
                        "HOME": "/nonexistent",
                        "TMPDIR": "/tmp",
                    },
                    limits=run_limits,
                    monitored_paths=(layout.runtime_output, layout.runtime_tmp),
                )

            session = _InteractiveSession(
                id=session_id,
                owner_key=request.owner_key,
                layout=layout,
                process=process,
                mapper=mapper,
                compilation=compilation,
                diagnostics=diagnostics,
                compile_status=compile_status,
                created_at=time.monotonic(),
            )
            with self._interactive_lock:
                self._interactive_sessions[session_id] = session

            if process is None:
                shutil.rmtree(layout.root, ignore_errors=True)
                session.layout = None
                session.terminal_at = time.monotonic()
                self._slots.release()
                slot_owned = False
            else:
                cleanup = threading.Thread(
                    target=self._finish_interactive_session,
                    args=(session_id, process),
                    daemon=True,
                )
                cleanup.start()
                slot_owned = False  # cleanup thread releases it
            return self._interactive_response(session)
        finally:
            if slot_owned:
                if layout is not None:
                    shutil.rmtree(layout.root, ignore_errors=True)
                self._slots.release()

    def interactive_snapshot(
        self, session_id: str, *, owner_key: str
    ) -> InteractiveSessionResponse:
        session = self._interactive_session(session_id, owner_key=owner_key)
        return self._interactive_response(session)

    def interactive_send_line(
        self, session_id: str, *, owner_key: str, text: str
    ) -> InteractiveSessionResponse:
        session = self._interactive_session(session_id, owner_key=owner_key)
        process = session.process
        if process is None or process.finished or not process.send_line(text):
            raise InteractiveSessionStateError("interactive program is not running")
        return self._interactive_response(session)

    def interactive_stop(
        self, session_id: str, *, owner_key: str
    ) -> InteractiveSessionResponse:
        session = self._interactive_session(session_id, owner_key=owner_key)
        process = session.process
        if process is not None and not process.finished:
            process.stop()
            process.wait(timeout=2)
        return self._interactive_response(session)

    def interactive_close_input(
        self, session_id: str, *, owner_key: str
    ) -> InteractiveSessionResponse:
        session = self._interactive_session(session_id, owner_key=owner_key)
        process = session.process
        if process is None or process.finished or not process.close_input():
            raise InteractiveSessionStateError("interactive program is not running")
        return self._interactive_response(session)

    def close_interactive_sessions(self) -> None:
        with self._interactive_lock:
            sessions = list(self._interactive_sessions.values())
        for session in sessions:
            if session.process is not None and not session.process.finished:
                session.process.stop()
        for session in sessions:
            if session.process is not None:
                session.process.wait(timeout=2)

    def _interactive_session(
        self, session_id: str, *, owner_key: str
    ) -> _InteractiveSession:
        self._prune_interactive_sessions()
        with self._interactive_lock:
            session = self._interactive_sessions.get(session_id)
        # Deliberately return the same result for an unknown id and wrong owner.
        if session is None or not secrets.compare_digest(session.owner_key, owner_key):
            raise InteractiveSessionNotFoundError("interactive session was not found")
        return session

    def _finish_interactive_session(
        self, session_id: str, process: InteractiveProcess
    ) -> None:
        process.wait()
        with self._interactive_lock:
            session = self._interactive_sessions.get(session_id)
            if session is None or session.process is not process:
                return
            layout = session.layout
            session.layout = None
            session.terminal_at = time.monotonic()
        if layout is not None:
            shutil.rmtree(layout.root, ignore_errors=True)
        self._slots.release()

    def _prune_interactive_sessions(self) -> None:
        cutoff = time.monotonic() - self.settings.interactive_terminal_ttl_seconds
        with self._interactive_lock:
            expired = [
                session_id
                for session_id, session in self._interactive_sessions.items()
                if session.terminal_at is not None and session.terminal_at < cutoff
            ]
            for session_id in expired:
                self._interactive_sessions.pop(session_id, None)

    def _interactive_response(
        self, session: _InteractiveSession
    ) -> InteractiveSessionResponse:
        process = session.process
        if process is None:
            compile_status = session.compile_status.value
            status = (
                compile_status
                if compile_status
                in {
                    "COMPILE_ERROR",
                    "TIME_LIMIT",
                    "MEMORY_LIMIT",
                    "OUTPUT_LIMIT",
                    "WORKSPACE_LIMIT",
                    "INFRA_ERROR",
                }
                else "INFRA_ERROR"
            )
            return InteractiveSessionResponse(
                session_id=session.id,
                status=status,
                terminal=True,
                duration_ms=session.compilation.duration_ms,
                stdout=session.compilation.stdout.text,
                # GCC/Clang structured diagnostics are carried by the dedicated
                # diagnostics field.  Do not leak their JSON/SARIF payload (for
                # a successful GCC invocation this is literally ``[]``) into
                # the program terminal.
                stderr=("" if session.diagnostics else session.compilation.stderr.text),
                output_truncated=(
                    session.compilation.stdout.truncated
                    or session.compilation.stderr.truncated
                ),
                input_closed=True,
                compilation=session.compilation,
                diagnostics=session.diagnostics,
            )

        outcome = process.snapshot()
        if not process.finished:
            status = "RUNNING"
        elif process.stopped_by_user:
            status = "STOPPED"
        else:
            status = self._overall_status(outcome, compilation=False).value
        if status == "COMPILED":
            status = "SUCCESS"
        runtime_stderr = session.mapper.sanitize_output(outcome.stderr)
        return InteractiveSessionResponse(
            session_id=session.id,
            status=status,
            terminal=process.finished,
            exit_code=outcome.exit_code,
            duration_ms=outcome.duration_ms,
            stdout=session.mapper.sanitize_output(outcome.stdout),
            # Compiler diagnostics are already structured and rendered next to
            # their source lines.  The interactive terminal is reserved for
            # stderr produced by the running program itself.
            stderr=runtime_stderr,
            output_truncated=(
                outcome.stdout_truncated
                or outcome.stderr_truncated
                or outcome.output_limited
            ),
            input_closed=process.input_closed,
            compilation=session.compilation,
            diagnostics=session.diagnostics,
        )
