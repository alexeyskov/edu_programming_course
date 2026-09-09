from __future__ import annotations

import json
import secrets
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from edu_runner.app import create_app
from edu_runner.config import Settings
from edu_runner.executor import LocalExecutor
from edu_runner.security import sign_body
from edu_runner.service import RunnerService


TEST_SECRET = b"test-runner-secret-that-is-long-enough-0001"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        shared_secret=TEST_SECRET,
        work_root=tmp_path / "jobs",
        queue_wait_seconds=0.05,
    )


@pytest.fixture
def service(settings: Settings) -> RunnerService:
    return RunnerService(settings, executor=LocalExecutor())


@pytest.fixture
def client(settings: Settings, service: RunnerService) -> Iterator[TestClient]:
    with TestClient(create_app(settings, service=service)) as value:
        yield value


def signed_headers(body: bytes, *, nonce: str | None = None) -> dict[str, str]:
    timestamp = str(int(time.time()))
    actual_nonce = nonce or secrets.token_hex(16)
    return {
        "Content-Type": "application/json",
        "X-Runner-Timestamp": timestamp,
        "X-Runner-Nonce": actual_nonce,
        "X-Runner-Signature": sign_body(TEST_SECRET, timestamp, actual_nonce, body),
    }


def post_job(client: TestClient, payload: dict[str, object]):
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    return client.post("/v1/jobs", content=body, headers=signed_headers(body))


def require_clang() -> None:
    if shutil.which("clang++") is None:
        pytest.skip("clang++ is required for integration tests")
