from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import event

from app.models.courses import Course
from app.models.identity import MoodleCredential
from app.models.integration import SyncOutbox
from app.services.workspace import submit_attempt
from tests.test_attempt_review_services import _start_multi_quiz

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/diagnose_moodle_submission.py"
spec = importlib.util.spec_from_file_location("submission_diagnostic", SCRIPT)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)

BASE = "https://moodle.example.test"
SECRET = "private-student-cookie"
POLICY = {"moodle_attempt_id": "141828", "moodle_cmid": 31529}


def state(**overrides):
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": SECRET,
                "domain": "moodle.example.test",
                "path": "/",
                "expires": -1,
                **overrides,
            }
        ]
    }


async def test_probe_is_one_bounded_get_without_javascript_or_outputting_answers():
    calls = []

    def handle(request):
        calls.append(request)
        assert request.method == "GET"
        assert request.url == BASE + "/mod/quiz/attempt.php?attempt=141828&cmid=31529&page=0"
        assert request.headers["Cookie"] == "MoodleSession=" + SECRET
        return httpx.Response(
            200,
            content=(b'<body id="page-mod-quiz-attempt">private student source' + b"x" * 150_000),
        )

    result = await diagnostic.probe_session(
        BASE, POLICY, state(), transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1
    assert result["http_status"] == 200 and result["quiz_attempt_markup"] is True
    assert result["bytes_read"] == diagnostic.MAX_BODY_BYTES
    assert result["body_limit_reached"] is True
    assert SECRET not in json.dumps(result) and "private student source" not in json.dumps(result)


@pytest.mark.parametrize(
    "location,is_login",
    [
        ("/login/index.php?secret=value", True),
        ("https://foreign.test/?secret=value", False),
    ],
)
async def test_probe_does_not_follow_redirects_or_print_their_location(location, is_login):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location})

    result = await diagnostic.probe_session(
        BASE, POLICY, state(), transport=httpx.MockTransport(handle)
    )
    assert len(calls) == 1 and result["redirect_to_login"] is is_login
    assert "secret" not in json.dumps(result) and "foreign.test" not in json.dumps(result)


@pytest.mark.parametrize(
    "base,policy,cookies",
    [
        ("http://moodle.example.test", POLICY, state()),
        ("https://user:password@moodle.example.test", POLICY, state()),
        ("https://foreign.test", POLICY, state()),
        (BASE, {**POLICY, "moodle_attempt_id": "0&delete=1"}, state()),
        (BASE, POLICY, state(domain="foreign.test")),
        (BASE, POLICY, state(path="/mod/qui")),
        (BASE, POLICY, state(expires=time.time() - 60)),
        (BASE, POLICY, state(value="bad\r\nCookie: extra")),
    ],
)
async def test_probe_rejects_invalid_target_or_session_before_network(base, policy, cookies):
    def forbidden(_request):
        pytest.fail("unsafe diagnostic network request")

    result = await diagnostic.probe_session(
        base, policy, cookies, transport=httpx.MockTransport(forbidden)
    )
    assert "skipped" in result


async def test_probe_timeout_does_not_print_sensitive_exception_text():
    def handle(_request):
        raise httpx.ReadTimeout("private URL and cookie: " + SECRET)

    result = await diagnostic.probe_session(
        BASE, POLICY, state(), transport=httpx.MockTransport(handle)
    )
    assert result["error_type"] == "ReadTimeout"
    assert SECRET not in json.dumps(result)


def test_error_metadata_excludes_arbitrary_error_text():
    result = diagnostic.checkpoint_metadata(
        {
            "last_error": "UNAVAILABLE: MOODLE_DOCUMENT_TIMEOUT: private-cookie=" + SECRET,
        }
    )
    assert result == {
        "last_error_code": "UNAVAILABLE",
        "navigation_error_code": "MOODLE_DOCUMENT_TIMEOUT",
    }


async def test_metadata_finds_root_from_second_question_and_never_reads_answers_or_writes(db):
    student, _, assessment, root, questions = await _start_multi_quiz(db)
    await submit_attempt(
        db,
        attempt_id=root.id,
        principal_id=student.id,
        expected_revision=0,
    )
    course = await db.get(Course, assessment.course_id)
    db.add(
        MoodleCredential(
            connection_id=course.connection_id,
            principal_id=student.id,
            kind=diagnostic.BROWSER_STATE_CREDENTIAL_KIND,
            encrypted_secret=SECRET,
            lease_expires_at=datetime.now(UTC) + timedelta(seconds=60),
        )
    )
    db.add(
        SyncOutbox(
            connection_id=course.connection_id,
            course_id=course.id,
            attempt_id=root.id,
            event_type="moodle.history.import",
            aggregate_type="Assessment",
            aggregate_id=assessment.id,
            idempotency_key="not-a-checkpoint",
        )
    )
    await db.flush()
    statements = []

    def capture(_connection, _cursor, statement, *_args):
        statements.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        report, _, _, credential = await diagnostic.read_attempt_report(
            db, questions[-1].attempt_id
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert report["root_attempt_id"] == root.id
    assert report["final_snapshot_present"] is True and report["state"] == "SUBMITTED"
    assert report["checkpoints"] and all(
        row["reason"] == "SUBMISSION" for row in report["checkpoints"]
    )
    assert credential.encrypted_secret == SECRET  # used internally, never part of the report
    assert SECRET not in json.dumps(report, default=str)
    assert all(sql.lstrip().upper().startswith("SELECT") for sql in statements)
    assert not any("core_snapshot" in sql or "core_workspacefile" in sql for sql in statements)
    assert not db.dirty and not db.new and not db.deleted


def test_launcher_runs_script_in_existing_worker_without_env_creation_or_restart(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "commands"
    captured_script = tmp_path / "script"
    docker = fake_bin / "docker"
    docker.write_text("""#!/bin/sh
printf '%s\n' "$*" >> "$EDUPROG_TEST_COMMANDS"
if [ "$1" = "ps" ]; then printf '%s\n' 123456abcdef; exit 0; fi
if [ "$1" = "exec" ]; then cat > "$EDUPROG_TEST_SCRIPT"; exit 0; fi
exit 1
""")
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)
    runtime = tmp_path / "absent-runtime.env"
    attempt_id = str(uuid.uuid4())
    result = subprocess.run(
        ["bash", str(ROOT / "run_eduprog.sh"), "diagnose-moodle-submission", attempt_id],
        env={
            **os.environ,
            "PATH": str(fake_bin) + ":" + os.environ["PATH"],
            "EDUPROG_ENV_FILE": str(runtime),
            "EDUPROG_HOST_ENV_FILE": str(tmp_path / "absent"),
            "EDUPROG_TEST_COMMANDS": str(capture),
            "EDUPROG_TEST_SCRIPT": str(captured_script),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert capture.read_text().splitlines() == [
        "ps -q --filter label=com.docker.compose.project=eduprog "
        "--filter label=com.docker.compose.service=sync-worker",
        "exec -i 123456abcdef python - " + attempt_id,
    ]
    assert captured_script.read_text() == SCRIPT.read_text()
    assert not runtime.exists()
