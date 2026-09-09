from __future__ import annotations

import re
from typing import Any, Literal, cast

MoodleAnswerTransport = Literal[
    "ESSAY_ATTACHMENT",
    "ESSAY_ONLINE_TEXT",
    "ASSIGN_FILE",
    "ASSIGN_ONLINE_TEXT",
]
# Compatibility name retained for callers that predate Assignment delivery.
MoodleEssayAnswerTransport = MoodleAnswerTransport
MOODLE_ANSWER_TRANSPORTS = frozenset(
    {
        "ESSAY_ATTACHMENT",
        "ESSAY_ONLINE_TEXT",
        "ASSIGN_FILE",
        "ASSIGN_ONLINE_TEXT",
    }
)
MOODLE_ESSAY_ANSWER_TRANSPORTS = MOODLE_ANSWER_TRANSPORTS
_MOODLE_FILE_TYPE_SPLIT = re.compile(r"[\s,;]+")


def moodle_file_type_allowed(value: object, suffix: str) -> bool:
    """Apply the extension/MIME subset needed by generated C/C++ artifacts."""

    if not isinstance(value, str) or not value.strip():
        return True
    tokens = {token.casefold() for token in _MOODLE_FILE_TYPE_SPLIT.split(value.strip()) if token}
    if tokens.intersection({"*", "*/*"}):
        return True
    suffix = suffix.casefold()
    if suffix in tokens:
        return True
    mime_tokens = {
        ".zip": {"application/zip", "application/x-zip-compressed"},
        ".c": {"text/plain", "text/x-c", "text/x-csrc"},
        ".cpp": {"text/plain", "text/x-c++", "text/x-c++src"},
    }
    return bool(tokens.intersection(mime_tokens.get(suffix, set())))


def normalize_moodle_essay_answer_transport(
    value: object,
) -> MoodleEssayAnswerTransport | None:
    """Accept only transport values proved by the Moodle adapter.

    A missing or future value is not guessed.  It may leave local publication
    usable, but attempt projection and delivery must fall back or fail closed
    without mutating Moodle.
    """

    if not isinstance(value, str) or value not in MOODLE_ANSWER_TRANSPORTS:
        return None
    return cast(MoodleAnswerTransport, value)


normalize_moodle_answer_transport = normalize_moodle_essay_answer_transport


def confirmed_moodle_activity_answer_transport(
    activity: dict[str, Any] | None,
    *,
    module: str,
) -> MoodleAnswerTransport | None:
    """Return a transport only when Moodle proved a supported module contract."""

    if module not in {"assign", "quiz"} or not isinstance(activity, dict):
        return None
    if activity.get("import_supported") is not True:
        return None
    transport = normalize_moodle_essay_answer_transport(activity.get("answer_transport"))
    expected = (
        {"ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"}
        if module == "quiz"
        else {"ASSIGN_FILE", "ASSIGN_ONLINE_TEXT"}
    )
    return transport if transport in expected else None
