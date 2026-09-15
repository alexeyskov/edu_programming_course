from __future__ import annotations

import pytest

from app.services.common import DomainError
from app.services.moodle_quiz_runtime import prepared_moodle_quiz_attempt
from app.services.moodle_source import moodle_statement_is_deferred


def _questions() -> list[dict[str, object]]:
    return [
        {
            "question_slot": str(slot),
            "question_text": f"Question {slot}",
            "answer_transport": "ESSAY_ONLINE_TEXT",
            "available_answer_transports": ["ESSAY_ONLINE_TEXT"],
            "question_max_mark": 5,
        }
        for slot in (1, 4)
    ]


@pytest.mark.parametrize("invalid_mark", [None, False, 0, -1, "NaN", "Infinity", "unknown"])
def test_multi_question_preparation_requires_real_positive_marks(invalid_mark):
    questions = _questions()
    questions[1]["question_max_mark"] = invalid_mark
    with pytest.raises(DomainError) as error:
        prepared_moodle_quiz_attempt(
            course_external_id="508", cmid=31529, external_attempt_id="123",
            questions=questions, **questions[0],
        )
    assert error.value.code == "INVALID_MOODLE_PREPARATION"


def test_multi_question_preparation_rejects_duplicate_slots():
    questions = _questions()
    questions[1]["question_slot"] = "1"
    with pytest.raises(DomainError) as error:
        prepared_moodle_quiz_attempt(
            course_external_id="508", cmid=31529, external_attempt_id="123",
            questions=questions, **questions[0],
        )
    assert error.value.code == "INVALID_MOODLE_PREPARATION"


def test_multi_question_preparation_rejects_scalar_list_disagreement():
    questions = _questions()
    scalar = {**questions[0], "question_text": "Unexpected statement"}
    with pytest.raises(DomainError) as error:
        prepared_moodle_quiz_attempt(
            course_external_id="508", cmid=31529, external_attempt_id="123",
            questions=questions, **scalar,
        )
    assert error.value.code == "INVALID_MOODLE_PREPARATION"


@pytest.mark.parametrize("random_count", [0, 1, 2])
def test_multi_essay_statement_is_deferred_only_after_complete_discovery(random_count):
    activity = {
        "module": "quiz",
        "question_count": 2,
        "essay_question_count": 2 - random_count,
        "random_question_count": random_count,
        "random_essay_confirmed": random_count > 0,
        "quiz_questions_confirmed": True,
        "statement_deferred": True,
        "statement_confirmed": False,
        "import_supported": True,
    }
    assert moodle_statement_is_deferred(activity)
    assert not moodle_statement_is_deferred({**activity, "quiz_questions_confirmed": False})
    assert not moodle_statement_is_deferred({**activity, "question_count": 3})
    assert not moodle_statement_is_deferred({**activity, "import_supported": False})
    if random_count:
        assert not moodle_statement_is_deferred({**activity, "random_essay_confirmed": False})
