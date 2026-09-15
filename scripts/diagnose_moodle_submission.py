"""Read-only submission diagnosis, piped into an already running sync-worker.

Never retry, submit, start an attempt, change a credential or print source/cookies.
The optional HTTP probe reads only the pinned Quiz page, without executing JS.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit

import app.models  # noqa: F401 -- register all model mappings before querying
import httpx
from app.core.config import Settings
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    CredentialDecryptionError,
    decrypt_moodle_browser_state,
)
from app.db.session import create_engine, create_session_factory
from app.models.attempts import Attempt, MoodleQuizQuestion, Submission
from app.models.courses import Course
from app.models.identity import LMSConnection, MoodleCredential
from app.models.integration import SyncOutbox
from app.models.tasks import Assessment
from sqlalchemy import select, text

MAX_BODY_BYTES = 131_072
PROBE_SECONDS = 20
NAVIGATION_CODES = (
    "MOODLE_RESPONSE_TIMEOUT",
    "MOODLE_DOCUMENT_TIMEOUT",
    "MOODLE_DNS_ERROR",
    "MOODLE_CONNECTION_ERROR",
    "MOODLE_TLS_ERROR",
    "MOODLE_HTTP_ERROR",
    "MOODLE_NAVIGATION_ERROR",
)


def emit(value):
    print(json.dumps(value, default=str, ensure_ascii=False, indent=2), flush=True)


def future(value):
    return value is not None and value.replace(tzinfo=value.tzinfo or UTC) > datetime.now(UTC)


def checkpoint_metadata(row):
    result = dict(row)
    detail = result.pop("last_error") or ""
    code = detail.partition(":")[0]
    result["last_error_code"] = code if re.fullmatch(r"[A-Z][A-Z_0-9]{0,79}", code) else None
    result["navigation_error_code"] = next(
        (code for code in NAVIGATION_CODES if code in detail), None
    )
    # Do not print arbitrary exception text from older deployed connectors.
    return result


def probe_target(base_url, policy, state):
    """Do not forward session state to another origin or accept an arbitrary URL."""
    base = urlsplit(base_url)
    if (
        base.scheme != "https"
        or not base.hostname
        or base.username
        or base.password
        or base.path not in {"", "/"}
        or base.query
        or base.fragment
    ):
        raise ValueError("HTTPS Moodle origin required")
    # Accessing .port also rejects malformed ports before selecting a cookie.
    _ = base.port
    ids = [str(policy.get(key, "")) for key in ("moodle_attempt_id", "moodle_cmid")]
    if not all(re.fullmatch(r"[1-9][0-9]{0,18}", value) for value in ids):
        raise ValueError("Pinned Quiz attempt required")
    cookies = state.get("cookies", [])
    if not isinstance(cookies, list):
        raise ValueError("Invalid session cookies")
    matches = []
    for cookie in cookies:
        if not isinstance(cookie, dict) or cookie.get("name") != "MoodleSession":
            continue
        if str(cookie.get("domain", "")).lstrip(".").lower() != base.hostname.lower():
            continue
        # The connector stores cookies for an exact origin; do not broaden path scope.
        path = cookie.get("path", "/")
        target_path = "/mod/quiz/attempt.php"
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or not (target_path == path or target_path.startswith(path.rstrip("/") + "/"))
        ):
            continue
        value, expires = cookie.get("value", ""), cookie.get("expires", -1)
        if not isinstance(expires, int | float) or (expires != -1 and expires <= time.time()):
            continue
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9,._~-]{1,1024}", value):
            continue
        matches.append(value)
    if len(matches) != 1:
        raise ValueError("One unexpired MoodleSession cookie required")
    query = urlencode({"attempt": ids[0], "cmid": ids[1], "page": 0})
    return f"{base_url.rstrip('/')}/mod/quiz/attempt.php?{query}", matches[0]


async def probe_session(base_url, policy, state, *, transport=None):
    try:
        url, session_cookie = probe_target(base_url, policy, state)
    except ValueError:
        return {"skipped": "invalid_https_origin_binding_or_session"}
    started = time.monotonic()
    result = {"method": "GET", "target": "pinned_quiz_attempt", "javascript": False}
    try:
        async with (
            asyncio.timeout(PROBE_SECONDS),
            httpx.AsyncClient(
                timeout=PROBE_SECONDS,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client,
        ):
            async with client.stream(
                "GET",
                url,
                headers={"Cookie": f"MoodleSession={session_cookie}", "Accept": "text/html"},
            ) as response:
                result.update(
                    http_status=response.status_code,
                    headers_seconds=round(time.monotonic() - started, 2),
                )
                # Never follow redirects (including login); neither their URL nor
                # arbitrary error HTML is safe diagnostic output.
                if response.is_redirect:
                    location = urlsplit(response.headers.get("location", ""))
                    result["redirect_to_login"] = location.path == "/login/index.php"
                    return result
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    body.extend(chunk[: MAX_BODY_BYTES - len(body)])
                    if len(body) >= MAX_BODY_BYTES:
                        break
                result.update(
                    bytes_read=len(body),
                    body_limit_reached=len(body) >= MAX_BODY_BYTES,
                    total_seconds=round(time.monotonic() - started, 2),
                    quiz_attempt_markup=b"page-mod-quiz-attempt" in body,
                    login_markup=b"page-login-index" in body,
                )
    except (TimeoutError, httpx.HTTPError) as exc:
        result.update(
            error_type=type(exc).__name__, total_seconds=round(time.monotonic() - started, 2)
        )
    return result


async def read_attempt_report(db, attempt_id):
    attempt = await db.get(Attempt, attempt_id)
    if attempt is None:
        raise ValueError("attempt_not_found")
    root_id = await db.scalar(
        select(MoodleQuizQuestion.root_attempt_id).where(
            MoodleQuizQuestion.attempt_id == attempt.id,
        )
    )
    root = await db.get(Attempt, root_id) if root_id else attempt
    if root is None or root.principal_id != attempt.principal_id:
        raise ValueError("invalid_quiz_root")
    connection = await db.scalar(
        select(LMSConnection)
        .join(
            Course,
            Course.connection_id == LMSConnection.id,
        )
        .join(Assessment, Assessment.course_id == Course.id)
        .where(
            Assessment.id == root.assessment_id,
        )
    )
    if connection is None:
        raise ValueError("connection_not_found")
    credential = await db.scalar(
        select(MoodleCredential).where(
            MoodleCredential.connection_id == connection.id,
            MoodleCredential.principal_id == root.principal_id,
            MoodleCredential.kind == BROWSER_STATE_CREDENTIAL_KIND,
        )
    )
    # Select metadata only: checkpoint payloads and snapshots contain student code.
    columns = [
        getattr(SyncOutbox, name)
        for name in (
            "id",
            "state",
            "attempts",
            "created_at",
            "last_attempt_at",
            "next_attempt_at",
            "delivered_at",
            "locked_at",
            "last_error",
        )
    ]
    rows = await db.execute(
        select(
            *columns,
            SyncOutbox.payload["reason"].as_string().label("reason"),
        )
        .where(
            SyncOutbox.attempt_id == root.id,
            SyncOutbox.connection_id == connection.id,
            SyncOutbox.event_type == "attempt.checkpoint",
        )
        .order_by(SyncOutbox.created_at.desc())
        .limit(12)
    )
    report = {
        "attempt_id": attempt.id,
        "root_attempt_id": root.id,
        "state": root.state,
        "submitted_at": root.submitted_at,
        "final_snapshot_present": await db.scalar(
            select(Submission.id)
            .where(
                Submission.attempt_id == root.id,
            )
            .limit(1)
        )
        is not None,
        "moodle_binding": {
            key: root.integrity_policy.get(key)
            for key in (
                "moodle_course_id",
                "moodle_cmid",
                "moodle_attempt_id",
            )
        },
        "checkpoints": [checkpoint_metadata(row) for row in rows.mappings()],
        "credential": None
        if credential is None
        else {
            "status": credential.status,
            "revision": credential.revision,
            "expires_at": credential.expires_at,
            "last_verified_at": credential.last_verified_at,
            "lease_expires_at": credential.lease_expires_at,
        },
    }
    return report, root.integrity_policy, connection, credential


async def diagnose(attempt_id):
    # Even if SQL debugging was enabled in the deployment, never log credentials.
    logging.disable(logging.CRITICAL)
    settings = Settings(db_echo=False)
    engine = create_engine(settings)
    try:
        async with create_session_factory(engine)() as db:
            if engine.dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION READ ONLY"))
                await db.execute(text("SET LOCAL statement_timeout = '5s'"))
            report, policy, connection, credential = await read_attempt_report(db, attempt_id)
        report["runtime"] = {
            name: getattr(settings, name)
            for name in (
                "moodle_browser_http_timeout_seconds",
                "sync_lease_seconds",
                "sync_max_attempts",
                "sync_retry_base_seconds",
                "sync_poll_seconds",
            )
        }
        emit(report)
        # Skip a known in-flight write rather than add another request to its session.
        if credential is None or credential.status != "ACTIVE" or credential.revoked_at:
            probe = {"skipped": "no_active_student_browser_session"}
        elif future(credential.lease_expires_at):
            probe = {"skipped": "student_session_in_use"}
        elif credential.expires_at is not None and not future(credential.expires_at):
            probe = {"skipped": "student_browser_session_expired"}
        else:
            try:
                state = decrypt_moodle_browser_state(
                    credential.encrypted_secret,
                    settings,
                    connection_id=connection.id,
                    principal_id=credential.principal_id,
                )
            except CredentialDecryptionError:
                probe = {"skipped": "student_browser_session_cannot_be_decrypted"}
            else:
                probe = await probe_session(connection.base_url, policy, state)
        emit({"saved_student_session_probe": probe})
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("attempt_id", type=uuid.UUID)
    args = parser.parse_args()
    try:
        asyncio.run(diagnose(args.attempt_id))
    except Exception as exc:
        # Driver/validation exceptions may include connection strings or SQL parameters.
        emit({"diagnostic_error_type": type(exc).__name__})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
