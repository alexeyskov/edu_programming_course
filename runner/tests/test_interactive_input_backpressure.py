from __future__ import annotations

import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from conftest import signed_headers
from edu_runner.process import InteractiveInputBackpressure, InteractiveProcess


@pytest.fixture
def pipe_process():
    read_fd, write_fd = os.pipe()
    os.set_blocking(read_fd, False)
    os.set_blocking(write_fd, False)
    writer = os.fdopen(write_fd, "wb", buffering=0)
    process = object.__new__(InteractiveProcess)
    process._input_lock = threading.Lock()
    process._input_closed = False
    process._pending_input = bytearray()
    process.process = SimpleNamespace(stdin=writer, poll=lambda: None)
    try:
        yield process, read_fd
    finally:
        writer.close()
        os.close(read_fd)


def test_full_stdin_does_not_block_input_state_or_eof(pipe_process):
    process, _ = pipe_process
    while True:
        try:
            os.write(process.process.stdin.fileno(), b"x" * 4096)
        except BlockingIOError:
            break
    # The program is alive but never reads. Neither HTTP input nor state/EOF
    # should wait on its pipe, and accepted input must remain queued intact.
    assert process.send_line("new input")
    assert not process.input_closed
    process._drain_input()
    assert process._pending_input == b"new input\n"
    assert process.close_input()
    assert process.input_closed
    assert not process.send_line("too late")


def test_partial_pipe_writes_preserve_order_and_deliver_eof_last(
    pipe_process, monkeypatch
):
    process, read_fd = pipe_process
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:7]))
    assert process.send_line("first: привет")
    assert process.send_line("second")
    assert process.close_input()
    expected = "first: привет\nsecond\n".encode()
    for _ in range(len(expected)):
        process._drain_input()
    assert process.process.stdin.closed
    assert os.read(read_fd, 1024) == expected
    assert os.read(read_fd, 1024) == b""


def test_queue_limit_rejects_whole_line_without_losing_accepted_input(
    pipe_process, monkeypatch
):
    process, _ = pipe_process
    monkeypatch.setattr("edu_runner.process.MAX_PENDING_INTERACTIVE_INPUT_BYTES", 16)
    assert process.send_line("123456789012345")
    with pytest.raises(InteractiveInputBackpressure):
        process.send_line("rejected")
    assert process._pending_input == b"123456789012345\n"
    assert not process.input_closed


def test_twenty_parallel_input_commands_finish_without_waiting_for_program(
    pipe_process,
):
    process, _ = pipe_process
    # No monitor drains the queue: submission itself must be nonblocking.
    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [
            pool.submit(process.send_line, str(index) * 1000) for index in range(20)
        ]
        assert all(future.result(timeout=2) for future in futures)
    lines = process._pending_input.decode().splitlines()
    assert sorted(lines) == sorted(str(index) * 1000 for index in range(20))


def test_input_backpressure_returns_explicit_http_429(client, service, monkeypatch):
    def full_queue(_text):
        raise InteractiveInputBackpressure("interactive input queue is full")

    monkeypatch.setattr(
        service,
        "_interactive_session",
        lambda *args, **kwargs: SimpleNamespace(
            process=SimpleNamespace(finished=False, send_line=full_queue)
        ),
    )
    body = json.dumps({"owner_key": "owner-test-00000001", "text": "line"}).encode()
    response = client.post(
        "/v1/interactive-sessions/test-session/input",
        content=body,
        headers=signed_headers(body),
    )
    assert response.status_code == 429
    assert response.json()["detail"] == "interactive input queue is full"
