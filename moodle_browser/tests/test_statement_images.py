from __future__ import annotations

import html
from pathlib import Path

import pytest

from moodle_browser.historical import parse_quiz_review_page
from moodle_browser.parsers import parse_activity_settings, parse_quiz_essay_question_edit
from moodle_browser.quiz import parse_attempt_page
from moodle_browser.statements import statement_image_url, statement_node_text, truncate_statement

BASE_URL = "https://edu.mmcs.sfedu.ru"
IMAGE_PATH = "/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png"
IMAGE_URL = BASE_URL + IMAGE_PATH
LATEX = r"\sum_{k=1}^{N}\frac{1}{k\sqrt{k}}"
TOKEN = rf"![\\sum_{{k=1}}^{{N}}\\frac{{1}}{{k\\sqrt{{k}}}}](<{IMAGE_URL}>)"
FORMULA = f'<img class="texrender rounded-0" alt="{LATEX}" src="{IMAGE_URL}">'
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize(
    "source, expected",
    [
        (IMAGE_PATH, IMAGE_URL),
        (IMAGE_URL, IMAGE_URL),
        ("//edu.mmcs.sfedu.ru" + IMAGE_PATH, IMAGE_URL),
        ("/pluginfile.php/21/question/questiontext/55/1/chart.png", BASE_URL
         + "/pluginfile.php/21/question/questiontext/55/1/chart.png"),
        ("/pluginfile.php/21/question/questiontext/55/1/chart (1).png", BASE_URL
         + "/pluginfile.php/21/question/questiontext/55/1/chart%20(1).png"),
        ("/file.php/21/diagram.webp?forcedownload=0", BASE_URL
         + "/file.php/21/diagram.webp?forcedownload=0"),
        ("https://example.org" + IMAGE_PATH, None),
        ("http://edu.mmcs.sfedu.ru" + IMAGE_PATH, None),
        ("https://user:secret@edu.mmcs.sfedu.ru" + IMAGE_PATH, None),
        (IMAGE_PATH + "#fragment", None),
        ("/pluginfile.php/21/question/../private.png", None),
        ("/pluginfile.php/21/question/%2e%2e/private.png", None),
        ("/pluginfile.php/21/question/%252e%252e/private.png", None),
        ("/pluginfile.php/21/question/%00private.png", None),
        ("/pluginfile.php/21/question/private.svg", None),
        ("/draftfile.php/21/question/private.png", None),
        ("/login/index.php?file=private.png", None),
        (IMAGE_PATH + "?token=secret", None),
        (IMAGE_PATH + "?sesskey=secret", None),
        (IMAGE_PATH + "?wstoken=secret", None),
        (IMAGE_PATH + "?forcedownload=0&forcedownload=1", None),
        (IMAGE_PATH + "?rev=abc", None),
        (IMAGE_PATH + "?cache=12", IMAGE_URL + "?cache=12"),
        ("javascript:alert(1)", None),
        ("data:image/png;base64,AA==", None),
        ("/pluginfile.php/21/evil\\private.png", None),
    ],
)
def test_statement_image_urls_are_origin_and_endpoint_bounded(source, expected) -> None:
    assert statement_image_url(source, BASE_URL) == expected


def test_url_length_is_bounded_after_unicode_and_whitespace_encoding() -> None:
    prefix = BASE_URL + "/pluginfile.php/21/question/"
    accepted = prefix + "a" * (4_096 - len(prefix) - len(".png")) + ".png"
    assert len(accepted) == 4_096
    assert statement_image_url(accepted, BASE_URL) == accepted
    rejected = prefix + "я " * 700 + ".png"
    assert len(rejected) < 4_096
    assert statement_image_url(rejected, BASE_URL) is None


