from __future__ import annotations

import hashlib
import stat
from io import BytesIO
from zipfile import ZipFile

import pytest

from app.integrations.errors import IntegrationProtocolError
from app.integrations.moodle_artifact import (
    build_moodle_online_text_artifact,
    build_moodle_quiz_essay_artifact,
)
from app.integrations.moodle_transport import moodle_file_type_allowed


def test_single_cpp_file_is_full_utf8_source_under_stable_ascii_name() -> None:
    source = "// проверка UTF-8\nint main() { return 0; }\n"

    artifact = build_moodle_quiz_essay_artifact(
        [{"path": "src/main.cpp", "content": source, "content_hash": "not-used-here"}]
    )

    assert artifact.filename == "main.cpp"
    assert artifact.raw_bytes == source.encode("utf-8")
    assert artifact.size == len(artifact.raw_bytes)
    assert artifact.sha256 == hashlib.sha256(artifact.raw_bytes).hexdigest()


def test_single_c_file_uses_stable_c_name_and_empty_source_is_valid() -> None:
    artifact = build_moodle_quiz_essay_artifact([{"path": "исходник/программа.c", "content": ""}])

    assert artifact.filename == "main.c"
    assert artifact.raw_bytes == b""
    assert artifact.size == 0
    assert artifact.sha256 == hashlib.sha256(b"").hexdigest()


def test_forced_archive_is_stable_for_one_file_and_deterministic() -> None:
    files = [{"path": "src/main.cpp", "content": "int main() {}\n"}]

    first = build_moodle_quiz_essay_artifact(files, force_archive=True)
    second = build_moodle_quiz_essay_artifact(files, force_archive=True)

    assert first.filename == "submission.zip"
    assert first.raw_bytes == second.raw_bytes
    with ZipFile(BytesIO(first.raw_bytes)) as archive:
        assert archive.namelist() == ["src/main.cpp"]
        assert archive.read("src/main.cpp") == b"int main() {}\n"


def test_multifile_zip_is_deterministic_sorted_and_regular() -> None:
    files = [
        {"path": "src/main.cpp", "content": '#include "lib/value.hpp"\n'},
        {"path": "lib/value.hpp", "content": "constexpr int value = 7;\n"},
    ]

    first = build_moodle_quiz_essay_artifact(files)
    second = build_moodle_quiz_essay_artifact(list(reversed(files)))

    assert first.filename == "submission.zip"
    assert first.raw_bytes == second.raw_bytes
    assert first.sha256 == second.sha256
    assert first.size == len(first.raw_bytes)
    assert first.sha256 == hashlib.sha256(first.raw_bytes).hexdigest()

    with ZipFile(BytesIO(first.raw_bytes)) as archive:
        assert archive.namelist() == ["lib/value.hpp", "src/main.cpp"]
        assert archive.read("lib/value.hpp") == b"constexpr int value = 7;\n"
        assert archive.read("src/main.cpp") == b'#include "lib/value.hpp"\n'
        for info in archive.infolist():
            mode = info.external_attr >> 16
            assert info.date_time == (1980, 1, 1, 0, 0, 0)
            assert info.create_system == 3
            assert stat.S_ISREG(mode)
            assert stat.S_IMODE(mode) == 0o644
            assert not stat.S_ISLNK(mode)


def test_companion_headers_text_and_inc_force_zip_and_preserve_relative_paths() -> None:
    files = [
        {"path": "src/main.cpp", "content": '#include "../include/config.inc"\n'},
        {"path": "include/value.h", "content": "int value();\n"},
        {"path": "include/value.hpp", "content": "constexpr int answer = 42;\n"},
        {"path": "include/config.inc", "content": "#define ENABLED 1\n"},
        {"path": "data/input.txt", "content": "42\n"},
    ]

    artifact = build_moodle_quiz_essay_artifact(files)

    assert artifact.filename == "submission.zip"
    with ZipFile(BytesIO(artifact.raw_bytes)) as archive:
        assert archive.namelist() == [
            "data/input.txt",
            "include/config.inc",
            "include/value.h",
            "include/value.hpp",
            "src/main.cpp",
        ]


def test_online_text_sends_only_translation_unit_and_keeps_workspace_data_out_of_answer() -> None:
    artifact = build_moodle_online_text_artifact(
        [
            {"path": "main.cpp", "content": "int main() { return 0; }\n"},
            {"path": "input.txt", "content": "42\n"},
        ]
    )

    assert artifact.filename == "main.cpp"
    assert artifact.raw_bytes == b"int main() { return 0; }\n"


