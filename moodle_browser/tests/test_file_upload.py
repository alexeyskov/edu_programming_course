from __future__ import annotations

import asyncio
import hashlib
import json
import os
from types import SimpleNamespace

import pytest
from playwright.async_api import Locator, async_playwright

from moodle_browser.config import Settings
from moodle_browser.service import BrowserUnavailable, MoodleBrowserService, MoodleProtocolError

BASE_URL = "https://edu.mmcs.sfedu.ru"


def service() -> MoodleBrowserService:
    return MoodleBrowserService(Settings(shared_secret=b"upload-regression-test-secret-32bytes"))


class UploadResponse:
    url = BASE_URL + "/repository/repository_ajax.php?action=upload"
    request = SimpleNamespace(method="POST")

    def __init__(self, body: object, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.ok = status == 200

    async def json(self) -> object:
        return self.body


class UploadPage:
    def __init__(self, response: UploadResponse) -> None:
        self.response = response
        self.observing = False
        self.clicked = False

    def expect_response(self, predicate, **_kwargs):
        assert predicate(self.response)
        assert not predicate(SimpleNamespace(
            url="https://other.test/repository/repository_ajax.php?action=upload",
            request=SimpleNamespace(method="POST"),
        ))
        assert not predicate(SimpleNamespace(
            url=BASE_URL + "/repository/repository_ajax.php?action=list",
            request=SimpleNamespace(method="POST"),
        ))
        return self

    async def __aenter__(self):
        self.observing = True
        return self

    async def __aexit__(self, *_args):
        return False

    @property
    def value(self):
        async def result():
            assert self.clicked
            return self.response
        return result()

    async def click(self):
        assert self.observing  # Install the response listener before uploading.
        self.clicked = True


@pytest.mark.asyncio
@pytest.mark.parametrize("body,expected", [
    ({"file": "main.cpp", "id": 123}, False),
    ({"event": "fileexists"}, True),
])
async def test_upload_waits_for_response_and_reports_overwrite(body, expected):
    page = UploadPage(UploadResponse(body))
    assert await service()._upload_repository_file(page, page) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["upload_error_invalid_file", "invalidfiletype", "maxbytesfile"])
async def test_http_200_upload_error_is_not_mistaken_for_a_missing_dom_filename(code):
    page = UploadPage(UploadResponse({"error": "private Moodle debug HTML", "errorcode": code}))
    with pytest.raises(MoodleProtocolError, match=f"Moodle upload rejected: {code}") as caught:
        await service()._upload_repository_file(page, page)
    assert "private" not in str(caught.value)


@pytest.mark.asyncio
async def test_unknown_upload_error_does_not_expose_moodle_debug_details():
    page = UploadPage(UploadResponse({"error": "secret", "errorcode": "secret URL"}))
    with pytest.raises(MoodleProtocolError, match="repository_error") as caught:
        await service()._upload_repository_file(page, page)
    assert "secret" not in str(caught.value)


@pytest.mark.asyncio
async def test_upload_http_failure_is_reported_immediately():
    page = UploadPage(UploadResponse({}, status=503))
    with pytest.raises(BrowserUnavailable, match="503"):
        await service()._upload_repository_file(page, page)


