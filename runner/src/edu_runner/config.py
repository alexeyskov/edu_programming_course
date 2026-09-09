from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


ExecutorMode = Literal["local"]


@dataclass(frozen=True, slots=True)
class Settings:
    shared_secret: bytes
    # C/C++ is deliberately executed as an ordinary subprocess inside the
    # dedicated runner container.  The host/container boundary is the only
    # deployment boundary; there is no per-job Bubblewrap namespace.
    executor_mode: ExecutorMode = "local"
    work_root: Path = Path("/tmp/edu-programming-runner")
    max_concurrent_jobs: int = 2
    queue_wait_seconds: float = 0.25
    request_body_limit_bytes: int = 4 * 1024 * 1024
    signature_clock_skew_seconds: int = 60
    nonce_ttl_seconds: int = 300
    interactive_wall_seconds: int = 60
    interactive_terminal_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if len(self.shared_secret) < 32:
            raise ValueError("RUNNER_SHARED_SECRET must contain at least 32 bytes")
        if self.executor_mode != "local":
            raise ValueError("RUNNER_EXECUTOR must be local")
        if self.max_concurrent_jobs < 1:
            raise ValueError("RUNNER_MAX_CONCURRENT_JOBS must be positive")
        if self.request_body_limit_bytes < 1024:
            raise ValueError("RUNNER_REQUEST_BODY_LIMIT_BYTES is too small")
        if self.signature_clock_skew_seconds < 1 or self.nonce_ttl_seconds < 1:
            raise ValueError("signature time windows must be positive")
        if not 5 <= self.interactive_wall_seconds <= 300:
            raise ValueError(
                "RUNNER_INTERACTIVE_WALL_SECONDS must be between 5 and 300"
            )
        if not 30 <= self.interactive_terminal_ttl_seconds <= 3600:
            raise ValueError(
                "RUNNER_INTERACTIVE_TERMINAL_TTL_SECONDS must be between 30 and 3600"
            )

    @classmethod
    def from_env(cls) -> "Settings":
        secret = os.environ.get("RUNNER_SHARED_SECRET")
        secret_file = os.environ.get("RUNNER_SHARED_SECRET_FILE")
        if secret is not None and secret_file is not None:
            raise RuntimeError(
                "set only one of RUNNER_SHARED_SECRET and RUNNER_SHARED_SECRET_FILE"
            )
        if secret_file is not None:
            secret_path = Path(secret_file)
            if secret_path.is_symlink() or not secret_path.is_file():
                raise RuntimeError("RUNNER_SHARED_SECRET_FILE must be a regular file")
            secret_bytes = secret_path.read_bytes().rstrip(b"\r\n")
        elif secret is not None:
            secret_bytes = secret.encode("utf-8")
        else:
            raise RuntimeError(
                "RUNNER_SHARED_SECRET or RUNNER_SHARED_SECRET_FILE is required"
            )
        mode = os.environ.get("RUNNER_EXECUTOR", "local").strip().lower()
        if mode != "local":
            raise ValueError("RUNNER_EXECUTOR must be local")
        return cls(
            shared_secret=secret_bytes,
            executor_mode=mode,  # type: ignore[arg-type]
            work_root=Path(
                os.environ.get("RUNNER_WORK_ROOT", "/tmp/edu-programming-runner")
            ).resolve(),
            max_concurrent_jobs=int(os.environ.get("RUNNER_MAX_CONCURRENT_JOBS", "2")),
            queue_wait_seconds=float(
                os.environ.get("RUNNER_QUEUE_WAIT_SECONDS", "0.25")
            ),
            request_body_limit_bytes=int(
                os.environ.get("RUNNER_REQUEST_BODY_LIMIT_BYTES", str(4 * 1024 * 1024))
            ),
            signature_clock_skew_seconds=int(
                os.environ.get("RUNNER_SIGNATURE_CLOCK_SKEW_SECONDS", "60")
            ),
            nonce_ttl_seconds=int(os.environ.get("RUNNER_NONCE_TTL_SECONDS", "300")),
            interactive_wall_seconds=int(
                os.environ.get("RUNNER_INTERACTIVE_WALL_SECONDS", "60")
            ),
            interactive_terminal_ttl_seconds=int(
                os.environ.get("RUNNER_INTERACTIVE_TERMINAL_TTL_SECONDS", "300")
            ),
        )
