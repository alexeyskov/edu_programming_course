"""Deployment boundaries without starting containers or reading operator secrets."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not shutil.which("docker"), reason="Docker Compose CLI unavailable")
@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("isolation", [None, "landlock"])
def test_clangd_is_private_optional_and_resource_bounded(local, isolation):
    environment = {
        "PATH": os.environ["PATH"],
        "APP_SECRET_KEY": "deployment-test-secret",
        "DATABASE_URL": "postgresql://test:test@postgres/test",
        "POSTGRES_DB": "test",
        "POSTGRES_USER": "test",
        "POSTGRES_PASSWORD": "deployment-test-password",
        "RUNNER_SHARED_SECRET": "deployment-test-runner-secret",
        "MOODLE_BROWSER_SHARED_SECRET": "deployment-test-browser-secret",
    }
    if not local:
        environment["CODE_INTELLIGENCE_SHARED_SECRET"] = "deployment-test-language-secret-32"
    if isolation:
        environment["CODE_INTELLIGENCE_ISOLATION"] = isolation
    command = ["docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "compose.yml")]
    if local:
        command.extend(["-f", str(ROOT / "compose.local.yml")])
    result = subprocess.run(
        [*command, "config", "--format", "json"],
        env=environment,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)
    services = config["services"]
    sidecar = services["code-intelligence"]
    assert sidecar["environment"]["CODE_INTELLIGENCE_ISOLATION"] == (isolation or "container")
    backend = services["backend"]
    assert not sidecar.get("ports") and not sidecar.get("volumes")
    assert sidecar["read_only"] is True
    assert sidecar["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in sidecar["security_opt"]
    assert sidecar["pids_limit"] == 128
    assert int(sidecar["mem_limit"]) == 2 * 1024**3
    assert float(sidecar["cpus"]) == 1
    assert set(sidecar["networks"]) == {"language-rpc"}
    assert config["networks"]["language-rpc"]["internal"] is True
    connected = {
        name for name, service in services.items() if "language-rpc" in service.get("networks", {})
    }
    assert connected == {"backend", "code-intelligence"}
    assert "code-intelligence" not in backend.get("depends_on", {})
    key = sidecar["environment"]["CODE_INTELLIGENCE_SHARED_SECRET"]
    assert len(key) >= 32
    assert key == backend["environment"]["CODE_INTELLIGENCE_SHARED_SECRET"]
    assert backend["environment"]["CODE_INTELLIGENCE_URL"] == "http://code-intelligence:8084"
    assert all(name.startswith("CODE_INTELLIGENCE_") for name in sidecar["environment"])
    for name, service in services.items():
        if name not in {"backend", "code-intelligence"}:
            assert "CODE_INTELLIGENCE_SHARED_SECRET" not in service.get("environment", {})


@pytest.mark.parametrize("existing", [False, True])
def test_launcher_generates_language_key_once_without_disclosing_it(tmp_path, existing):
    runtime = tmp_path / "runtime.env"
    original = (
        "APP_SECRET_KEY=existing-app-key\nRUNNER_SHARED_SECRET=existing-runner-key\n"
        "MOODLE_CREDENTIAL_ENCRYPTION_KEY=existing-credential-key\n"
        "MOODLE_BROWSER_SHARED_SECRET=existing-browser-key\n"
    )
    if existing:
        runtime.write_text(original)
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    docker = executable_dir / "docker"
    docker.write_text('#!/bin/sh\nif [ "$1" = "volume" ]; then exit 1; fi\nexit 0\n')
    docker.chmod(0o700)
    environment = {
        "PATH": f"{executable_dir}:{os.environ['PATH']}",
        "EDUPROG_ENV_FILE": str(runtime),
        "EDUPROG_HOST_ENV_FILE": str(tmp_path / "absent-host.env"),
        "EDUPROG_BASHRC_FILE": str(tmp_path / "absent-bashrc"),
        "DBLOGIN": "test-user",
        "DBPASSWORD": "test-password",
    }
    previous = None
    for _ in range(2):
        result = subprocess.run(
            ["bash", str(ROOT / "run_eduprog.sh"), "diagnose-code-intelligence"],
            env=environment,
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        values = runtime.read_text()
        if existing:
            assert values.startswith(original)
        lines = [
            line
            for line in values.splitlines()
            if line.startswith("CODE_INTELLIGENCE_SHARED_SECRET=")
        ]
        assert len(lines) == 1
        key = lines[0].partition("=")[2]
        assert len(key) == 64 and all(character in "0123456789abcdef" for character in key)
        assert key not in result.stdout + result.stderr
        assert runtime.stat().st_mode & 0o777 == 0o600
        if previous:
            assert values == previous
        previous = values
