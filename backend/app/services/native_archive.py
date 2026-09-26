"""Independent, bounded in-memory 7z reader; never extracts to the filesystem."""

from __future__ import annotations

import ctypes as c
import ctypes.util
import time
import zlib
from functools import lru_cache


class NativeArchiveError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@lru_cache(maxsize=1)
def _library():
    name = ctypes.util.find_library("archive")
    if not name:
        raise NativeArchiveError("ARCHIVE_READER_UNAVAILABLE")
    try:
        lib = c.CDLL(name)
        signatures = {
            "archive_read_new": (c.c_void_p, []),
            "archive_read_support_format_7zip": (c.c_int, [c.c_void_p]),
            "archive_read_open_memory": (c.c_int, [c.c_void_p, c.c_void_p, c.c_size_t]),
            "archive_read_next_header": (c.c_int, [c.c_void_p, c.POINTER(c.c_void_p)]),
            "archive_entry_pathname_utf8": (c.c_char_p, [c.c_void_p]),
            "archive_entry_size": (c.c_longlong, [c.c_void_p]),
            "archive_entry_filetype": (c.c_uint, [c.c_void_p]),
            "archive_read_data": (c.c_ssize_t, [c.c_void_p, c.c_void_p, c.c_size_t]),
            "archive_read_data_skip": (c.c_int, [c.c_void_p]),
            "archive_error_string": (c.c_char_p, [c.c_void_p]),
            "archive_read_free": (c.c_int, [c.c_void_p]),
        }
        for symbol, (result, args) in signatures.items():
            fn = getattr(lib, symbol)
            fn.restype, fn.argtypes = result, args
        return lib
    except (OSError, AttributeError) as exc:
        raise NativeArchiveError("ARCHIVE_READER_UNAVAILABLE") from exc


def read_7z_members(
    content: bytes,
    members: dict[str, tuple[int, int | None]],
    *,
    max_file_bytes: int,
    max_source_bytes: int,
    max_expanded_bytes: int,
    max_members: int,
) -> dict[str, bytes]:
    """Return selected regular files, bounding sizes and verifying their CRC.

    The caller has already validated the entire 7z inventory and source paths.
    Recheck native headers too: malformed/differing metadata must fail closed.
    Ignored build binaries are streamed past, never retained as Python bytes.
    """
    if len(content) > 100 * 1024 * 1024 or len(members) > max_members:
        raise NativeArchiveError("ARCHIVE_TOO_LARGE")
    if any(not 0 <= size <= max_file_bytes for size, _ in members.values()):
        raise NativeArchiveError("ARCHIVE_EXPANDED_SIZE_LIMIT")
    if sum(size for size, _ in members.values()) > max_source_bytes:
        raise NativeArchiveError("ARCHIVE_EXPANDED_SIZE_LIMIT")
    lib = _library()
    archive = lib.archive_read_new()
    if not archive:
        raise NativeArchiveError("ARCHIVE_READER_UNAVAILABLE")
    # Keep content alive while C reads it; explicit length handles embedded NULs.
    memory = c.c_char_p(content)
    deadline = time.monotonic() + 30
    result: dict[str, bytes] = {}
    source_bytes = 0

    def check(code: int) -> None:
        if code < 0:
            message = (lib.archive_error_string(archive) or b"").lower()
            reason = (
                "ARCHIVE_CHECKSUM_MISMATCH"
                if b"crc" in message or b"checksum" in message
                else "ARCHIVE_INVALID_OR_ENCRYPTED"
            )
            raise NativeArchiveError(reason)
        if time.monotonic() > deadline:
            raise NativeArchiveError("ARCHIVE_EXTRACTION_TIMEOUT")

    try:
        check(lib.archive_read_support_format_7zip(archive))
        check(lib.archive_read_open_memory(archive, memory, len(content)))
        entry = c.c_void_p()
        chunk = c.create_string_buffer(64 * 1024)
        total_declared = count = 0
        while True:
            code = lib.archive_read_next_header(archive, c.byref(entry))
            check(code)
            if code == 1:  # ARCHIVE_EOF
                break
            count += 1
            size = lib.archive_entry_size(entry)
            total_declared += max(0, size)
            if count > max_members or total_declared > max_expanded_bytes:
                raise NativeArchiveError("ARCHIVE_EXPANDED_SIZE_LIMIT")
            raw_name = lib.archive_entry_pathname_utf8(entry)
            name = raw_name.decode("utf-8", errors="strict") if raw_name else ""
            if name not in members:
                check(lib.archive_read_data_skip(archive))
                continue
            if name in result:
                raise NativeArchiveError("ARCHIVE_DUPLICATE_PATH")
            expected_size, expected_crc = members[name]
            if lib.archive_entry_filetype(entry) != 0o100000 or not 0 <= size <= max_file_bytes:
                raise NativeArchiveError("ARCHIVE_EXTRACTION_INCOMPLETE")
            # py7zr can report an incorrect unpacked size for a valid solid
            # archive. Independent decoding plus the archive's CRC is required
            # before accepting different metadata; limits still apply to both.
            if size != expected_size and expected_crc is None:
                raise NativeArchiveError("ARCHIVE_EXTRACTION_INCOMPLETE")
            data = bytearray()
            crc = 0
            while True:
                length = lib.archive_read_data(archive, chunk, len(chunk))
                check(length)
                if length == 0:
                    break
                if len(data) + length > size:
                    raise NativeArchiveError("ARCHIVE_EXPANDED_SIZE_LIMIT")
                block = chunk.raw[:length]
                data.extend(block)
                crc = zlib.crc32(block, crc)
            if len(data) != size and expected_crc is None:
                raise NativeArchiveError("ARCHIVE_EXTRACTION_INCOMPLETE")
            if expected_crc is not None and crc != expected_crc:
                raise NativeArchiveError("ARCHIVE_CHECKSUM_MISMATCH")
            source_bytes += len(data)
            if source_bytes > max_source_bytes:
                raise NativeArchiveError("ARCHIVE_EXPANDED_SIZE_LIMIT")
            result[name] = bytes(data)
        if result.keys() != members.keys():
            raise NativeArchiveError("ARCHIVE_EXTRACTION_INCOMPLETE")
        return result
    except UnicodeError as exc:
        raise NativeArchiveError("ARCHIVE_INVALID_OR_ENCRYPTED") from exc
    finally:
        lib.archive_read_free(archive)
