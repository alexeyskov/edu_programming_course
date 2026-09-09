from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_launcher_maps_openrouter_aliases_and_compose_forwards_ai_environment(
    tmp_path: Path,
) -> None:
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "\n".join(
            (
                "APP_SECRET_KEY=test-app-secret",
                "RUNNER_SHARED_SECRET=test-runner-secret",
                "MOODLE_CREDENTIAL_ENCRYPTION_KEY=test-credential-secret",
                "MOODLE_BROWSER_SHARED_SECRET=test-browser-secret",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    capture = tmp_path / "ai-environment.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "volume" ] && [ "$2" = "inspect" ]; then exit 1; fi
if [ "$1" = "compose" ]; then
  printf '%s\n%s\n%s\n%s\n%s\n' \
    "$AI_ENABLED" "$AI_BASE_URL" "$AI_API_KEY" "$AI_MODEL" "$AI_API_STYLE" \
    > "$EDUPROG_CAPTURE_FILE"
  exit 0
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)

    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "EDUPROG_ENV_FILE": str(runtime_env),
            "EDUPROG_CAPTURE_FILE": str(capture),
            "DBLOGIN": "test-db-user",
            "DBPASSWORD": "test-db-password",
            "OPENROUTER_API_KEY": "openrouter-test-key",
            "OPENROUTER_MODEL": "test/vendor-model",
        }
    )
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "run_eduprog.sh"), "config"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert capture.read_text(encoding="utf-8").splitlines() == [
        "true",
        "https://openrouter.ai/api/v1",
        "openrouter-test-key",
        "test/vendor-model",
        "chat_completions",
    ]

    compose = (PROJECT_ROOT / "compose.yml").read_text(encoding="utf-8")
    assert "AI_API_KEY: ${AI_API_KEY:-}" in compose
    assert "AI_MODEL: ${AI_MODEL:-gpt-5-mini}" in compose
    assert "environment: *backend-environment" in compose
    assert "RUNNER_EXECUTOR: local" in compose
    assert "RUNNER_ALLOW_LOCAL_FALLBACK" not in compose
    assert "seccomp=unconfined" not in compose
    assert "apparmor=unconfined" not in compose

    runner_dockerfile = (PROJECT_ROOT / "runner" / "Dockerfile").read_text(encoding="utf-8")
    assert "RUNNER_EXECUTOR=local" in runner_dockerfile
    assert "bubblewrap" not in runner_dockerfile

    diagnostic = subprocess.run(
        ["bash", str(PROJECT_ROOT / "run_eduprog.sh"), "diagnose-ai"],
        cwd=PROJECT_ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert diagnostic.returncode == 0, diagnostic.stderr
    assert "AI_ENABLED=true" in diagnostic.stdout
    assert "AI_MODEL=test/vendor-model" in diagnostic.stdout
    assert "AI_API_STYLE=chat_completions" in diagnostic.stdout
    assert "AI_API_KEY=configured" in diagnostic.stdout
    assert "openrouter-test-key" not in diagnostic.stdout
    assert "openrouter-test-key" not in diagnostic.stderr
