from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


class UnsafePathError(ValueError):
    pass


def normalize_relative_path(raw: str) -> PurePosixPath:
    if not raw or "\x00" in raw or "\\" in raw or raw.startswith("/"):
        raise UnsafePathError("path must be a normalized relative POSIX path")
    components = raw.split("/")
    if any(part in {"", ".", ".."} for part in components):
        raise UnsafePathError("path contains a forbidden component")
    if any(len(part.encode("utf-8")) > 100 for part in components):
        raise UnsafePathError("path component is too long")
    path = PurePosixPath(*components)
    if path.is_absolute() or str(path) != raw:
        raise UnsafePathError("path is not normalized")
    return path


def write_regular_file(
    root: Path,
    relative: PurePosixPath,
    content: bytes,
    *,
    writable: bool = False,
) -> Path:
    destination = root.joinpath(*relative.parts)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    root_resolved = root.resolve(strict=True)
    parent_resolved = destination.parent.resolve(strict=True)
    if (
        root_resolved != parent_resolved
        and root_resolved not in parent_resolved.parents
    ):
        raise UnsafePathError("destination escaped workspace")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(destination, flags, 0o600 if writable else 0o400)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
    return destination
