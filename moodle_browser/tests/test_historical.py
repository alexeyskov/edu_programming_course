from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest

from moodle_browser.config import Settings
from moodle_browser.historical import (
    finalize_historical_submission,
    parse_assignment_grader_page,
    parse_assignment_grading_page,
    parse_quiz_report_page,
    parse_quiz_review_page,
    prioritize_historical_attempts,
    quiz_review_navigation_urls,
)
from moodle_browser.parsers import MoodleMarkupError
from moodle_browser.service import (
    BrowserUnavailable,
    MoodleBrowserService,
    MoodleProtocolError,
    MoodleSessionExpired,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE_URL = "https://edu.mmcs.sfedu.ru"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_quiz_report_and_review_have_stable_attempt_user_and_source() -> None:
    index = parse_quiz_report_page(
        fixture("quiz_report_historical.html"),
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        page_number=0,
    )
    assert index.has_next is True
    assert index.skipped_rows == 1
    assert len(index.items) == 1
    item = index.items[0]
    assert item["attempt_id"] == "9001"
    assert item["user_id"] == "77"
    assert item["state"] == "GRADED"
    assert item["submitted_at_epoch"] == 1_787_608_800
    assert item["grade"] == 8.0
    assert item["grade_max"] == 10.0

    detail = parse_quiz_review_page(
        fixture("quiz_review_historical.html"),
        f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )
    response = detail["responses"][0]
    assert response["response_id"] == "1"
    assert "return 1.0" in response["answer_text"]
    assert response["grade"] == 8.0
    assert response["comment"] == "Хорошая работа."
    assert [entry["filename"] for entry in response["_artifact_links"]] == ["main.cpp"]

    item.pop("_detail_url")
    item["responses"] = detail["responses"]
    public = finalize_historical_submission(item)
    assert public["external_id"] == "quiz:30354:9001"
    assert len(public["external_revision"]) == 64
    assert "_artifact_links" not in public["responses"][0]


@pytest.mark.parametrize(
    "ungraded_label",
    ["Балл: 3,00", "Marked out of 3.00"],
)
def test_quiz_review_treats_single_grade_value_as_ungraded_maximum(
    ungraded_label: str,
) -> None:
    html = fixture("quiz_review_historical.html").replace(
        "Балл: 8,00 из 10,00",
        ungraded_label,
    )

    detail = parse_quiz_review_page(
        html,
        f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )

    response = detail["responses"][0]
    assert response["grade"] is None
    assert response["grade_max"] == 3.0


@pytest.mark.parametrize(
    "attempt_evidence",
    [
        '<a href="/mod/quiz/attempt.php?attempt=9002&amp;cmid=30354">Продолжить попытку</a>',
        '<input type="checkbox" name="attemptid[]" value="9002">',
    ],
)
def test_quiz_report_keeps_in_progress_attempt_without_review_link(
    attempt_evidence: str,
) -> None:
    html = fixture("quiz_report_historical.html").replace(
        "</tbody>",
        f"""
        <tr>
          <td><a href="/user/view.php?id=78&amp;course=549">Test2 User2</a></td>
          <td>В процессе</td>
          <td>{attempt_evidence}</td>
        </tr>
        </tbody>
        """,
    )
    index = parse_quiz_report_page(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        page_number=0,
    )
    active = next(item for item in index.items if item["attempt_id"] == "9002")
    assert active["user_id"] == "78"
    assert active["state"] == "IN_PROGRESS"
    assert active["_detail_url"] in {
        "",
        f"{BASE_URL}/mod/quiz/attempt.php?attempt=9002&cmid=30354",
    }


def test_quiz_report_rejects_ambiguous_in_progress_attempt_ids() -> None:
    html = fixture("quiz_report_historical.html").replace(
        "</tbody>",
        """
        <tr>
          <td><a href="/user/view.php?id=78&amp;course=549">Test2 User2</a></td>
          <td>В процессе</td>
          <td><a href="/mod/quiz/attempt.php?attempt=9002&amp;cmid=30354">Attempt</a></td>
          <td><input name="attemptid[]" value="9003"></td>
        </tr>
        </tbody>
        """,
    )
    index = parse_quiz_report_page(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        page_number=0,
    )
    assert all(item["user_id"] != "78" for item in index.items)
    assert index.skipped_rows == 2


def test_quiz_report_ignores_structural_and_never_attempted_rows() -> None:
    html = fixture("quiz_report_historical.html").replace(
        "</tbody>",
        """
        <tr><td colspan="9"></td></tr>
        <tr class="average"><td colspan="7">Общее среднее 2,37 (95)</td></tr>
        <tr>
          <td><a href="/user/view.php?id=78&amp;course=549">Test2 User2</a></td>
          <td>Попыток пока нет</td><td>-</td><td>-</td>
        </tr>
        </tbody>
        """,
    )
    index = parse_quiz_report_page(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        page_number=0,
    )

    # The existing cross-origin attempt remains the only genuinely
    # unidentified row; layout and no-attempt rows do not weaken the guard.
    assert index.skipped_rows == 1
    assert [item["attempt_id"] for item in index.items] == ["9001"]


@pytest.mark.parametrize("module", ["quiz", "assign"])
@pytest.mark.parametrize(
    "message", ["Nothing to display", "Нечего показывать", "Нет данных для отображения"],
)
@pytest.mark.parametrize("element", ['div class="alert alert-info"', 'h2'])
def test_empty_report_without_table_is_not_a_sync_error(
    module: str, message: str, element: str,
) -> None:
    parser = parse_quiz_report_page if module == "quiz" else parse_assignment_grading_page
    html = (
        '<body class="course-549"><main id="region-main">'
        f'<{element}>{message}</{element.split()[0]}></main></body>'
    )

    index = parser(html, base_url=BASE_URL, course_id="549", cmid=777, page_number=0)

    assert index.items == []
    assert index.has_next is False
    assert index.skipped_rows == 0


@pytest.mark.parametrize("module", ["quiz", "assign"])
@pytest.mark.parametrize(
    "content",
    [
        '<main id="region-main"><h2>Report</h2></main>',
        '<main id="region-main"><div class="alert alert-danger">Access denied</div></main>',
        '<aside><h2>Nothing to display</h2></aside><main id="region-main"></main>',
        '<main id="region-main"><div class="alert alert-info">Nothing to display</div>'
        '<div class="errorbox">Database error</div></main>',
        '<main id="region-main"><div class="alert alert-info">Nothing to display</div>'
        '<a href="/mod/quiz/review.php?attempt=9001">Review</a></main>',
        '<main id="region-main"><div class="alert alert-info">Nothing to display</div>'
        '<a href="/mod/assign/view.php?id=777&amp;action=grader&amp;userid=77">Grade</a></main>',
        '<main id="region-main"><div class="alert alert-info">Nothing to display</div>'
        '<input type="password" name="password"></main>',
    ],
)
def test_missing_or_failed_report_is_not_mistaken_for_zero_submissions(
    module: str, content: str,
) -> None:
    parser = parse_quiz_report_page if module == "quiz" else parse_assignment_grading_page
    with pytest.raises(MoodleMarkupError, match="has no .* table"):
        parser(
            f'<body class="course-549">{content}</body>',
            base_url=BASE_URL, course_id="549", cmid=777, page_number=0,
        )


@pytest.mark.parametrize("module", ["quiz", "assign"])
def test_empty_report_still_requires_the_correct_course(module: str) -> None:
    parser = parse_quiz_report_page if module == "quiz" else parse_assignment_grading_page
    with pytest.raises(MoodleMarkupError, match="another course context"):
        parser(
            '<body class="course-550"><main id="region-main">'
            '<div class="alert alert-info">Nothing to display</div></main></body>',
            base_url=BASE_URL, course_id="549", cmid=777, page_number=0,
        )


def test_assignment_students_without_submissions_are_not_unidentified_rows() -> None:
    html = '''<body class="course-549"><table id="mod_assign_grading"><tbody>
        <tr><td><a href="/user/view.php?id=77&amp;course=549">Test User</a></td>
            <td>No submission</td><td class="grade">-</td></tr>
        <tr><td><a href="/user/view.php?id=78&amp;course=549">Test2 User2</a></td>
            <td>Ответ не предоставлен</td><td class="grade">-</td></tr>
    </tbody></table></body>'''

    index = parse_assignment_grading_page(
        html, base_url=BASE_URL, course_id="549", cmid=777, page_number=0,
    )

    assert index.items == []
    assert index.has_next is False
    assert index.skipped_rows == 0


def test_quiz_report_treats_finished_not_graded_row_as_submitted() -> None:
    html = fixture("quiz_report_historical.html").replace(
        '<td class="grade">8,00 / 10,00</td>',
        '<td class="grade">Ещё не оценено · Требуется оценивание</td>',
    )
    index = parse_quiz_report_page(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        page_number=0,
    )

    assert index.items[0]["grade"] is None
    assert index.items[0]["state"] == "SUBMITTED"


def test_historical_priority_keeps_latest_attempt_and_pending_rows_first() -> None:
    def item(
        attempt_id: str,
        user_id: str,
        state: str,
        submitted_at_epoch: int,
        grade: float | None,
    ) -> dict[str, object]:
        return {
            "module": "quiz",
            "cmid": 30354,
            "attempt_id": attempt_id,
            "user_id": user_id,
            "display_name": f"Student {user_id}",
            "state": state,
            "submitted_at_epoch": submitted_at_epoch,
            "grade": grade,
        }

    rows = [
        item("100", "1", "SUBMITTED", 100, None),
        item("101", "1", "GRADED", 200, 3.0),
        item("102", "2", "SUBMITTED", 300, None),
        item("103", "3", "IN_PROGRESS", 400, None),
        item("99", "4", "GRADED", 50, 2.0),
    ]

    all_latest = prioritize_historical_attempts(rows)  # type: ignore[arg-type]
    pending = prioritize_historical_attempts(rows, pending_only=True)  # type: ignore[arg-type]

    assert [entry["attempt_id"] for entry in all_latest] == ["102", "103", "101", "99"]
    assert [entry["attempt_id"] for entry in pending] == ["102", "103"]


def test_quiz_review_keeps_multiple_essay_responses_and_artifacts_separate() -> None:
    arguments = {
        "current_url": f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
        "base_url": BASE_URL,
        "course_id": "549",
        "cmid": 30354,
        "attempt_id": "9001",
        "user_id": "77",
    }
    first = parse_quiz_review_page(
        fixture("quiz_review_multi_essay.html"),
        **arguments,
    )
    repeated = parse_quiz_review_page(
        fixture("quiz_review_multi_essay.html"),
        **arguments,
    )

    assert [response["response_id"] for response in first["responses"]] == ["11", "12"]
    assert [response["response_id"] for response in repeated["responses"]] == ["11", "12"]
    assert [artifact["filename"] for artifact in first["responses"][0]["_artifact_links"]] == [
        "time.cpp",
        "time.hpp",
    ]
    assert [artifact["filename"] for artifact in first["responses"][1]["_artifact_links"]] == [
        "date.cpp"
    ]
    assert "time_inline = 11" in first["responses"][0]["answer_text"]
    assert "date_inline = 12" in first["responses"][1]["answer_text"]
    assert all(response["artifacts"] == [] for response in first["responses"])


def test_quiz_review_navigation_rejects_foreign_attempt_activity_and_origin() -> None:
    current_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=0"
    show_all, pages = quiz_review_navigation_urls(
        fixture("quiz_review_paginated_0.html"),
        current_url,
        base_url=BASE_URL,
        cmid=30354,
        attempt_id="9001",
    )

    assert show_all is None
    assert pages == [f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=1"]


@pytest.mark.asyncio
async def test_historical_quiz_detail_collects_every_paginated_essay_and_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    first_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=0"
    second_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=1"

    class FakePage:
        url = first_url

    page = FakePage()
    visited: list[str] = []

    async def goto(fake_page: FakePage, url: str) -> str:
        visited.append(url)
        fake_page.url = url
        if url == first_url:
            return fixture("quiz_review_paginated_0.html")
        if url == second_url:
            return fixture("quiz_review_paginated_1.html")
        raise AssertionError(f"unexpected Quiz review target: {url}")

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    detail, warning = await service._historical_detail(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        module="quiz",
        course_id="549",
        cmid=30354,
        item={"attempt_id": "9001", "user_id": "77"},
        detail_url=first_url,
    )

    assert visited == [first_url, second_url]
    assert warning is None
    assert detail["responses_complete"] is True
    assert [response["response_id"] for response in detail["responses"]] == ["1", "2"]
    assert [artifact["filename"] for artifact in detail["responses"][0]["_artifact_links"]] == [
        "SAM4.7z"
    ]
    assert [artifact["filename"] for artifact in detail["responses"][1]["_artifact_links"]] == [
        "point.hpp",
        "input.txt",
    ]


@pytest.mark.asyncio
async def test_historical_quiz_detail_prefers_validated_show_all_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    first_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=0"
    show_all_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&showall=1"
    first_html = fixture("quiz_review_paginated_0.html").replace(
        "</nav>",
        '<a href="/mod/quiz/review.php?attempt=9001&amp;cmid=30354&amp;showall=1">'
        "Показать все вопросы на одной странице</a></nav>",
    )

    class FakePage:
        url = first_url

    page = FakePage()
    visited: list[str] = []

    async def goto(fake_page: FakePage, url: str) -> str:
        visited.append(url)
        fake_page.url = url
        if url == first_url:
            return first_html
        if url == show_all_url:
            return fixture("quiz_review_multi_essay.html")
        raise AssertionError(f"unexpected Quiz review target: {url}")

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    detail, warning = await service._historical_detail(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        module="quiz",
        course_id="549",
        cmid=30354,
        item={"attempt_id": "9001", "user_id": "77"},
        detail_url=first_url,
    )

    assert visited == [first_url, show_all_url]
    assert warning is None
    assert detail["responses_complete"] is True
    assert [response["response_id"] for response in detail["responses"]] == ["11", "12"]


@pytest.mark.asyncio
async def test_show_all_keeps_attachment_exposed_only_on_question_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    first_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=0"
    show_all_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&showall=1"
    first_html = fixture("quiz_review_paginated_0.html").replace(
        "</nav>",
        '<a href="/mod/quiz/review.php?attempt=9001&amp;cmid=30354&amp;showall=1">'
        "Показать все вопросы на одной странице</a></nav>",
    )
    show_all_html = fixture("quiz_review_paginated_0.html").replace(
        '<div class="attachments">\n'
        '          <a href="/pluginfile.php/123/question/response_attachments/1/SAM4.7z">'
        "SAM4.7z</a>\n"
        "        </div>",
        "",
    )

    class FakePage:
        url = first_url

    page = FakePage()

    async def goto(fake_page: FakePage, url: str) -> str:
        fake_page.url = url
        if url == first_url:
            return first_html
        if url == show_all_url:
            return show_all_html
        raise AssertionError(f"unexpected Quiz review target: {url}")

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    detail, warning = await service._historical_detail(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        module="quiz",
        course_id="549",
        cmid=30354,
        item={"attempt_id": "9001", "user_id": "77"},
        detail_url=first_url,
    )

    assert warning is None
    assert detail["responses_complete"] is True
    assert [artifact["filename"] for artifact in detail["responses"][0]["_artifact_links"]] == [
        "SAM4.7z"
    ]


@pytest.mark.asyncio
async def test_historical_quiz_detail_marks_failed_child_page_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    first_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=0"
    second_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354&page=1"

    class FakePage:
        url = first_url

    page = FakePage()

    async def goto(fake_page: FakePage, url: str) -> str:
        fake_page.url = url
        if url == first_url:
            return fixture("quiz_review_paginated_0.html")
        if url == second_url:
            raise BrowserUnavailable("Moodle child review page timed out")
        raise AssertionError(f"unexpected Quiz review target: {url}")

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    detail, warning = await service._historical_detail(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        module="quiz",
        course_id="549",
        cmid=30354,
        item={"attempt_id": "9001", "user_id": "77"},
        detail_url=first_url,
    )

    assert [response["response_id"] for response in detail["responses"]] == ["1"]
    assert detail["responses_complete"] is False
    assert warning == "DETAIL_PAGINATION_INCOMPLETE:9001"


def test_quiz_review_recognizes_7z_as_the_essay_attachment() -> None:
    html = (
        fixture("quiz_review_historical.html")
        .replace(
            "/pluginfile.php/123/question/response_attachments/1/main.cpp",
            "/pluginfile.php/123/question/response_attachments/1/SAM4.7z",
        )
        .replace(">main.cpp<", ">SAM4.7z<")
    )
    detail = parse_quiz_review_page(
        html,
        f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )

    assert [artifact["filename"] for artifact in detail["responses"][0]["_artifact_links"]] == [
        "SAM4.7z"
    ]


@pytest.mark.asyncio
async def test_historical_artifact_download_keeps_7z_bytes_bounded() -> None:
    content = b"7z\xbc\xaf'\x1c" + b"bounded-archive"
    encoded = base64.b64encode(content).decode("ascii")

    class FakePage:
        async def evaluate(self, _script: str, payload: dict[str, object]):
            assert payload["url"] == (
                f"{BASE_URL}/pluginfile.php/123/question/response_attachments/1/SAM4.7z"
            )
            assert payload["maxBytes"] == 1_024
            return {
                "size": len(content),
                "contentType": "application/x-7z-compressed",
                "contentBase64": encoded,
            }

    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, consumed = await service._historical_artifact(
        FakePage(),  # type: ignore[arg-type]
        {
            "external_id": "a" * 64,
            "filename": "SAM4.7z",
            "url": f"{BASE_URL}/pluginfile.php/123/question/response_attachments/1/SAM4.7z",
        },
        maximum_bytes=1_024,
    )

    assert artifact["downloaded"] is True
    assert artifact["filename"] == "SAM4.7z"
    assert artifact["content_base64"] == encoded
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert consumed == len(encoded)


@pytest.mark.asyncio
async def test_historical_artifact_uses_authenticated_browser_request_context() -> None:
    content = b"7z\xbc\xaf'\x1c" + b"authenticated-download"

    class FakeResponse:
        url = f"{BASE_URL}/pluginfile.php/123/question/response_attachments/1/SAM4.7z"
        ok = True
        status = 200
        headers = {
            "content-type": "application/x-7z-compressed",
            "content-length": str(len(content)),
        }

        async def body(self) -> bytes:
            return content

        async def dispose(self) -> None:
            return None

    class FakeRequest:
        async def get(self, url: str, *, timeout: int):
            assert url == FakeResponse.url
            assert timeout == 10_000
            return FakeResponse()

    class FakeContext:
        request = FakeRequest()

    class FakePage:
        context = FakeContext()

        async def evaluate(self, *_args: object):
            raise AssertionError("page-level fetch must not be used")

    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    artifact, consumed = await service._historical_artifact(
        FakePage(),  # type: ignore[arg-type]
        {
            "external_id": "b" * 64,
            "filename": "SAM4.7z",
            "url": FakeResponse.url,
        },
        maximum_bytes=1_024,
    )

    assert artifact["downloaded"] is True
    assert base64.b64decode(artifact["content_base64"]) == content
    assert artifact["sha256"] == hashlib.sha256(content).hexdigest()
    assert consumed == len(artifact["content_base64"])


def test_live_quiz_review_preserves_code_whitespace_and_cleans_feedback() -> None:
    detail = parse_quiz_review_page(
        fixture("quiz_review_live_feedback.html"),
        f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )

    response = detail["responses"][0]
    assert response["answer_text"] == (
        "#include <iostream>\n\n"
        "\tint main() {\n"
        "\t    int value{};\n"
        "\t    std::cin >> value;\n"
        "\t    return value;\n"
        "}"
    )
    assert response["comment"] == (
        "вектор конечно должен иметь координаты double\n"
        "в operator* не нужно создавать локальный вектор"
    )
    assert response["reviewer_name"] == "Герасименко Т."
    assert "Комментарии" not in response["comment"]
    assert "Оставить комментарий" not in response["comment"]
    assert "служебный текст" not in response["comment"]


def test_quiz_rich_text_answer_joins_inline_spans_without_extra_lines() -> None:
    html = fixture("quiz_review_historical.html").replace(
        "<pre>double mean() {\n    return 1.0;\n}</pre>",
        "<p><span>int</span> main() {</p>\n"
        "<p>&nbsp;&nbsp;&nbsp;&nbsp;std::<span>cin</span> &gt;&gt; value;</p>\n"
        "<p>}</p>",
    )
    detail = parse_quiz_review_page(
        html,
        f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )

    assert detail["responses"][0]["answer_text"] == ("int main() {\n    std::cin >> value;\n}")


@pytest.mark.asyncio
async def test_browser_rendered_quiz_answer_preserves_internal_whitespace() -> None:
    class RenderedResponse:
        @property
        def first(self):
            return self

        async def count(self) -> int:
            return 1

        async def inner_text(self) -> str:
            return "\n\tint main() {\r\n\t    int value{};  \r\n\r\n\t}\n"

    class Question:
        def locator(self, selector: str) -> RenderedResponse:
            assert "qtype_essay_response" in selector
            return RenderedResponse()

    class Questions:
        async def count(self) -> int:
            return 1

        def nth(self, index: int) -> Question:
            assert index == 0
            return Question()

    class FakePage:
        def locator(self, selector: str) -> Questions:
            assert selector == ".que.essay"
            return Questions()

    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    values = await service._historical_quiz_inner_text(FakePage())  # type: ignore[arg-type]

    assert values == ["\tint main() {\n\t    int value{};  \n\n\t}"]


@pytest.mark.asyncio
async def test_browser_rendered_assignment_answer_preserves_internal_whitespace() -> None:
    class RenderedResponse:
        @property
        def first(self):
            return self

        async def count(self) -> int:
            return 1

        async def inner_text(self) -> str:
            return "\n\tint main() {\r\n\t    int value{};  \r\n\r\n\t}\n"

    class FakePage:
        def locator(self, selector: str) -> RenderedResponse:
            assert selector == ".assignsubmission_onlinetext"
            return RenderedResponse()

    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    value = await service._historical_assignment_inner_text(  # type: ignore[arg-type]
        FakePage()
    )

    assert value == "\tint main() {\n\t    int value{};  \n\n\t}"


def test_assignment_feedback_html_detaches_reviewer_signature() -> None:
    html = fixture("assign_grader_historical.html").replace(
        '<textarea name="assignfeedbackcomments_editor[text]">Принято</textarea>',
        '<textarea name="assignfeedbackcomments_editor[text]">'
        "&lt;p&gt;Принято&lt;/p&gt;&lt;p&gt;Герасименко Т.&lt;/p&gt;"
        "</textarea>",
    )
    detail = parse_assignment_grader_page(
        html,
        f"{BASE_URL}/mod/assign/view.php?id=777&action=grader&userid=77&attemptnumber=2",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        user_id="77",
    )

    assert detail["comment"] == "Принято"
    assert detail["responses"][0]["reviewer_name"] == "Герасименко Т."


def test_quiz_report_does_not_treat_numeric_sort_fields_as_timestamps() -> None:
    html = fixture("quiz_report_historical.html").replace(
        '<td><a href="/user/view.php?id=77&amp;course=549">Иванов Иван</a></td>',
        '<td data-sort="77"><a href="/user/view.php?id=77&amp;course=549">Иванов Иван</a></td>',
    )
    index = parse_quiz_report_page(
        html,
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        page_number=0,
    )

    assert index.items[0]["submitted_at_epoch"] == 1_787_608_800


def test_quiz_review_rejects_changed_attempt_or_user() -> None:
    with pytest.raises(MoodleMarkupError, match="identifiers changed"):
        parse_quiz_review_page(
            fixture("quiz_review_historical.html"),
            f"{BASE_URL}/mod/quiz/review.php?attempt=9002&cmid=30354",
            base_url=BASE_URL,
            course_id="549",
            cmid=30354,
            attempt_id="9001",
            user_id="77",
        )
    with pytest.raises(MoodleMarkupError, match="another user"):
        parse_quiz_review_page(
            fixture("quiz_review_historical.html"),
            f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354",
            base_url=BASE_URL,
            course_id="549",
            cmid=30354,
            attempt_id="9001",
            user_id="88",
        )


def test_assignment_grading_and_detail_support_reopened_attempt() -> None:
    index = parse_assignment_grading_page(
        fixture("assign_grading_historical.html"),
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        page_number=0,
    )
    assert index.has_next is False
    assert index.skipped_rows == 0
    item = index.items[0]
    assert item["attempt_id"] == "user-77-attempt-2"
    assert item["submitted_at_epoch"] == 1_787_583_600
    assert item["grade"] == 9.0

    detail = parse_assignment_grader_page(
        fixture("assign_grader_historical.html"),
        f"{BASE_URL}/mod/assign/view.php?id=777&action=grader&userid=77&attemptnumber=2",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        user_id="77",
    )
    assert "int main" in detail["responses"][0]["answer_text"]
    assert detail["grade"] == 9.0
    assert detail["grade_max"] == 10.0
    assert detail["comment"] == "Принято"
    assert detail["responses"][0]["_artifact_links"][0]["filename"] == "main.cpp"


def test_assignment_grader_keeps_online_text_and_exact_file_plugin_separate() -> None:
    detail = parse_assignment_grader_page(
        fixture("assign_grader_mixed_response.html"),
        f"{BASE_URL}/mod/assign/view.php?id=777&action=grader&userid=77&attemptnumber=2",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
        user_id="77",
    )

    response = detail["responses"][0]
    assert response["answer_text"] == "\tint main() {\n\t    return 0;  \n\t}"
    assert [item["filename"] for item in response["_artifact_links"]] == [
        "solution.zip",
        "api.hpp",
        "input.txt",
    ]
    assert all(
        item["filename"] not in {"condition.pdf", "example.cpp"}
        for item in response["_artifact_links"]
    )


@pytest.mark.asyncio
async def test_detail_navigation_failure_is_an_explicit_per_attempt_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    class FakePage:
        url = ""

    page = FakePage()
    failed_once = False

    async def goto(fake_page: FakePage, url: str) -> str:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise BrowserUnavailable("Moodle navigation failed")
        fake_page.url = url
        return fixture("quiz_review_historical.html")

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    item = {"attempt_id": "9001", "user_id": "77"}
    detail_url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354"

    omitted, warning = await service._historical_detail(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        module="quiz",
        course_id="549",
        cmid=30354,
        item=item,
        detail_url=detail_url,
    )
    recovered, recovered_warning = await service._historical_detail(
        page,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        module="quiz",
        course_id="549",
        cmid=30354,
        item=item,
        detail_url=detail_url,
    )

    assert warning == "DETAIL_NAVIGATION_FAILED:9001"
    assert omitted["responses"] == [
        {
            "response_id": "moodle-detail",
            "question_text": "",
            "answer_text": "",
            "answer_complete": False,
            "answer_omission_reason": "DETAIL_NAVIGATION_FAILED",
            "grade": None,
            "grade_max": None,
            "comment": "",
            "artifacts": [],
        }
    ]
    assert recovered_warning is None
    assert "return 1.0" in recovered["responses"][0]["answer_text"]


@pytest.mark.asyncio
async def test_detail_recovery_does_not_hide_expired_session_or_changed_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    class FakePage:
        url = f"{BASE_URL}/mod/quiz/review.php?attempt=9001&cmid=30354"

    page = FakePage()

    async def goto(_page: FakePage, _url: str) -> str:
        return fixture("quiz_review_historical.html")

    async def expired(*_args: object) -> None:
        raise MoodleSessionExpired("Moodle browser session expired")

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", expired)
    arguments = {
        "module": "quiz",
        "course_id": "549",
        "cmid": 30354,
        "item": {"attempt_id": "9001", "user_id": "77"},
        "detail_url": page.url,
    }

    with pytest.raises(MoodleSessionExpired):
        await service._historical_detail(  # type: ignore[arg-type]
            page,
            object(),
            **arguments,
        )

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    page.url = f"{BASE_URL}/mod/quiz/review.php?attempt=9002&cmid=30354"
    with pytest.raises(MoodleProtocolError, match="identifiers changed"):
        await service._historical_detail(  # type: ignore[arg-type]
            page,
            object(),
            **arguments,
        )
