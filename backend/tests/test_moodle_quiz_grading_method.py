from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.authoring import _require_confirmed_quiz_grading_for_publication
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


@pytest.mark.parametrize("method", ["HIGHEST", "AVERAGE", "FIRST", "LAST"])
@pytest.mark.parametrize("attempt_limit", [1, 10])
def test_publication_accepts_confirmed_moodle_grading_methods(
    method: str, attempt_limit: int
) -> None:
    mapping = _publication_mapping(method)
    mapping.metadata_json["activity"]["attempt_limit"] = attempt_limit
    _require_confirmed_quiz_grading_for_publication(mapping)
    assert mapping.metadata_json["activity"]["quiz_grading_method"] == method


@pytest.mark.parametrize("method", [None, "", "SUM"])
def test_publication_rejects_missing_or_unknown_grading_method(method: str | None) -> None:
    mapping = _publication_mapping(method)
    with pytest.raises(HTTPException) as blocked:
        _require_confirmed_quiz_grading_for_publication(mapping)
    assert blocked.value.status_code == 409
    assert blocked.value.detail["code"] == "MOODLE_QUIZ_GRADING_METHOD_UNCONFIRMED"


@pytest.mark.parametrize("method", ["HIGHEST", "AVERAGE", "FIRST", "LAST"])
def test_publication_rejects_unconfirmed_grading_method(method: str) -> None:
    mapping = _publication_mapping(method)
    mapping.metadata_json["activity"]["quiz_grading_method_confirmed"] = False
    with pytest.raises(HTTPException) as blocked:
        _require_confirmed_quiz_grading_for_publication(mapping)
    assert blocked.value.detail["code"] == "MOODLE_QUIZ_GRADING_METHOD_UNCONFIRMED"


def test_assignment_publication_does_not_require_quiz_grading_method() -> None:
    mapping = ExternalMapping(
        external_type="mod_assign",
        external_id="30354",
        metadata_json={"module": "assign", "cmid": 30354},
    )
    _require_confirmed_quiz_grading_for_publication(mapping)


def test_quiz_attempt_policy_requires_confirmed_grading_method() -> None:
    activity = {
        "module": "quiz",
        "attempt_policy_confirmed": True,
        "quiz_grading_method": "LAST",
        "quiz_grading_method_confirmed": False,
    }
    assert confirmed_moodle_quiz_grading_method(activity) is None
    assert moodle_source_confirmation_from_activity(activity)["attempt_policy"] is False
