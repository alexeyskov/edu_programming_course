from __future__ import annotations

from typing import Any

MOODLE_SOURCE_CONFIRMATION_FIELDS: tuple[str, ...] = (
    "title",
    "settings",
    "statement",
    "schedule",
    "duration",
    "grade",
    "attempt_policy",
)
_STATEMENT_DEFERRED = "statement_deferred"
_QUIZ_GRADING_METHODS = frozenset({"HIGHEST", "AVERAGE", "FIRST", "LAST"})


def confirmed_moodle_quiz_grading_method(activity: object) -> str | None:
    """Return a bounded Quiz attempt aggregation method with explicit evidence.

    Moodle computes the gradebook value from all finished attempts according
    to ``grademethod``.  A local decision attached to the latest attempt is not
    enough to prove that the gradebook will use it, so callers must not infer a
    safe method from a missing field or from the number of attempts.
    """

    if not isinstance(activity, dict):
        return None
    method = str(activity.get("quiz_grading_method", "")).upper()
    if (
        activity.get("quiz_grading_method_confirmed") is not True
        or method not in _QUIZ_GRADING_METHODS
    ):
        return None
    return method


def moodle_quiz_uses_latest_attempt_grade(activity: object) -> bool:
    return confirmed_moodle_quiz_grading_method(activity) == "LAST"


def moodle_statement_is_deferred(activity: dict[str, Any]) -> bool:
    """Accept a missing static statement only for one proved random Essay slot.

    Moodle does not choose the concrete question from a random slot until an
    attempt starts.  The browser connector therefore cannot truthfully mark a
    statement as confirmed during course discovery.  This exception remains
    deliberately narrow: discovery must have proved both the single-slot quiz
    shape and that every candidate in the random pool is an Essay question.
    """

    question_count = activity.get("question_count")
    random_question_count = activity.get("random_question_count")
    return (
        str(activity.get("module", "")).removeprefix("mod_") == "quiz"
        and isinstance(question_count, int)
        and not isinstance(question_count, bool)
        and question_count == 1
        and isinstance(random_question_count, int)
        and not isinstance(random_question_count, bool)
        and random_question_count == 1
        and activity.get("random_essay_confirmed") is True
        and activity.get("statement_deferred") is True
        and activity.get("import_supported") is True
        and activity.get("statement_confirmed") is not True
    )


def normalize_moodle_source_confirmation(value: object) -> dict[str, bool]:
    raw = value if isinstance(value, dict) else {}
    return {
        **{field: raw.get(field) is True for field in MOODLE_SOURCE_CONFIRMATION_FIELDS},
        _STATEMENT_DEFERRED: raw.get(_STATEMENT_DEFERRED) is True,
    }


def moodle_source_confirmation_from_activity(activity: dict[str, Any]) -> dict[str, bool]:
    """Return connector provenance without inferring truth from field values.

    Zero/None can mean either an explicitly disabled Moodle restriction or a
    failed parser.  Only the connector's dedicated evidence flags distinguish
    those cases, so the backend must never reconstruct confirmation from a
    convenient local default.
    """

    module = str(activity.get("module", "")).lower().removeprefix("mod_")
    attempt_policy_confirmed = activity.get("attempt_policy_confirmed") is True
    if module == "quiz":
        attempt_policy_confirmed = bool(
            attempt_policy_confirmed and confirmed_moodle_quiz_grading_method(activity) is not None
        )
    return normalize_moodle_source_confirmation(
        {
            "title": activity.get("title_confirmed"),
            "settings": activity.get("settings_confirmed"),
            "statement": activity.get("statement_confirmed"),
            "schedule": activity.get("schedule_confirmed"),
            "duration": activity.get("duration_confirmed"),
            "grade": activity.get("grade_confirmed"),
            "attempt_policy": attempt_policy_confirmed,
            _STATEMENT_DEFERRED: moodle_statement_is_deferred(activity),
        }
    )


def missing_moodle_source_confirmations(value: object) -> list[str]:
    confirmation = normalize_moodle_source_confirmation(value)
    return [
        field
        for field in MOODLE_SOURCE_CONFIRMATION_FIELDS
        if not confirmation[field]
        and not (field == "statement" and confirmation[_STATEMENT_DEFERRED])
    ]


def moodle_source_is_confirmed(value: object) -> bool:
    return not missing_moodle_source_confirmations(value)
