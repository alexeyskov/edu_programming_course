"""Strict evidence for attempts deleted from Moodle, without changing Moodle.

Moodle's review pages deliberately hide the underlying exception behind
``attempterrorcontentchange``. That also covers broken questions or DB failures,
so neither it nor report absence proves deletion. The AJAX function below is
declared ``type=read`` in Moodle 4.2+ and only computes confirmation text. It
does NOT reopen an attempt. Its direct quiz_attempt::create call preserves the
missing-record error. Moodle 5.2 hides the table name with debugging disabled;
that generic error needs corroboration from a complete, unfiltered report.

Sources: mod/quiz/db/services.php, classes/external/get_reopen_attempt_confirmation.php,
lib/dmllib.php and lib/external/classes/external_api.php in upstream Moodle.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from .historical import HistoricalIndexPage, parse_quiz_report_page
from .parsers import MoodleMarkupError

READ_ATTEMPT_METHOD = "mod_quiz_get_reopen_attempt_confirmation"
ALL_ATTEMPT_STATES = "notstarted-inprogress-overdue-submitted-finished-abandoned"


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


def unknown_missing_record_response(payload: Any) -> bool:
    """A corroboration candidate, NOT sufficient deletion evidence by itself."""
    if not isinstance(payload, list) or len(payload) != 1:
        return False
    result = payload[0]
    if not isinstance(result, dict) or result.get("error") is not True:
        return False
    error = result.get("exception")
    if not isinstance(error, dict) or error.get("errorcode") != "invalidrecordunknown":
        return False
    info = error.get("moreinfourl")
    message = error.get("message")
    if not isinstance(info, str) or not isinstance(message, str) or len(message) > 2_000:
        return False
    try:
        return urlsplit(info).path.rstrip("/").endswith("/error/moodle/invalidrecordunknown")
    except ValueError:
        return False


def parse_unfiltered_attempt_report(
    markup: str, *, base_url: str, course_id: str, cmid: int, page_number: int
) -> HistoricalIndexPage:
    """Validate rendered filters, not just requested URL parameters.

    all_with includes unenrolled users and is only available to teachers with
    access to all groups. Moodle silently replaces it for restricted teachers.
    Never use such a restricted, filtered, malformed or partially parsed report
    to corroborate a missing record. Every page must pass the same checks.
    """
    soup = BeautifulSoup(markup, "html.parser")

    def reject() -> None:
        raise MoodleMarkupError("Moodle deletion report is not demonstrably unfiltered")

    selectors = soup.select("select[name='attempts']")
    if len(selectors) != 1:
        reject()
    selected = selectors[0].select("option[selected]")
    if len(selected) != 1 or selected[0].get("value") != "all_with":
        reject()
    form = selectors[0].find_parent("form")
    if form is None or soup.select_one(
        ".errorbox, .errormessage, .alert-danger, .alert-error, input[type='password']"
    ):
        reject()
    for name, expected in (("id", str(cmid)), ("mode", "overview")):
        values = {str(node.get("value", "")) for node in form.select(f"input[name='{name}']")}
        if values != {expected}:
            reject()
    for group in soup.select("select[name='group']"):
        options = group.select("option[selected]")
        if len(options) != 1 or options[0].get("value") != "0":
            reject()
    if any(node.get("value") not in ("", "0") for node in soup.select("input[name='group']")):
        reject()
    states = form.select("input[type='checkbox'][name^='state']")
    if not {"stateinprogress", "stateoverdue", "statefinished", "stateabandoned"}.issubset(
        {node.get("name") for node in states}
    ) or any(not node.has_attr("checked") for node in states):
        reject()
    for node in soup.select("input[name='onlygraded'], input[name='onlyregraded']"):
        if node.get("type") == "checkbox":
            if node.has_attr("checked"):
                reject()
        elif node.get("value", "") not in ("", "0"):
            reject()
    for selector in (".initialbar.firstinitial", ".initialbar.lastinitial"):
        bars = soup.select(selector)
        if len(bars) != 1:
            reject()
        active = bars[0].select(".active")
        if len(active) != 1 or "initialbarall" not in active[0].get("class", []):
            reject()
    result = parse_quiz_report_page(
        markup, base_url=base_url, course_id=course_id, cmid=cmid, page_number=page_number
    )
    if result.skipped_rows:
        reject()
    table = soup.select_one("table#attempts")
    if table is not None:
        # all_with must contain an identified attempt for every student row;
        # the import parser also supports enrolled_without and can otherwise
        # legitimately skip students without attempts. Not evidence here.
        student_rows = [row for row in table.select("tbody tr") if row.select_one(
            "a[href*='/user/view.php'], a[href*='/user/profile.php']"
        )]
        if not result.items or len(student_rows) != len(result.items):
            reject()
    return result
