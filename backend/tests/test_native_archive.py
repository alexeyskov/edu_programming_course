import io
import zlib
from dataclasses import replace

import py7zr
import pytest
from py7zr.exceptions import CrcError

from app.services import moodle_history as history
from app.services.native_archive import NativeArchiveError, read_7z_members


def archive_bytes():
    stream = io.BytesIO()
    with py7zr.SevenZipFile(stream, mode="w", filters=[{"id": py7zr.FILTER_COPY}]) as archive:
        archive.writestr(b"x" * 1024, "build/unneeded.bin")
        archive.writestr(b"int main() { return 0; }\n", "src/main.cpp")
    return stream.getvalue()


def native(content, *, size=24, crc=None, **overrides):
    return read_7z_members(
        content,
        {"src/main.cpp": (size, crc)},
        **{
            "max_file_bytes": 262144,
            "max_source_bytes": 524288,
            "max_expanded_bytes": 268435456,
            "max_members": 512,
            **overrides,
        },
    )


def test_native_reader_only_returns_selected_source_files_with_crc():
    source = b"int main() { return 0; }\n"
    assert native(archive_bytes(), size=len(source), crc=zlib.crc32(source)) == {
        "src/main.cpp": source,
    }


def test_py7zr_size_bug_recovers_only_using_independently_verified_source_crc(monkeypatch):
    original_list = py7zr.SevenZipFile.list

    def wrong_source_size(self):
        return [
            replace(member, uncompressed=60000) if member.filename.endswith(".cpp") else member
            for member in original_list(self)
        ]

    def crc_failure(*_args, **_kwargs):
        raise CrcError(1, 2, "src/main.cpp")

    monkeypatch.setattr(py7zr.SevenZipFile, "list", wrong_source_size)
    monkeypatch.setattr(py7zr.SevenZipFile, "extract", crc_failure)
    files, error = history._seven_zip_sources(archive_bytes(), set())
    assert error is None
    assert files == [("src/main.cpp", "int main() { return 0; }\n")]


def test_native_reader_does_not_accept_size_disagreement_without_a_checksum():
    with pytest.raises(NativeArchiveError, match="ARCHIVE_EXTRACTION_INCOMPLETE"):
        native(archive_bytes(), size=60000)


def test_native_reader_rejects_incorrect_crc():
    with pytest.raises(NativeArchiveError, match="ARCHIVE_CHECKSUM_MISMATCH"):
        native(archive_bytes(), size=24, crc=1)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_file_bytes": 8},
        {"max_source_bytes": 8},
        {"max_expanded_bytes": 8},
        {"max_members": 1},
    ],
)
def test_native_reader_cannot_bypass_expansion_or_file_limits(limits):
    with pytest.raises(NativeArchiveError):
        native(archive_bytes(), **limits)
