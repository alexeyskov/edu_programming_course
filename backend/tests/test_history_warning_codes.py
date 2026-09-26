from app.services.moodle_history_diagnostics import history_warning_codes


def test_history_warning_codes_drop_student_identifiers_and_unknown_text():
    assert history_warning_codes([
        "ARTIFACT_OMITTED:12345:private-file", "ARTIFACT_OMITTED:67890:other-file",
        "DETAIL_PAGINATION_INCOMPLETE:12345", "DELETION_CHECK_INCOMPLETE",
        "private unknown message", {"session": "secret"},
    ]) == [
        "ARTIFACT_OMITTED", "DELETION_CHECK_INCOMPLETE", "DETAIL_PAGINATION_INCOMPLETE",
        "HISTORY_IMPORT_WARNING",
    ]
    assert history_warning_codes("not a list") == []
    assert history_warning_codes(["ARTIFACT_OMITTED"] * 32 + ["private text"]) == [
        "ARTIFACT_OMITTED",
    ]
