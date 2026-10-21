from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from moodle_browser.historical import parse_quiz_review_page
from moodle_browser.parsers import _plain_rich_text
from moodle_browser.quiz import parse_attempt_page
from moodle_browser.statements import (
    statement_node_content,
    statement_node_text,
    truncate_statement,
)

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://edu.mmcs.sfedu.ru"
TEX = r"\sum_{k=1}^{N}{ \frac{(-1)^{k}}{k(k+1)} }"


def test_mixed_mathjax_formula_formula_image_and_ordinary_picture_are_all_preserved():
    formula_url = BASE + "/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png"
    picture_url = BASE + "/pluginfile.php/21/question/questiontext/55/1/diagram.png"
    source = (
        '<div>До <span id="m-Frame" class="MathJax">x 2</span>'
        '<script id="m" type="math/tex">x^2</script> между '
        f'<img alt="Формула-картинка" src="{formula_url}"> затем '
        f'<img alt="Рисунок к заданию" src="{picture_url}"> после</div>'
    )
    node = BeautifulSoup(source, "html.parser").div
    content = statement_node_content(node, base_url=BASE)
    assert content.text == (
        rf"До \(x^2\) между ![Формула-картинка](<{formula_url}>) затем "
        f"![Рисунок к заданию](<{picture_url}>) после"
    )
    assert len(content.legacy_hashes) == 2  # Pre-image and image-only legacy import.


def test_real_moodle_mathjax_shape_uses_tex_once_and_keeps_instructions():
    original = BeautifulSoup((FIXTURES / "statement_mathjax.html").read_text(), "html.parser")
    legacy = " ".join(original.get_text(" ", strip=True).split())
    content = statement_node_content(original.div, base_url=BASE)
    assert content.text.count(TEX) == 1
    assert rf"\[{TEX}\]" in content.text
    assert "∑ N k = 1" not in content.text
    assert content.text.endswith("В основной программе вызвать функцию для разных значений N")
    assert hashlib.sha256(legacy.encode()).hexdigest() in content.legacy_hashes
    assert "<script" not in content.text and "<span" not in content.text
    # The parser does not alter the source DOM used for other Moodle selectors.
    assert original.select_one("#MathJax-Element-1-Frame") is not None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('Before <span class="MathJax_Preview">x^2</span>'
         '<script type="math/tex">x^2</script> after', r"Before \(x^2\) after"),
        (
            'Before <span id="MathJax-Element-2-Frame" class="MathJax">x y</span>'
            '<script id="MathJax-Element-2" type="math/tex">x^2</script> after',
            r"Before \(x^2\) after",
        ),
        ('<script type="math/tex; mode=display">x^2</script>', r"\[x^2\]"),
        (r"Before \(x^2\) after", r"Before \(x^2\) after"),
        (r"Before \[\frac{1}{n}\] after", r"Before \[\frac{1}{n}\] after"),
        ('<script>alert("do not execute")</script>Text', "Text"),
        ('<span class="MathJax">Keep ordinary instructions</span>', "Keep ordinary instructions"),
        (
            '<math display="block"><semantics><mi>x</mi>'
            '<annotation encoding="application/x-tex">x^2</annotation></semantics></math>',
            r"\[x^2\]",
        ),
        (
            '<mjx-container display="true"><math><semantics><mi>x</mi>'
            '<annotation encoding="application/x-tex">x^2</annotation>'
            '</semantics></math></mjx-container>',
            r"\[x^2\]",
        ),
        ('<script type="math/tex">x % hide this\n + y + 20\\%</script>', r"\[x + y + 20\%\]"),
    ],
)
def test_tex_sources_are_preserved_without_executing_moodle_html(source, expected):
    node = BeautifulSoup(f"<div>{source}</div>", "html.parser").div
    assert statement_node_text(node, base_url=BASE) == expected


def test_long_formulas_are_bounded_and_not_half_serialized():
    for marker in (rf"\({TEX}\)", rf"\[{TEX}\]"):
        value = f"Before {marker} After"
        assert truncate_statement(value, maximum=15) == "Before"
    node = BeautifulSoup(
        '<div><script type="math/tex">' + "x" * 4097 + "</script></div>", "html.parser"
    ).div
    assert not statement_node_content(node, base_url=BASE).legacy_hashes


def test_rich_editor_preserves_math_and_paragraph_boundaries():
    source = '<p>Before</p><p><span id="m-Frame" class="MathJax">x 2</span>'
    source += '<script id="m" type="math/tex">x^2</script></p><p>After</p>'
    assert (
        _plain_rich_text(source, maximum=50_000, base_url=BASE) == "Before\n\n\\[x^2\\]\n\nAfter"
    )
    assert (
        _plain_rich_text("<p>std::vector&lt;int&gt;</p>", maximum=50_000, base_url=BASE)
        == "std::vector<int>"
    )


def test_live_and_teacher_history_extract_identical_math_from_their_question():
    fragment = (FIXTURES / "statement_mathjax.html").read_text()
    source = (
        (FIXTURES / "quiz_attempt.html")
        .read_text()
        .replace(
            '<div class="formulation">',
            '<div class="formulation">' + fragment,
        )
    )
    question = parse_attempt_page(
        source,
        BASE + "/mod/quiz/attempt.php?attempt=123&cmid=777",
        base_url=BASE,
        course_id="549",
        cmid=777,
    )
    assert TEX in question.question_text and question.question_text_legacy_hashes
    review = (
        (FIXTURES / "quiz_review_historical.html")
        .read_text()
        .replace(
            '<div class="qtext">',
            '<div class="qtext">' + fragment,
        )
    )
    response = parse_quiz_review_page(
        review,
        BASE + "/mod/quiz/review.php?attempt=9001&cmid=30354",
        base_url=BASE,
        course_id="549",
        cmid=30354,
        attempt_id="9001",
        user_id="77",
    )["responses"][0]
    assert TEX in response["question_text"]
