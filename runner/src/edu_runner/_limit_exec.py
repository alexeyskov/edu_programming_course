"""Small exec trampoline that applies rlimits without unsafe ``preexec_fn``."""

from __future__ import annotations

import os
import resource
import sys


def main() -> int:
    if len(sys.argv) < 8 or sys.argv[6] != "--":
        return 125
    cpu, memory, process_count, file_bytes, open_files = map(int, sys.argv[1:6])
    target = sys.argv[7:]
    try:
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
        resource.setrlimit(resource.RLIMIT_NOFILE, (open_files, open_files))
        if os.uname().sysname == "Linux":
            if hasattr(resource, "RLIMIT_NPROC"):
                resource.setrlimit(
                    resource.RLIMIT_NPROC, (process_count, process_count)
                )
            if hasattr(resource, "RLIMIT_AS"):
                resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        os.execvpe(target[0], target, os.environ)
    except Exception as exc:  # no secret or command details in the diagnostic
        sys.stderr.write(f"runner-limit-exec: {type(exc).__name__}\n")
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
