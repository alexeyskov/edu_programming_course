"""Native form/editor regressions; all Moodle requests are local fixtures."""

from __future__ import annotations

import html
import json
import os
from dataclasses import dataclass, field
from urllib.parse import parse_qs

import pytest
from playwright.async_api import Route, async_playwright

from moodle_browser.config import Settings
from moodle_browser.models import GradeRequest
from moodle_browser.service import MoodleBrowserService, MoodleProtocolError

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1",
        reason="opt-in Chromium DOM test",
    ),
]

BASE = "https://edu.mmcs.sfedu.ru"
TEXTAREA_ID = "q12:1_-comment_id"
COMMENT = "Проверено\nПример <code> & детали\nПреподаватель А."


def grade_request() -> GradeRequest:
    return GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "quiz",
                "course_id": "549",
                "cmid": 777,
                "user_id": "77",
                "attempt_id": "134403",
                "question_slot": 1,
                "grade": 1.2,
                "grade_scale_max": 3,
                "quiz_overall_grade_max": 3,
                "comment": COMMENT,
            },
            "idempotency_key": "quiz-grade:134403:1:editor-regression",
        }
    )


def comment_html() -> str:
    return "".join(f"<p>{html.escape(line)}</p>" for line in COMMENT.splitlines())


@dataclass
class GradeFixture:
    editor: str = "tiny"
    mark: str = ""
    comment: str = ""
    restore_local_draft: bool = False
    lose_comment: bool = False
    writes: list[dict[str, list[str]]] = field(default_factory=list)

    def markup(self) -> str:
        if self.editor.startswith("tiny"):
            editor = f"""
            <textarea id="unrelated">Other editor</textarea>
            <script>
            const install = () => {{
            const target = document.getElementById({json.dumps(TEXTAREA_ID)});
            let documentContent = target.defaultValue;
            const handlers = new Set();
            const instance = {{
                initialized: {
                json.dumps(self.editor not in {"tiny_initializing", "tiny_late_initializing"})
            },
                on: (_, callback) => handlers.add(callback),
                off: (_, callback) => handlers.delete(callback),
                getElement: () => target,
                setContent: value => {{ documentContent = value; }},
                save: () => {{ target.value = documentContent; }}
            }};
            const other = {{setContent: () => {{ throw new Error('Wrong editor'); }}}};
            window.tinymce = {{
                activeEditor: other,
                get: id => id === {json.dumps(TEXTAREA_ID)} ? instance : other
            }};
            target.form.addEventListener('submit', () => instance.save());
            if (!instance.initialized) {{
                window.setTimeout(() => {{
                    instance.initialized = true;
                    for (const callback of handlers) callback();
                }}, 150);
            }}
            }};
            if ({json.dumps(self.editor in {"tiny_late", "tiny_late_initializing"})}) {{
                document.addEventListener('click', event => {{
                    if (event.target.id === 'id_submitbutton') install();
                }}, {{once: true}});
            }} else install();
            </script>
            """
        elif self.editor == "atto":
            editor = f"""
            <div id="{TEXTAREA_ID}editable" class="editor_atto_content"
                contenteditable="true">{self.comment}</div>
            <div class="editor_atto_content" contenteditable="true"
                id="unrelated">Other editor</div>
            <script>
            const target = document.getElementById({json.dumps(TEXTAREA_ID)});
            target.form.addEventListener('submit', () => {{
                target.value = document.getElementById(
                    {json.dumps(TEXTAREA_ID + "editable")}).innerHTML;
            }});
            </script>
            """
        else:
            editor = ""
        restore = ""
        if self.restore_local_draft:
            restore = f"""<script>
            document.querySelector('input[name$="-mark"]').value = '2';
            document.getElementById({json.dumps(TEXTAREA_ID)}).value = {
                json.dumps(comment_html())
            };
            </script>"""
        return f"""<!doctype html><html><head><meta charset="utf-8"></head><body>
        <form id="manualgradingform" action="/mod/quiz/comment.php" method="post">
          <input type="hidden" name="attempt" value="134403">
          <input type="hidden" name="slot" value="1">
          <div class="fitem">Балл из 5,00
            <input name="q12:1_-mark" value="{html.escape(self.mark, quote=True)}">
          </div>
          <textarea id="{TEXTAREA_ID}" name="q12:1_-comment">{html.escape(self.comment)}</textarea>
          {editor}
          <input id="id_submitbutton" type="submit" value="Save">
        </form>{restore}</body></html>"""

    async def route(self, route: Route) -> None:
        assert route.request.url.startswith(BASE + "/mod/quiz/comment.php")
        if route.request.method == "POST":
            values = parse_qs(route.request.post_data or "", keep_blank_values=True)
            assert values["attempt"] == ["134403"]
            assert values["slot"] == ["1"]
            self.writes.append(values)
            self.mark = values["q12:1_-mark"][0]
            if not self.lose_comment:
                self.comment = values["q12:1_-comment"][0]
            await route.fulfill(content_type="text/html", body="<p>Saved. Close window.</p>")
        else:
            await route.fulfill(content_type="text/html", body=self.markup())


