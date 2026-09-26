from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import zipfile
from urllib.parse import parse_qs, urlsplit

import pytest
import test_quiz_native_upload as fixture_helpers
from playwright.async_api import Browser, async_playwright

from moodle_browser.config import Settings
from moodle_browser.models import AssignmentSubmissionSyncRequest
from moodle_browser.parsers import MoodleMarkupError
from moodle_browser.quiz import QuizAttempt
from moodle_browser.quiz_upload import parse_assignment_draft
from moodle_browser.service import BrowserUnavailable, MoodleBrowserService, MoodleProtocolError

BASE = "https://moodle.example.test"


def assignment_markup(data=None, *, base=BASE, owner=1):
    data = data or fixture_helpers.options()
    return f"""<!doctype html><html><body class="course-549">
    <a href="/login/logout.php?sesskey=fixture">Log out</a>
    <script defer src="/never-ready.js"></script>
    <form class="mform" action="{base}/mod/assign/view.php" method="post">
      <input type="hidden" name="id" value="777">
      <input type="hidden" name="action" value="savesubmission">
      <input type="hidden" name="sesskey" value="key{owner}">
      <div id="fitem_id_files_filemanager">
        <input type="hidden" name="files_filemanager" value="{data["itemid"]}">
        <div class="filemanager fm-loading"><button class="fp-btn-add">Add</button></div>
      </div>
      <button type="submit" name="submitbutton">Save changes</button>
    </form>
    <script>M.form_filemanager.init(Y, {json.dumps(data)});</script>
    </body></html>"""


def test_assignment_native_contract_binds_fresh_draft_without_filepicker_js():
    file = ("main.cpp", BASE + "/draftfile.php/5/user/draft/900/main.cpp")
    result = parse_assignment_draft(
        assignment_markup(fixture_helpers.options(files=(file,))),
        base_url=BASE,
        cmid=777,
    )
    assert result.files == (file,) and result.sesskey == "key1"


@pytest.mark.parametrize("fault", ["id", "action", "method", "item", "context", "foreign"])
def test_assignment_native_contract_rejects_foreign_or_ambiguous_targets(fault):
    source = assignment_markup()
    changes = {
        "id": ('name="id" value="777"', 'name="id" value="778"'),
        "action": ('value="savesubmission"', 'value="submit"'),
        "method": ('method="post"', 'method="get"'),
        "item": ('name="files_filemanager" value="900"', 'name="files_filemanager" value="901"'),
        "foreign": (f'action="{BASE}', 'action="https://foreign.test'),
        "context": ('"instanceid": 777', '"instanceid": 778'),
    }
    source = source.replace(*changes[fault])
    with pytest.raises(MoodleMarkupError):
        parse_assignment_draft(source, base_url=BASE, cmid=777)


