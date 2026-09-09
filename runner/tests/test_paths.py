from __future__ import annotations

from pathlib import Path, PurePosixPath
import stat

import pytest

from edu_runner.paths import (
    UnsafePathError,
    normalize_relative_path,
    write_regular_file,
)


@pytest.mark.parametrize(
    "value",
    [
        "../secret.cpp",
        "src/../../secret.cpp",
        "/etc/passwd",
        "src//main.cpp",
        "./main.cpp",
        "src/./main.cpp",
        "src\\main.cpp",
        "main.cpp\x00ignored",
        "",
    ],
)
def test_rejects_traversal_and_non_normal_paths(value: str) -> None:
    with pytest.raises(UnsafePathError):
        normalize_relative_path(value)


def test_writes_only_a_new_regular_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    destination = write_regular_file(
        root, PurePosixPath("include/value.hpp"), b"#pragma once"
    )
    assert destination.read_bytes() == b"#pragma once"
    assert not destination.is_symlink()
    with pytest.raises(FileExistsError):
        write_regular_file(root, PurePosixPath("include/value.hpp"), b"replacement")


def test_can_stage_a_user_writable_runtime_data_file(tmp_path: Path) -> None:
    root = tmp_path / "runtime-output"
    root.mkdir()
    destination = write_regular_file(
        root,
        PurePosixPath("fixtures/input.txt"),
        b"initial",
        writable=True,
    )
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600
    destination.write_text("updated", encoding="utf-8")
    assert destination.read_text(encoding="utf-8") == "updated"