def service() -> MoodleBrowserService:
    result = MoodleBrowserService(Settings(shared_secret=b"x" * 32))

    async def authenticated(*_args: object) -> None:
        return None

    async def live_preconditions(*_args: object) -> None:
        return None

    # Live target/policy/attempt guards have independent fixture tests; this
    # suite isolates the editor's POST and the persisted GET response.
    result._require_session_without_global_navigation = authenticated  # type: ignore[method-assign]
    result._require_live_quiz_grade_preconditions = live_preconditions  # type: ignore[method-assign]
    return result


@pytest.mark.parametrize("editor", ["tiny", "tiny_initializing", "tiny_late", "atto", "plain"])
async def test_comment_survives_editor_save_and_retry_does_not_write_again(editor: str) -> None:
    fixture = GradeFixture(editor=editor)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route("**/*", fixture.route)
            page = await context.new_page()
            connector = service()
            target = BASE + "/mod/quiz/comment.php?attempt=134403&slot=1"
            if editor != "plain":
                # Reproduce the old implementation: only filling the hidden
                # textarea lets the editor overwrite the comment on Submit.
                await page.goto(target)
                await page.locator("input[name$='-mark']").fill("2")
                await page.locator("textarea[name$='-comment']").evaluate(
                    "(element, text) => { element.value = text; }", comment_html()
                )
                await page.locator("#id_submitbutton").click()
                assert fixture.mark == "2"
                assert fixture.comment == ""
                fixture.writes.clear()
            await connector._grade_quiz_essay(page, context, grade_request())
            assert fixture.mark == "2"
            assert fixture.comment == comment_html()
            assert len(fixture.writes) == 1
            if editor != "plain":
                assert await page.locator("#unrelated").text_content() == "Other editor"
            await connector._grade_quiz_essay(page, context, grade_request())
            assert len(fixture.writes) == 1
        finally:
            await browser.close()