class AssignmentFixture:
    """Real browser forms with separate students, native drafts and server-side save."""

    def __init__(self):
        self.base = BASE
        self.drafts = {}
        self.saved = {}
        self.finished = set()
        self.posts = []

    async def browser_request(self, owner, route):
        request = route.request
        path, query = urlsplit(request.url).path, parse_qs(urlsplit(request.url).query)
        assert path == "/mod/assign/view.php", "No JS/theme/editor requests are needed"
        if request.method == "POST":
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            assert fields["id"] == ["777"] and fields["sesskey"] == [f"key{owner}"]
            assert owner not in self.finished, "Never rewrite a terminal submission"
            action = fields["action"][0]
            if action == "savesubmission":
                draft_owner, files = self.drafts[fields["files_filemanager"][0]]
                assert draft_owner == owner
                self.saved[owner] = files.copy()
                if owner % 2 == 0:  # Moodle submissions without a separate draft phase.
                    self.finished.add(owner)
            elif action == "submit":
                assert self.saved[owner]
                self.finished.add(owner)
            else:
                raise AssertionError(f"Unexpected Assignment mutation {action}")
            self.posts.append((owner, action))
            await route.fulfill(status=302, headers={"Location": self.base + path + "?id=777"})
            return
        if query.get("action") == ["editsubmission"]:
            assert owner not in self.finished
            item = str(900 + len(self.drafts))
            files = self.saved.get(owner, {}).copy()
            self.drafts[item] = (owner, files)
            data = fixture_helpers.options(
                itemid=item,
                files=tuple(
                    (name, self.base + f"/draftfile.php/{owner}/user/draft/{item}/{name}")
                    for name in files
                ),
            )
            body = assignment_markup(data, base=self.base, owner=owner)
        else:
            terminal = owner in self.finished
            body = (
                '<!doctype html><body class="course-549">'
                '<a href="/login/logout.php?sesskey=fixture">Log out</a>'
                '<div class="submissionstatustable">'
                f'<div class="submissionstatus{"submitted" if terminal else "draft"}"></div>'
            )
            for name in self.saved.get(owner, {}):
                body += (
                    f'<a href="{self.base}/pluginfile.php/{owner}/assignsubmission_file/'
                    f'submission_files/101/{name}">{name}</a>'
                )
            body += "</div>"
            if not terminal:
                body += (
                    f'<form action="{self.base}/mod/assign/view.php" method="post">'
                    '<input type="hidden" name="id" value="777">'
                    '<input type="hidden" name="action" value="submit">'
                    f'<input type="hidden" name="sesskey" value="key{owner}">'
                    '<button type="submit">Submit for grading</button></form>'
                )
            body += "</body>"
        await route.fulfill(content_type="text/html", body=body)

    async def api_post(self, owner, url, **kwargs):
        fields = kwargs["multipart"]
        item = fields["itemid"]
        draft_owner, files = self.drafts[item]
        assert owner == draft_owner and fields["sesskey"] == f"key{owner}"
        action = parse_qs(urlsplit(url).query)["action"][0]
        if action == "upload":
            upload = fields["repo_upload_file"]
            name = upload["name"]
            assert name not in files or fields["overwrite"] == "1"
            files[name] = upload["buffer"]
            result = {
                "id": item,
                "file": name,
                "url": self.base + f"/draftfile.php/{owner}/user/draft/{item}/{name}",
            }
        else:
            assert action == "delete" and fields["filepath"] == "/"
            del files[fields["filename"]]
            result = {"filepath": "/"}
        self.posts.append((owner, action))
        return fixture_helpers.Response(url, json.dumps(result).encode())

    async def api_get(self, owner, url, **kwargs):
        path = urlsplit(url).path.split("/")
        assert path[2] == str(owner)
        if path[1] == "draftfile.php":
            draft_owner, files = self.drafts[path[5]]
            assert draft_owner == owner
        else:
            assert path[1] == "pluginfile.php" and owner in self.finished
            files = self.saved[owner]
        return fixture_helpers.Response(url, files[path[-1]])