def test_live_quiz_preserves_texrender_formula_between_existing_words() -> None:
    source = (FIXTURES / "quiz_attempt.html").read_text()
    source = source.replace(
        '<div class="formulation">',
        '<div class="formulation"><div class="qtext">Найти сумму. '
        + FORMULA + " Вызвать функцию.</div>",
    )
    attempt = parse_attempt_page(
        source,
        BASE_URL + "/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert attempt.question_text == f"Найти сумму. {TOKEN} Вызвать функцию."
    assert "<img" not in attempt.question_text


def test_teacher_historical_review_preserves_the_same_formula() -> None:
    source = (FIXTURES / "quiz_review_historical.html").read_text()
    source = source.replace("<div class=\"qtext\">", '<div class="qtext">' + FORMULA)
    response = parse_quiz_review_page(
        source,
        BASE_URL + "/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE_URL,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )["responses"][0]
    assert TOKEN in response["question_text"]
    assert "<img" not in response["question_text"]


def test_question_editor_and_assignment_discovery_preserve_task_images() -> None:
    question_html = (FIXTURES / "question_edit_essay.html").read_text()
    start = question_html.index('<textarea name="questiontext[text]">')
    end = question_html.index("</textarea>", start)
    question_html = (
        question_html[:start] + '<textarea name="questiontext[text]">'
        + html.escape(f"<p>Вычислить сумму</p><p>{FORMULA}</p><p>Вызвать функцию.</p>")
        + question_html[end:]
    )
    question = parse_quiz_essay_question_edit(
        question_html,
        course_id="549",
        cmid=30354,
        question_id="7711",
        base_url=BASE_URL,
    )
    assert question["description"] == f"Вычислить сумму\n\n{TOKEN}\n\nВызвать функцию."

    assignment_html = f"""
    <body class="course-549 path-mod-assign" data-courseid="549">
    <form class="mform"><input name="coursemodule" value="777">
    <input name="course" value="549"><input name="modulename" value="assign">
    <textarea name="introeditor[text]">{html.escape('<p>Задание ' + FORMULA + '</p>')}</textarea>
    </form></body>
    """
    assignment = parse_activity_settings(
        assignment_html, course_id="549", cmid=777, module="assign", base_url=BASE_URL
    )
    assert assignment["description"] == f"Задание {TOKEN}"


def test_unsafe_source_still_preserves_formula_alt_and_never_html() -> None:
    from bs4 import BeautifulSoup

    fragment = BeautifulSoup(
        '<div>Начало <img src="https://other.example/x.png" alt="sum(x)"> конец</div>',
        "html.parser",
    ).div
    assert statement_node_text(fragment, base_url=BASE_URL) == "Начало sum(x) конец"


def test_sensitive_query_is_not_persisted_even_as_a_broken_image_marker() -> None:
    from bs4 import BeautifulSoup

    fragment = BeautifulSoup(
        f'<div><img src="{IMAGE_URL}?token=do-not-persist" alt="formula"></div>',
        "html.parser",
    ).div
    assert statement_node_text(fragment, base_url=BASE_URL) == "formula"


def test_alt_title_and_escaping_are_lossless_and_token_is_never_cut() -> None:
    from bs4 import BeautifulSoup

    fragment = BeautifulSoup(
        f'<div>Before<img src="{IMAGE_PATH}" title="[x]\\alpha">After</div>',
        "html.parser",
    ).div
    value = statement_node_text(fragment, base_url=BASE_URL)
    marker = rf"![\[x\]\\alpha](<{IMAGE_URL}>)"
    assert value == f"Before {marker} After"
    assert truncate_statement(value, maximum=len("Before ") + 10) == "Before"
    assert truncate_statement(value, maximum=50_000) == value
    assert len(statement_node_text(fragment, base_url=BASE_URL, maximum=8)) <= 8


def test_only_first_32_images_get_canonical_tokens() -> None:
    from bs4 import BeautifulSoup

    fragment = BeautifulSoup("<div>" + FORMULA * 35 + "</div>", "html.parser").div
    value = statement_node_text(fragment, base_url=BASE_URL)
    assert value.count(f"](<{IMAGE_URL}>)") == 32
    assert value.endswith(LATEX)