@pytest.mark.asyncio
@pytest.mark.parametrize("selected", [False, True])
async def test_upload_repository_waits_for_initial_render_and_only_switches_if_needed(
    monkeypatch, selected
):
    events = []

    class Control:
        @property
        def first(self):
            return self

        async def wait_for(self, *, state, **_kwargs):
            assert state == "detached"  # An invisible loading indicator is still loading.
            events.append("rendered")

        async def evaluate(self, _script):
            events.append("selection_checked")
            return selected

        async def click(self):
            events.append("clicked")

    control = Control()
    page = SimpleNamespace(get_by_text=lambda *_args: control, locator=lambda _selector: control)
    uploader = service()

    async def unique(locator, **_kwargs):
        return locator

    monkeypatch.setattr(uploader, "_wait_for_unique_locator", unique)
    await uploader._select_upload_repository(page)
    assert events == ["rendered", "selection_checked"] + (
        [] if selected else ["clicked", "rendered"]
    )


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("module", ["quiz", "assign"])
@pytest.mark.parametrize("recent_repo", ["upload", "recent"])
@pytest.mark.parametrize("active_marker", ["class", "aria"])
async def test_auto_selected_upload_repository_does_not_reset_the_selected_file(
    monkeypatch, recent_repo, active_marker, module
):
    """Moodle opens the recent repo itself; selecting it again races its response.

    A delayed second `list` response recreates the upload form after the file
    was selected. Make that ordering deterministic, as on a busy browser, so
    this regression cannot accidentally pass because the upload click wins.
    """
    markup = """
    <div id="fitem_id_files_filemanager" class="que essay" data-slot="1"><div class="filemanager">
      <button class="fp-btn-add">Add</button><div id="files"></div>
    </div></div>
    <div id="picker" class="file-picker" style="display:none">
      <div class="fp-repo" id="upload"><button id="repo">Upload a file</button></div>
      <div class="fp-repo" id="recent">Recent files</div>
      <div class="fp-content"></div>
    </div>
    <script>
    window.renders=0;
    async function list(repository) {
      const content=document.querySelector('.fp-content');
      content.innerHTML='<div class="fp-content-loading fp-content-hidden"' +
        ' style="visibility:hidden"></div>';
      await fetch('/repository/repository_ajax.php?action=list', {method:'POST'});
      document.querySelectorAll('.fp-repo').forEach(e=>{
        e.classList.remove('active'); e.setAttribute('aria-selected','false');
      });
      const selected=document.getElementById(repository);
      if('ACTIVE_MARKER' === 'class') selected.classList.add('active');
      else selected.setAttribute('aria-selected','true');
      if(repository !== 'upload') { content.textContent='Recent files'; return; }
      content.innerHTML='<input type="file"><button class="fp-upload-btn">Upload</button>';
      window.renders++;
      content.querySelector('input').onchange=()=>window.fileWasSelected();
      content.querySelector('button').onclick=async () => {
        const file=content.querySelector('input').files[0];
        if(!file) return; // Moodle shows "nofile" and sends no upload request.
        const data=new FormData(); data.append('repo_upload_file',file);
        await fetch('/repository/repository_ajax.php?action=upload', {method:'POST',body:data});
        document.querySelector('#files').textContent=file.name;
        document.querySelector('#picker').style.display='none';
      };
    }
    document.querySelector('.fp-btn-add').onclick=()=>{
      document.querySelector('#picker').style.display='block'; list('RECENT_REPO');
    };
    document.querySelector('#repo').onclick=()=>list('upload');
    </script>
    """.replace("RECENT_REPO", recent_repo).replace("ACTIVE_MARKER", active_marker)
    listing_requests = 0
    uploads = []
    selected = asyncio.Event()

    async def respond(route):
        nonlocal listing_requests
        if "action=list" in route.request.url:
            listing_requests += 1
            if recent_repo == "upload" and listing_requests == 2:
                await selected.wait()
            else:
                await asyncio.sleep(0.1)
            await route.fulfill(status=200, content_type="application/json", body="{}")
        elif "action=upload" in route.request.url:
            uploads.append(route.request.post_data_buffer)
            await route.fulfill(
                status=200, content_type="application/json", body='{"file":"main.cpp"}'
            )
        else:
            await route.fulfill(status=200, content_type="text/html", body=markup)

    original_set_files = Locator.set_input_files

    async def select_then_allow_pending_response(locator, *args, **kwargs):
        await original_set_files(locator, *args, **kwargs)
        if recent_repo == "upload" and listing_requests > 1:
            await page.wait_for_function("window.renders === 2")

    monkeypatch.setattr(Locator, "set_input_files", select_then_allow_pending_response)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.expose_function("fileWasSelected", selected.set)
            await page.route("**/*", respond)
            await page.goto(BASE_URL + "/mod/quiz/attempt.php?attempt=123&cmid=31529")
            uploader = MoodleBrowserService(Settings(
                shared_secret=b"upload-regression-test-secret-32bytes", navigation_timeout_ms=1_000
            ))
            source = b"int main() { return 0; }\n"
            if module == "quiz":
                await uploader._replace_quiz_attachment(
                    page, "main.cpp", source, replace_existing=False, question_slot="1"
                )
            else:
                await uploader._replace_assignment_file(
                    page, "main.cpp", source, replace_existing=False
                )
            assert listing_requests == (1 if recent_repo == "upload" else 2)
            assert len(uploads) == 1 and source in uploads[0]
            assert await page.locator('#files').text_content() == "main.cpp"
        finally:
            await browser.close()