@pytest.mark.skipif(os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium")
@pytest.mark.asyncio
async def test_twenty_assignment_submissions_use_light_pool_and_recover_lost_save(
    monkeypatch,
    tmp_path,
):
    remote = AssignmentFixture()
    server, base, errors = await fixture_helpers.fixture_server(remote, tmp_path)
    remote.base = base
    monkeypatch.setattr(fixture_helpers, "BASE", base)
    original_context = Browser.new_context

    async def trust_fixture(browser, **kwargs):
        assert kwargs["java_script_enabled"] is False
        return await original_context(browser, **kwargs, ignore_https_errors=True)

    monkeypatch.setattr(Browser, "new_context", trust_fixture)
    connector = MoodleBrowserService(Settings(shared_secret=b"x" * 32, base_url=base))
    real_save = connector._save_assignment_submission
    lost_ack = set()

    async def save(page, context, request):
        result = await real_save(page, context, request)
        owner = int(request.storage_state.cookies[0].value)
        if owner in {1, 2} and owner not in lost_ack:
            lost_ack.add(owner)
            raise BrowserUnavailable("Fixture lost Save acknowledgement")
        return result

    monkeypatch.setattr(connector, "_save_assignment_submission", save)
    requests = []

    async def submit(owner):
        code, filename = f"// owner {owner}\nint main() {{}}\n".encode(), "main.cpp"
        if owner == 20:
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("main.cpp", b"")
            code, filename = buffer.getvalue(), "solution.zip"
        request = AssignmentSubmissionSyncRequest(
            schema_version="1.0",
            base_url=base,
            course_id="549",
            cmid=777,
            answer_transport="ASSIGN_FILE",
            finalize=True,
            submission_drafts=owner % 2 != 0,
            requires_submission_statement=False,
            max_submission_bytes_inherited=True,
            idempotency_key=f"assign:{owner}",
            artifact={
                "filename": filename,
                "content_base64": base64.b64encode(code).decode(),
                "sha256": hashlib.sha256(code).hexdigest(),
            },
            storage_state={
                "cookies": [
                    {
                        "name": "MoodleSession",
                        "value": str(owner),
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            },
        )
        requests.append(request)
        if owner in {1, 2}:
            with pytest.raises(BrowserUnavailable, match="lost Save"):
                await connector.sync_assignment_submission(request)
        result = await connector.sync_assignment_submission(request)
        assert result.status == "FINALIZED" and remote.saved[owner][filename] == code

    try:
        await connector.start()
        async with (
            connector._operation(),
            connector._operation(foreground=True),
            connector._operation(interactive=True),
        ):
            await asyncio.wait_for(asyncio.gather(*(submit(i) for i in range(1, 21))), 120)
        assert remote.finished == set(range(1, 21))
        assert sum(action == "upload" for _, action in remote.posts) == 20
        assert not errors
        before = len(remote.posts)
        connector._assignment_sync_cache.clear()  # Process restart loses the receipt cache.
        await connector.sync_assignment_submission(requests[1])
        assert len(remote.posts) == before
        remote.saved[2]["main.cpp"] = b"External replacement"
        connector._assignment_sync_cache.clear()
        with pytest.raises(MoodleProtocolError, match="changed"):
            await connector.sync_assignment_submission(requests[1])
        assert len(remote.posts) == before  # No overwrite of an externally changed final file.
        assert connector._pending_student_operations == 0
        assert not connector._student_session_locks
    finally:
        await connector.close()
        server.close()
        await server.wait_closed()


@pytest.mark.skipif(os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium")
@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["quiz", "assign"])
async def test_script_free_html_editor_preserves_cpp_includes_and_comparisons(module):
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(java_script_enabled=False)
        try:
            page = await context.new_page()
            name = "q88:1_answer" if module == "quiz" else "onlinetext_editor[text]"
            format_name = name + "format" if module == "quiz" else "onlinetext_editor[format]"
            await page.set_content(
                '<form class="mform"><div class="que essay" id="question-88-1" data-slot="1">'
                f'<textarea name="{name}"></textarea><input type="hidden" '
                f'name="{format_name}" value="1"></div></form>'
            )
            connector = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
            source = "#include <vector>\nif (a < b && b > c) {}\n"
            if module == "quiz":
                question = QuizAttempt("123", "1", (), online_text_control_name=name)
                await connector._replace_quiz_online_text(page, question, source.encode())
            else:
                from moodle_browser.assignment import AssignmentSubmissionForm

                form = AssignmentSubmissionForm(("ASSIGN_ONLINE_TEXT",), (), name)
                await connector._replace_assignment_online_text(page, form, source.encode())
            value = await page.locator("textarea").input_value()
            assert value.startswith("<pre>#include &lt;vector&gt;")
            assert "&lt; b &amp;&amp; b &gt;" in value
        finally:
            await context.close()
            await browser.close()