def test_online_text_refuses_to_guess_between_translation_units() -> None:
    with pytest.raises(IntegrationProtocolError, match="exactly one"):
        build_moodle_online_text_artifact(
            [
                {"path": "first.cpp", "content": ""},
                {"path": "second.cpp", "content": ""},
                {"path": "input.txt", "content": ""},
            ]
        )


@pytest.mark.parametrize(
    "path",
    ["only.hpp", "only.h", "only.hh", "only.hxx", "only.inc", "input.txt"],
)
def test_single_non_translation_unit_is_still_an_archive(path: str) -> None:
    artifact = build_moodle_quiz_essay_artifact([{"path": path, "content": "data\n"}])

    assert artifact.filename == "submission.zip"
    with ZipFile(BytesIO(artifact.raw_bytes)) as archive:
        assert archive.namelist() == [path]


@pytest.mark.parametrize(
    "path",
    [
        "../main.cpp",
        "src/../../main.cpp",
        "/tmp/main.cpp",
        "src\\main.cpp",
        "src//main.cpp",
        "C:/main.cpp",
        "src/control\n.cpp",
        "src/no-extension",
    ],
)
def test_unsafe_source_paths_are_rejected(path: str) -> None:
    with pytest.raises(IntegrationProtocolError, match="unsafe source path"):
        build_moodle_quiz_essay_artifact([{"path": path, "content": ""}])


@pytest.mark.parametrize(
    "marker",
    [
        {"is_symlink": True},
        {"type": "symlink"},
        {"kind": "link"},
        {"mode": stat.S_IFLNK | 0o777},
    ],
)
def test_symbolic_link_entries_are_rejected(marker: dict[str, object]) -> None:
    with pytest.raises(IntegrationProtocolError, match="symbolic links"):
        build_moodle_quiz_essay_artifact([{"path": "main.cpp", "content": "", **marker}])


def test_duplicate_paths_are_rejected() -> None:
    with pytest.raises(IntegrationProtocolError, match="duplicate file paths"):
        build_moodle_quiz_essay_artifact(
            [
                {"path": "main.cpp", "content": "first"},
                {"path": "main.cpp", "content": "second"},
            ]
        )


@pytest.mark.parametrize("content", [b"bytes are not snapshot text", "\ud800"])
def test_non_utf8_snapshot_content_is_rejected(content: object) -> None:
    with pytest.raises(IntegrationProtocolError, match="UTF-8"):
        build_moodle_quiz_essay_artifact([{"path": "main.cpp", "content": content}])


def test_decoded_artifact_limit_covers_source_and_zip_overhead() -> None:
    with pytest.raises(IntegrationProtocolError, match="4 MiB"):
        build_moodle_quiz_essay_artifact(
            [{"path": "main.cpp", "content": "x" * 65}], maximum_bytes=64
        )

    with pytest.raises(IntegrationProtocolError, match="4 MiB"):
        build_moodle_quiz_essay_artifact(
            [
                {"path": "a.cpp", "content": "x"},
                {"path": "b.cpp", "content": "y"},
            ],
            maximum_bytes=64,
        )


def test_file_list_and_limit_configuration_are_bounded() -> None:
    with pytest.raises(IntegrationProtocolError, match="file count"):
        build_moodle_quiz_essay_artifact([])
    with pytest.raises(IntegrationProtocolError, match="size limit"):
        build_moodle_quiz_essay_artifact(
            [{"path": "main.cpp", "content": ""}], maximum_bytes=4 * 1024 * 1024 + 1
        )
    with pytest.raises(IntegrationProtocolError, match="archive flag"):
        build_moodle_quiz_essay_artifact(  # type: ignore[arg-type]
            [{"path": "main.cpp", "content": ""}], force_archive=1
        )


def test_assignment_accepted_types_do_not_silently_change_artifact_shape() -> None:
    assert moodle_file_type_allowed("", ".cpp") is True
    assert moodle_file_type_allowed(".cpp,.zip", ".cpp") is True
    assert moodle_file_type_allowed(".cpp,.zip", ".zip") is True
    assert moodle_file_type_allowed(".zip", ".cpp") is False
    assert moodle_file_type_allowed(".cpp", ".zip") is False