# Opt-in real Chromium regression: no live Moodle, credentials or external
# writes. All requests are intercepted by the fixture below.
@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("overwrite", [False, True])
async def test_slow_upload_and_late_overwrite_stay_bound_to_the_correct_question(overwrite):
    markup = """
    <div class="que essay" data-slot="1">
      <div class="filemanager"><button class="fp-btn-add">Add</button><div id="files"></div></div>
    </div>
    <div class="que essay" data-slot="2">
      <div class="filemanager"><button class="fp-btn-add">Add</button><span>other.cpp</span></div>
    </div>
    <div class="file-picker" style="display:none"><input type="file"></div>
    <div id="picker" class="file-picker" style="display:none">
      <button id="repo">Upload a file</button>
      <div id="inputs" style="display:none">
        <input type="file"><button class="fp-upload-btn">Upload</button>
      </div>
    </div>
    <button class="fp-dlg-butoverwrite" style="display:none">Overwrite</button>
    <script>
    let target, filename;
    window.overwrites = 0;
    document.querySelectorAll('.fp-btn-add').forEach(e => e.onclick = () => {
      target=e.closest('.filemanager'); document.querySelector('#picker').style.display='block';
    });
    document.querySelector('#repo').onclick = () => {
      document.querySelector('#inputs').style.display='block';
    };
    function finish() {
      const entry=document.createElement('span'); entry.textContent=filename; target.append(entry);
      document.querySelector('#picker').style.display='none';
    }
    document.querySelector('.fp-upload-btn').onclick = async () => {
      const input=document.querySelector('#inputs input');
      filename=input.files[0].name;
      const data=new FormData(); data.append('repo_upload_file',input.files[0]);
      const response=await fetch('/repository/repository_ajax.php?action=upload', {
        method:'POST',body:data
      });
      const result=await response.json();
      if(result.event==='fileexists') {
        document.querySelector('.fp-dlg-butoverwrite').style.display='block';
      }
      else finish();
    };
    document.querySelector('.fp-dlg-butoverwrite').onclick = e => {
      window.overwrites++; e.target.style.display='none'; finish();
    };
    </script>
    """
    uploads = []

    async def respond(route):
        if route.request.method == "POST":
            uploads.append(route.request.post_data_buffer)
            # Longer than the old 1.5-second overwrite probe.
            await asyncio.sleep(2)
            await route.fulfill(
                status=200, content_type="text/html",
                body=json.dumps({"event": "fileexists"} if overwrite else {"file": "main.cpp"}),
            )
        else:
            await route.fulfill(status=200, content_type="text/html", body=markup)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.route("**/*", respond)
            await page.goto(BASE_URL + "/mod/quiz/attempt.php?attempt=123&cmid=31529")
            source = b"int main() { return 42; }\n"
            await service()._replace_quiz_attachment(
                page, "main.cpp", source, replace_existing=False, question_slot="1",
                previous_managed_filename="main.cpp" if overwrite else None,
                previous_managed_sha256=hashlib.sha256(b"old").hexdigest() if overwrite else None,
            )
            assert len(uploads) == 1 and source in uploads[0]
            assert await page.locator('[data-slot="1"] .filemanager').get_by_text(
                "main.cpp", exact=True
            ).count() == 1
            assert await page.locator('[data-slot="2"] .filemanager span').all_text_contents() == [
                "other.cpp"
            ]
            assert await page.evaluate("window.overwrites") == int(overwrite)
        finally:
            await browser.close()


@pytest.mark.skipif(
    os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium DOM test"
)
@pytest.mark.asyncio
@pytest.mark.parametrize("verified,path", [(True, "/"), (False, "/"), (True, "/nested/")])
async def test_packaging_change_only_removes_a_verified_root_file(monkeypatch, verified, path):
    markup = """
    <div class="que essay" data-slot="2"><div class="filemanager fm-loaded">
      <span class="fp-filename" id="old">submission.zip</span>
      <span class="fp-filename">other.cpp</span>
    </div></div>
    <div id="details" class="moodle-dialogue" style="display:none">
      <div class="fp-saveas"><input value="submission.zip"></div>
      <div class="fp-path"><select><option value="ROOT_PATH">Root</option></select></div>
      <button class="fp-file-delete">Delete</button>
    </div>
    <div role="dialog" id="confirm" style="display:none">
      <button data-action="save">Yes</button>
    </div>
    <script>
    window.deleted = false;
    document.querySelector('#old').onclick=()=>document.querySelector('#details').style.display='block';
    document.querySelector('.fp-file-delete').onclick=()=>{
      document.querySelector('#details').style.display='none';
      document.querySelector('#confirm').style.display='block';
    };
    document.querySelector('[data-action=save]').onclick=()=>{
      window.deleted=true;
      document.querySelector('#confirm').style.display='none';
      document.querySelector('#old').style.display='none';
    };
    </script>
    """.replace("ROOT_PATH", path)
    uploader = service()

    async def verify(*_args, **kwargs):
        assert kwargs["expected_sha256"] == hashlib.sha256(b"archive").hexdigest()
        return verified

    monkeypatch.setattr(uploader, "_verify_quiz_managed_file_if_exposed", verify)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(markup)
            arguments = dict(
                filename="submission.zip", expected_sha256=hashlib.sha256(b"archive").hexdigest(),
                attachment_urls=((
                    "submission.zip", BASE_URL + "/draftfile.php/1/user/draft/2/submission.zip"
                ),),
                question_slot="2",
            )
            manager = page.locator('.que.essay .filemanager')
            if verified and path == "/":
                await uploader._remove_previous_quiz_attachment(page, manager, **arguments)
                # Real Moodle also retains the hidden, old view entry.
                assert await page.locator('#old').count() == 1
                assert not await page.locator('#old').is_visible()
                assert await page.evaluate('window.deleted') is True
            else:
                with pytest.raises(MoodleProtocolError):
                    await uploader._remove_previous_quiz_attachment(page, manager, **arguments)
                assert await page.evaluate('window.deleted') is False
            assert await manager.get_by_text('other.cpp', exact=True).is_visible()
        finally:
            await browser.close()
