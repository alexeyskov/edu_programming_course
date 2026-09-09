from __future__ import annotations

import pytest

from moodle_browser.config import Settings, exact_https_origin


def test_base_url_is_an_exact_https_origin() -> None:
    assert exact_https_origin("https://EDU.MMCS.SFEDU.RU:443/") == ("https://edu.mmcs.sfedu.ru")
    for value in (
        "http://edu.mmcs.sfedu.ru",
        "https://edu.mmcs.sfedu.ru/course/view.php",
        "https://user:password@edu.mmcs.sfedu.ru",
        "https://edu.mmcs.sfedu.ru?next=evil",
        "https://edu.mmcs.sfedu.ru#fragment",
    ):
        with pytest.raises(ValueError, match="exact HTTPS origin"):
            exact_https_origin(value)


def test_request_origin_must_equal_configured_origin() -> None:
    settings = Settings(
        shared_secret=b"x" * 32,
        base_url="https://edu.mmcs.sfedu.ru",
    )
    settings.require_base_url("https://edu.mmcs.sfedu.ru/")
    with pytest.raises(ValueError, match="does not match"):
        settings.require_base_url("https://other.example")


def test_login_course_role_page_limit_is_bounded() -> None:
    Settings(shared_secret=b"x" * 32, max_login_course_role_pages=1)
    Settings(shared_secret=b"x" * 32, max_login_course_role_pages=512)
    for value in (0, 513):
        with pytest.raises(ValueError, match="MAX_LOGIN_COURSE_ROLE_PAGES"):
            Settings(shared_secret=b"x" * 32, max_login_course_role_pages=value)


def test_login_course_role_time_budget_is_bounded() -> None:
    Settings(shared_secret=b"x" * 32, login_course_role_budget_seconds=1)
    Settings(shared_secret=b"x" * 32, login_course_role_budget_seconds=60)
    for value in (0.99, 60.01):
        with pytest.raises(ValueError, match="LOGIN_COURSE_ROLE_BUDGET_SECONDS"):
            Settings(shared_secret=b"x" * 32, login_course_role_budget_seconds=value)


def test_login_operation_deadline_is_bounded() -> None:
    assert Settings(shared_secret=b"x" * 32).login_operation_timeout_seconds == 45
    Settings(shared_secret=b"x" * 32, login_operation_timeout_seconds=10)
    Settings(shared_secret=b"x" * 32, login_operation_timeout_seconds=120)
    for value in (9.99, 120.01):
        with pytest.raises(ValueError, match="LOGIN_OPERATION_TIMEOUT_SECONDS"):
            Settings(shared_secret=b"x" * 32, login_operation_timeout_seconds=value)


def test_activity_detail_budget_covers_large_courses_and_is_bounded() -> None:
    assert Settings(shared_secret=b"x" * 32).activity_detail_budget_seconds == 240
    Settings(shared_secret=b"x" * 32, activity_detail_budget_seconds=30)
    Settings(shared_secret=b"x" * 32, activity_detail_budget_seconds=480)
    for value in (29.99, 480.01):
        with pytest.raises(ValueError, match="ACTIVITY_DETAIL_BUDGET_SECONDS"):
            Settings(shared_secret=b"x" * 32, activity_detail_budget_seconds=value)
