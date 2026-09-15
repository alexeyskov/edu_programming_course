from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

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
            "EDUPROG_HOST_ENV_FILE": str(tmp_path / "absent-host.env"),
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
    assert "LLM_API_KEY: ${LLM_API_KEY-${AI_API_KEY:-${OPENROUTER_API_KEY:-}}}" in compose
    assert "LLM_MODEL: ${LLM_MODEL:-${AI_MODEL:-${OPENROUTER_MODEL:-gpt-5-mini}}}" in compose
    assert (
        "LLM_API_ADDRESS: ${LLM_API_ADDRESS:-${AI_BASE_URL:-https://api.openai.com/v1}}" in compose
    )
    assert "LLM_THINKING: ${LLM_THINKING:-}" in compose
    assert '"host.docker.internal:host-gateway"' in compose
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
    assert "LLM_MODEL=test/vendor-model" in diagnostic.stdout
    assert "AI_API_STYLE=chat_completions" in diagnostic.stdout
    assert "LLM_API_KEY=configured" in diagnostic.stdout
    assert "openrouter-test-key" not in diagnostic.stdout
    assert "openrouter-test-key" not in diagnostic.stderr


@pytest.mark.parametrize("thinking", ["0", "1"])
@pytest.mark.parametrize("api_key", ["", "local-gateway-test-key"])
def test_launcher_loads_new_host_settings_without_exposing_keys(tmp_path, thinking, api_key):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text(
        "APP_SECRET_KEY=test-secret\nRUNNER_SHARED_SECRET=test-secret\n"
        "MOODLE_CREDENTIAL_ENCRYPTION_KEY=test-secret\nMOODLE_BROWSER_SHARED_SECRET=test-secret\n"
    )
    host_env = tmp_path / "cpp_markup.env"
    host_env.write_text(
        'DBLOGIN="db-user"\nDBPASSWORD="db-pass"\n'
        'LLM_API_ADDRESS="http://host.docker.internal:11434/api"\n'
        f'LLM_API_KEY="{api_key}"\nLLM_MODEL="Maternion/minicpm5:2b-q4_K_M"\n'
        f"LLM_THINKING={thinking}\n"
        'MOODLE_SYNC_TIMEOUT="600"\n'
        'AI_API_KEY="stale-ai-secret"\nAI_MODEL="stale-ai-model"\n'
        'OPENROUTER_API_KEY="stale-router-secret"\nOPENROUTER_MODEL="stale-router-model"\n'
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "captured.env"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        """#!/bin/sh
if [ "$1" = "compose" ] && [ "$2" = "version" ]; then exit 0; fi
if [ "$1" = "volume" ]; then exit 1; fi
printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n' \\
  "$LLM_API_ADDRESS" "$LLM_API_KEY" "$LLM_MODEL" "$LLM_THINKING" \\
  "$AI_ENABLED" "$AI_API_STYLE" "$MOODLE_SYNC_TIMEOUT" > "$EDUPROG_CAPTURE_FILE"
"""
    )
    fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)
    environment = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("LLM_", "AI_", "OPENROUTER_")) and name != "MOODLE_SYNC_TIMEOUT"
    }
    environment.update(
        {
            "PATH": f"{fake_bin}:{environment['PATH']}",
            "EDUPROG_ENV_FILE": str(runtime_env),
            "EDUPROG_HOST_ENV_FILE": str(host_env),
            "EDUPROG_CAPTURE_FILE": str(capture),
        }
    )
    result = subprocess.run(
        ["bash", str(PROJECT_ROOT / "run_eduprog.sh"), "diagnose-ai"],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "http://host.docker.internal:11434/api",
        api_key,
        "Maternion/minicpm5:2b-q4_K_M",
        thinking,
        "true",
        "auto",
        "600",
    ]
    assert f"LLM_THINKING={thinking}" in result.stdout
    assert "LLM_API_KEY=" + ("configured" if api_key else "not configured") in result.stdout
    for secret in (api_key, "stale-ai-secret", "stale-router-secret"):
        if secret:
            assert secret not in result.stdout and secret not in result.stderr


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is optional locally")
@pytest.mark.parametrize("new_names", [False, True])
def test_real_compose_interpolation_of_provider_settings(new_names):
    environment = {
        "PATH": os.environ["PATH"],
        "APP_SECRET_KEY": "test-only-secret",
        "DATABASE_URL": "postgresql://test:test@postgres/test",
        "POSTGRES_DB": "test",
        "POSTGRES_USER": "test",
        "POSTGRES_PASSWORD": "test",
        "RUNNER_SHARED_SECRET": "test-only-secret",
        "MOODLE_BROWSER_SHARED_SECRET": "test-only-secret",
        "AI_ENABLED": "true",
        "AI_BASE_URL": "https://old.example/v1",
        "AI_API_KEY": "old-test-key",
        "AI_MODEL": "old-test-model",
        "AI_API_STYLE": "responses",
        "MOODLE_SYNC_TIMEOUT": "600",
    }
    if new_names:
        environment.update(
            {
                "LLM_API_ADDRESS": "http://host.docker.internal:11434/api",
                "LLM_API_KEY": "",
                "LLM_MODEL": "local/model",
                "LLM_THINKING": "0",
                "AI_API_STYLE": "auto",
            }
        )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            "/dev/null",
            "-f",
            str(PROJECT_ROOT / "compose.yml"),
            "config",
            "--format",
            "json",
        ],
        env=environment,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    for name in ("backend", "sync-worker", "deadline-worker"):
        assert services[name]["environment"]["MOODLE_SYNC_TIMEOUT"] == "600"
    for name in ("backend",):
        config = services[name]["environment"]
        assert config["LLM_API_ADDRESS"] == environment.get(
            "LLM_API_ADDRESS", environment["AI_BASE_URL"]
        )
        assert config["LLM_API_KEY"] == ("" if new_names else "old-test-key")
        assert config["LLM_MODEL"] == ("local/model" if new_names else "old-test-model")
        assert config["LLM_THINKING"] == ("0" if new_names else "")
        assert config["AI_API_STYLE"] == ("auto" if new_names else "responses")
        assert "host.docker.internal=host-gateway" in services[name]["extra_hosts"]
    for name in ("frontend", "runner", "sync-worker", "deadline-worker", "moodle-browser"):
        assert "LLM_API_KEY" not in services[name].get("environment", {})
