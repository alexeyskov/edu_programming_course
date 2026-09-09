from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.authoring import _require_latest_attempt_quiz_grading_for_publication
from app.models.integration import ExternalMapping
from app.services.moodle_source import (
    confirmed_moodle_quiz_grading_method,
    moodle_source_confirmation_from_activity,
)


def _publication_mapping(method: str | None) -> ExternalMapping:
    activity: dict[str, object] = {
        "module": "quiz",
        "quiz_grading_method_confirmed": method is not None,
    }
    if method is not None:
        activity["quiz_grading_method"] = method
    mapping = ExternalMapping(
        external_type="mod_quiz",
        external_id="30354",
        metadata_json={"module": "quiz", "cmid": 30354, "activity": activity},
    )
    return mapping


def test_multiple_attempt_publication_requires_last_attempt_grading() -> None:
    mapping = _publication_mapping("HIGHEST")
    with pytest.raises(HTTPException) as blocked:
        _require_latest_attempt_quiz_grading_for_publication(mapping)
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "MOODLE_LAST_ATTEMPT_GRADING_REQUIRED"


def test_multiple_attempt_publication_accepts_last_attempt_grading() -> None:
    mapping = _publication_mapping("LAST")
    _require_latest_attempt_quiz_grading_for_publication(mapping)


def test_group_publication_requires_last_attempt_grading_even_before_a_retry() -> None:
    mapping = _publication_mapping("HIGHEST")
    with pytest.raises(HTTPException) as blocked:
        _require_latest_attempt_quiz_grading_for_publication(mapping)
    assert blocked.value.detail["code"] == "MOODLE_LAST_ATTEMPT_GRADING_REQUIRED"


def test_quiz_attempt_policy_requires_confirmed_grading_method() -> None:
    activity = {
        "module": "quiz",
        "attempt_policy_confirmed": True,
        "quiz_grading_method": "LAST",
        "quiz_grading_method_confirmed": False,
    }
    assert confirmed_moodle_quiz_grading_method(activity) is None
    assert moodle_source_confirmation_from_activity(activity)["attempt_policy"] is False
