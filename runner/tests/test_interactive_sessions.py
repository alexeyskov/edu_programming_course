from __future__ import annotations

import json
import time

from fastapi.testclient import TestClient

from conftest import require_clang, signed_headers


def _post(client: TestClient, path: str, payload: dict[str, object]):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    return client.post(path, content=body, headers=signed_headers(body))


def _start(
    client: TestClient,
    source: str,
    extra_files: list[dict[str, str]] | None = None,
):
    return _post(
        client,
        "/v1/interactive-sessions",
        {
            "schema_version": "1.0",
            "request_id": "interactive-test-request",
            "owner_key": "experiment-owner-00000001",
            "profile_id": "cpp-clang-c++20-single",
            "files": [
                {"path": "main.cpp", "content": source},
                *(extra_files or []),
            ],
            "limits": {"cpu_seconds": 2, "memory_mb": 256},
        },
    )


def _state(client: TestClient, session_id: str):
    return _post(
        client,
        f"/v1/interactive-sessions/{session_id}/state",
        {"owner_key": "experiment-owner-00000001"},
    )


def _wait_for(client: TestClient, session_id: str, predicate, timeout: float = 3):
    deadline = time.monotonic() + timeout
    latest = _state(client, session_id)
    while time.monotonic() < deadline and not predicate(latest.json()):
        time.sleep(0.03)
        latest = _state(client, session_id)
    return latest


def test_interactive_program_accepts_separate_input_lines(client: TestClient) -> None:
    require_clang()
    started = _start(
        client,
        """
#include <iostream>
#include <string>
int main() {
  std::string first, second;
  std::cout << "first> " << std::flush;
  std::getline(std::cin, first);
  std::cout << "second> " << std::flush;
  std::getline(std::cin, second);
  std::cout << first << ":" << second << "\\n";
}
""",
    )
    assert started.status_code == 200, started.text
    assert started.json()["stderr"] == ""
    session_id = started.json()["session_id"]
    prompt = _wait_for(client, session_id, lambda value: "first>" in value["stdout"])
    assert prompt.json()["status"] == "RUNNING"

    first = _post(
        client,
        f"/v1/interactive-sessions/{session_id}/input",
        {"owner_key": "experiment-owner-00000001", "text": "hello"},
    )
    assert first.status_code == 200, first.text
    second_prompt = _wait_for(
        client, session_id, lambda value: "second>" in value["stdout"]
    )
    assert second_prompt.json()["status"] == "RUNNING"
    second = _post(
        client,
        f"/v1/interactive-sessions/{session_id}/input",
        {"owner_key": "experiment-owner-00000001", "text": "world"},
    )
    assert second.status_code == 200, second.text
    completed = _wait_for(client, session_id, lambda value: value["terminal"])
    assert completed.json()["status"] == "SUCCESS"
    assert completed.json()["stderr"] == ""
    assert "hello:world" in completed.json()["stdout"]

    hidden = _post(
        client,
        f"/v1/interactive-sessions/{session_id}/state",
        {"owner_key": "different-owner-000000001"},
    )
    assert hidden.status_code == 404


def test_interactive_program_reads_and_rewrites_runtime_text_data(
    client: TestClient,
) -> None:
    require_clang()
    started = _start(
        client,
        """
#include <fstream>
#include <iostream>
#include <string>
int main() {
  std::ifstream input("input.txt");
  std::string value;
  std::getline(input, value);
  if (!input) return 2;
  input.close();
  std::ofstream output("input.txt", std::ios::trunc);
  output << "rewritten";
  output.close();
  if (!output) return 3;
  std::cout << value << "\\n";
}
""",
        [{"path": "input.txt", "content": "from workspace\n"}],
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    completed = _wait_for(client, session_id, lambda value: value["terminal"])
    assert completed.json()["status"] == "SUCCESS"
    assert "from workspace" in completed.json()["stdout"]


def test_interactive_program_can_be_stopped_and_compile_errors_are_terminal(
    client: TestClient,
) -> None:
    require_clang()
    started = _start(
        client,
        """
#include <chrono>
#include <thread>
int main() { for (;;) std::this_thread::sleep_for(std::chrono::milliseconds(10)); }
""",
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    stopped = _post(
        client,
        f"/v1/interactive-sessions/{session_id}/stop",
        {"owner_key": "experiment-owner-00000001"},
    )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["terminal"] is True
    assert stopped.json()["status"] == "STOPPED"

    failed = _start(client, "int main( {\n")
    assert failed.status_code == 200, failed.text
    assert failed.json()["terminal"] is True
    assert failed.json()["status"] == "COMPILE_ERROR"
    assert failed.json()["diagnostics"]


def test_interactive_program_receives_explicit_eof_and_finishes_normally(
    client: TestClient,
) -> None:
    require_clang()
    started = _start(
        client,
        """
#include <iostream>
#include <string>
int main() {
  std::string line;
  int count = 0;
  std::cout << "lines> " << std::flush;
  while (std::getline(std::cin, line)) ++count;
  std::cout << "count=" << count << "\\n";
}
""",
    )
    assert started.status_code == 200, started.text
    session_id = started.json()["session_id"]
    assert _wait_for(client, session_id, lambda value: "lines>" in value["stdout"])
    for value in ("one", "two"):
        sent = _post(
            client,
            f"/v1/interactive-sessions/{session_id}/input",
            {"owner_key": "experiment-owner-00000001", "text": value},
        )
        assert sent.status_code == 200, sent.text

    eof = _post(
        client,
        f"/v1/interactive-sessions/{session_id}/eof",
        {"owner_key": "experiment-owner-00000001"},
    )
    assert eof.status_code == 200, eof.text
    assert eof.json()["input_closed"] is True
    completed = _wait_for(client, session_id, lambda value: value["terminal"])
    assert completed.json()["status"] == "SUCCESS"
    assert "count=2" in completed.json()["stdout"]

    after_eof = _post(
        client,
        f"/v1/interactive-sessions/{session_id}/input",
        {"owner_key": "experiment-owner-00000001", "text": "late"},
    )
    assert after_eof.status_code == 409