async def test_editor_local_draft_is_not_a_persisted_grade_receipt() -> None:
    fixture = GradeFixture(mark="0", comment="<p>Previous comment</p>", restore_local_draft=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route("**/*", fixture.route)
            page = await context.new_page()
            await service()._grade_quiz_essay(page, context, grade_request())
            # The restored draft matches in the live DOM, but not in Moodle;
            # an actual POST must still be required to deliver it.
            assert len(fixture.writes) == 1
            assert fixture.mark == "2"
            assert fixture.comment == comment_html()
        finally:
            await browser.close()


async def test_lost_comment_never_returns_delivered_even_if_grade_was_saved() -> None:
    fixture = GradeFixture(lose_comment=True, restore_local_draft=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route("**/*", fixture.route)
            page = await context.new_page()
            connector = service()
            with pytest.raises(MoodleProtocolError, match="grade or comment readback"):
                await connector._grade_quiz_essay(page, context, grade_request())
            assert fixture.mark == "2"
            assert fixture.comment == ""
            assert len(fixture.writes) == 1
            fixture.lose_comment = False
            await connector._grade_quiz_essay(page, context, grade_request())
            assert fixture.comment == comment_html()
            assert len(fixture.writes) == 2
            await connector._grade_quiz_essay(page, context, grade_request())
            assert len(fixture.writes) == 2
        finally:
            await browser.close()


class AssignmentGradeFixture(GradeFixture):
    def markup(self) -> str:
        return (
            super()
            .markup()
            .replace(
                '<form id="manualgradingform" action="/mod/quiz/comment.php"',
                '<a href="/course/view.php?id=549">Course</a>'
                '<form action="/mod/assign/view.php?id=888&amp;action=grader&amp;userid=77"',
            )
            .replace(
                '<input type="hidden" name="attempt" value="134403">',
                '<input type="hidden" name="userid" value="77">',
            )
            .replace('<input type="hidden" name="slot" value="1">', "")
            .replace('name="q12:1_-mark"', 'name="grade"')
            .replace('name="q12:1_-comment"', 'name="assignfeedbackcomments_editor[text]"')
            .replace("id_submitbutton", "id_savegrade")
        )

    async def route(self, route: Route) -> None:
        assert route.request.url.startswith(BASE + "/mod/assign/view.php")
        if route.request.method == "POST":
            values = parse_qs(route.request.post_data or "", keep_blank_values=True)
            assert values["userid"] == ["77"]
            self.writes.append(values)
            self.mark = values["grade"][0]
            self.comment = values["assignfeedbackcomments_editor[text]"][0]
        await route.fulfill(content_type="text/html", body=self.markup())


def assignment_grade_request() -> GradeRequest:
    return GradeRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE,
            "storage_state": {"cookies": [], "origins": []},
            "payload": {
                "module": "assign",
                "course_id": "549",
                "cmid": 888,
                "user_id": "77",
                "attempt_number": 2,
                "grade": 8.5,
                "comment": COMMENT,
            },
            "idempotency_key": "assignment-grade:888:77:editor-regression",
        }
    )


@pytest.mark.parametrize("editor", ["tiny", "tiny_initializing", "tiny_late", "atto", "plain"])
async def test_assignment_editor_save_preserves_grade_and_comment(editor: str) -> None:
    fixture = AssignmentGradeFixture(editor=editor)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route("**/*", fixture.route)
            page = await context.new_page()
            connector = service()

            async def guard(*_args: object) -> None:
                return None

            connector._require_authenticated_page = guard  # type: ignore[method-assign]
            connector._require_live_assignment_grade_preconditions = guard  # type: ignore[method-assign]
            assert (
                await connector._grade_assignment_submission(
                    page, context, assignment_grade_request()
                )
                == "/mod/assign/view.php"
            )
            assert len(fixture.writes) == 1
            assert fixture.mark == "8.5"
            assert fixture.comment == comment_html()
        finally:
            await browser.close()


@pytest.mark.parametrize("module", ["quiz", "assign"])
async def test_late_unready_editor_cancelled_submit_is_not_delivery(module: str) -> None:
    fixture = (
        AssignmentGradeFixture(editor="tiny_late_initializing")
        if module == "assign"
        else GradeFixture(editor="tiny_late_initializing")
    )
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route("**/*", fixture.route)
            page = await context.new_page()
            connector = service()

            async def guard(*_args: object) -> None:
                return None

            connector._require_authenticated_page = guard  # type: ignore[method-assign]
            connector._require_live_assignment_grade_preconditions = guard  # type: ignore[method-assign]
            with pytest.raises(MoodleProtocolError, match="synchronized before save"):
                if module == "assign":
                    await connector._grade_assignment_submission(
                        page, context, assignment_grade_request()
                    )
                else:
                    await connector._grade_quiz_essay(page, context, grade_request())
            assert fixture.writes == []
            assert fixture.mark == ""
            assert fixture.comment == ""
        finally:
            await browser.close()
