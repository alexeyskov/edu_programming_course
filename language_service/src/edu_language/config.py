from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Settings:
    shared_secret: bytes
    work_root: Path = Path("/tmp/edu-language")
    clangd: str = "/usr/bin/clangd"
    sandbox_launcher: str = "/usr/local/bin/edu-clangd-sandbox"
    isolation: str = "container"
    max_sessions: int = 4
    idle_seconds: float = 180
    request_seconds: float = 8
    process_memory_mb: int = 1024
    body_limit: int = 3 * 1024 * 1024
    output_limit: int = 1024 * 1024
    # Constructor injection for tests only; deliberately not available via env.
    test_without_sandbox: bool = False

    def __post_init__(self) -> None:
        if self.isolation not in {"container", "landlock"}:
            raise ValueError("isolation must be container or landlock")
        if len(self.shared_secret) < 32:
            raise ValueError(
                "CODE_INTELLIGENCE_SHARED_SECRET must contain at least 32 bytes"
            )
        if not 1 <= self.max_sessions <= 32:
            raise ValueError("max_sessions must be between 1 and 32")
        if not 5 <= self.idle_seconds <= 3600 or not 1 <= self.request_seconds <= 60:
            raise ValueError("invalid language service time limits")
        if not 256 <= self.process_memory_mb <= 4096:
            raise ValueError("invalid clangd memory limit")

    @classmethod
    def from_env(cls) -> Settings:
        prefix = "CODE_INTELLIGENCE_"
        secret = os.environ.get(prefix + "SHARED_SECRET")
        secret_file = os.environ.get(prefix + "SHARED_SECRET_FILE")
        if secret is not None and secret_file is not None:
            raise ValueError("set only one language service secret source")
        if secret_file:
            path = Path(secret_file)
            if path.is_symlink() or not path.is_file():
                raise ValueError("secret file must be a regular file")
            secret_bytes = path.read_bytes().rstrip(b"\r\n")
        else:
            secret_bytes = (secret or "").encode()
        return cls(
            shared_secret=secret_bytes,
            work_root=Path(os.environ.get(prefix + "WORK_ROOT", "/tmp/edu-language")),
            clangd=os.environ.get(prefix + "CLANGD", "/usr/bin/clangd"),
            sandbox_launcher=os.environ.get(
                prefix + "SANDBOX_LAUNCHER", "/usr/local/bin/edu-clangd-sandbox"
            ),
            isolation=os.environ.get(prefix + "ISOLATION", "container"),
            max_sessions=int(os.environ.get(prefix + "MAX_SESSIONS", "4")),
            idle_seconds=float(os.environ.get(prefix + "IDLE_SECONDS", "180")),
            request_seconds=float(os.environ.get(prefix + "REQUEST_SECONDS", "8")),
            process_memory_mb=int(os.environ.get(prefix + "PROCESS_MEMORY_MB", "1024")),
        )
