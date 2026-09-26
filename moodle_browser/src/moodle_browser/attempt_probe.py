"""Strict evidence for attempts deleted from Moodle, without changing Moodle.

Moodle's review pages deliberately hide the underlying exception behind
``attempterrorcontentchange``. That also covers broken questions or DB failures,
so neither it nor report absence proves deletion. The AJAX function below is
declared ``type=read`` in Moodle 4.2+ and only computes confirmation text. It
does NOT reopen an attempt. Its direct quiz_attempt::create call preserves the
missing-record error and the table name in Moodle's localized error message.

Sources: mod/quiz/db/services.php, classes/external/get_reopen_attempt_confirmation.php,
lib/dmllib.php and lib/external/classes/external_api.php in upstream Moodle.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

READ_ATTEMPT_METHOD = "mod_quiz_get_reopen_attempt_confirmation"


def teacher_session_key(markup: str, *, base_url: str) -> str | None:
    """Read a session key from server-rendered controls, never evaluate scripts."""
    soup = BeautifulSoup(markup, "html.parser")
    keys = {str(node.get("value", "")) for node in soup.select("input[name='sesskey']")}
    for anchor in soup.select("a[href]"):
        try:
            parsed = urlsplit(str(anchor.get("href", "")))
        except ValueError:
            continue
        if f"{parsed.scheme}://{parsed.netloc}" == base_url and parsed.path == "/login/logout.php":
            keys.update(parse_qs(parsed.query).get("sesskey", []))
    keys.discard("")
    if len(keys) != 1:
        return None
    key = next(iter(keys))
    return key if re.fullmatch(r"[A-Za-z0-9]{6,128}", key) else None


def missing_quiz_attempt_response(payload: Any) -> bool:
    """Accept only the pinned read function's exact missing quiz_attempts record.

    The JSON ``exception`` object is Moodle's get_exception_info output, not
    arbitrary HTML submitted by a student. Generic errors, absent rows, failed
    permissions, missing external functions and missing question records are
    deliberately not interpreted as a deleted Quiz attempt.
    """
    if not isinstance(payload, list) or len(payload) != 1:
        return False
    result = payload[0]
    if not isinstance(result, dict) or result.get("error") is not True:
        return False
    error = result.get("exception")
    if not isinstance(error, dict) or error.get("errorcode") != "invalidrecord":
        return False
    info_url = error.get("moreinfourl")
    message = error.get("message")
    if not isinstance(info_url, str) or not isinstance(message, str) or len(message) > 2_000:
        return False
    try:
        error_path = urlsplit(info_url).path.rstrip("/")
    except ValueError:
        return False
    if not error_path.endswith("/error/moodle/invalidrecord"):
        return False
    text = BeautifulSoup(message, "html.parser").get_text(" ", strip=True)
    return re.search(r"(?<![A-Za-z0-9_])quiz_attempts(?![A-Za-z0-9_])", text) is not None
