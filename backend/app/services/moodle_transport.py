"""Deprecated import path for Moodle adapter transport helpers.

New code should import from :mod:`app.integrations.moodle_transport`; this
module remains as a compatibility shim for extensions using the old path.
"""

from app.integrations.moodle_transport import (
    MOODLE_ANSWER_TRANSPORTS,
    MOODLE_ESSAY_ANSWER_TRANSPORTS,
    MoodleAnswerTransport,
    MoodleEssayAnswerTransport,
    confirmed_moodle_activity_answer_transport,
    moodle_file_type_allowed,
    normalize_moodle_answer_transport,
    normalize_moodle_essay_answer_transport,
)

__all__ = [
    "MOODLE_ANSWER_TRANSPORTS",
    "MOODLE_ESSAY_ANSWER_TRANSPORTS",
    "MoodleAnswerTransport",
    "MoodleEssayAnswerTransport",
    "confirmed_moodle_activity_answer_transport",
    "moodle_file_type_allowed",
    "normalize_moodle_answer_transport",
    "normalize_moodle_essay_answer_transport",
]
