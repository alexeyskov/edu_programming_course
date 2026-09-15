from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import ssl
import subprocess
import sys
import zipfile
from email import policy
from email.parser import BytesParser
from http.cookies import SimpleCookie
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Browser

from moodle_browser.config import Settings
from moodle_browser.models import (
    BrowserStorageState,
    QuizAnswersSyncRequest,
    QuizEssayPrepareRequest,
)
from moodle_browser.parsers import MoodleMarkupError
from moodle_browser.quiz import QuizAttempt
from moodle_browser.quiz_upload import (
    DraftSessionExpired,
    DraftUnavailable,
    draft_request,
    draft_url,
    parse_essay_draft,
)
from moodle_browser.service import BrowserUnavailable, MoodleBrowserService, MoodleProtocolError

BASE = "https://moodle.example.test"


def options(itemid="900", files=()):
    return {
        "itemid": itemid,
        "client_id": "manager123",
        "context": {"id": 80, "instanceid": 777},
        "maxbytes": 100000,
        "areamaxbytes": -1,
        "accepted_types": [".cpp", ".zip"],
        "author": "Student",
        "defaultlicense": "allrightsreserved",
        "filepicker": {"repositories": {"3": {"id": 3, "type": "upload"}}},
        "list": [{"filename": name, "filepath": "/", "url": url} for name, url in files],
    }


def markup(data=None, *, attempt="123", slot="1", sesskey="key1"):
    data = data or options()
    return f"""<!doctype html><html><body class="course-549">
    <a href="/login/logout.php?sesskey=fixture">Log out</a>
    <script defer src="/never-ready.js"></script>
    <form id="responseform" method="post" action="{BASE}/mod/quiz/processattempt.php?cmid=777">
      <div class="que essay" id="question-88-{slot}" data-slot="{slot}">
        <div class="qtext">Solve question {slot}</div>
        <div class="filemanager fm-loading"><button class="fp-btn-add">Add</button></div>
        <input type="hidden" name="q88:{slot}_attachments" value="{data["itemid"]}">
      </div>
      <input type="hidden" name="attempt" value="{attempt}">
      <input type="hidden" name="sesskey" value="{sesskey}">
      <input type="hidden" name="thispage" value="{int(slot) - 1}">
      <input type="hidden" name="nextpage" value="{slot}">
      <input type="hidden" name="slots" value="{slot}">
      <input type="hidden" name="q88:{slot}_:sequencecheck" value="1">
      <input id="mod_quiz-next-nav" type="submit" name="next" value="Next page">
    </form>
    <script>M.form_filemanager.init(Y, {json.dumps(data)});</script>
    </body></html>"""


def test_native_contract_reads_initial_loading_manager_without_javascript():
    file = ("main.cpp", BASE + "/draftfile.php/5/user/draft/900/main.cpp")
    draft = parse_essay_draft(
        markup(options(files=(file,))),
        base_url=BASE,
        cmid=777,
        question=QuizAttempt("123", "1", ()),
    )
    assert draft.files == (file,)
    assert draft.itemid == "900" and draft.repo_id == "3"


@pytest.mark.parametrize("fault", ["item", "slot", "attempt", "context", "duplicate", "action"])
def test_native_contract_rejects_mismatched_targets(fault):
    source = markup()
    if fault == "item":
        source = source.replace('value="900"', 'value="901"')
    elif fault == "slot":
        source = source.replace("q88:1_attachments", "q88:2_attachments")
    elif fault == "attempt":
        source = source.replace('value="123"', 'value="999"')
    elif fault == "context":
        source = markup({**options(), "context": {"id": 80, "instanceid": 778}})
    elif fault == "duplicate":
        source += f"<script>M.form_filemanager.init(Y, {json.dumps(options())});</script>"
    else:
        source = source.replace(BASE + "/mod/quiz/processattempt.php", "https://foreign.test/post")
    with pytest.raises(MoodleMarkupError):
        parse_essay_draft(source, base_url=BASE, cmid=777, question=QuizAttempt("123", "1", ()))


@pytest.mark.parametrize(
    "url",
    [
        "https://foreign.test/draftfile.php/5/user/draft/900/main.cpp",
        BASE + "/draftfile.php/5/user/draft/901/main.cpp",
        BASE + "/draftfile.php/5/user/draft/900/nested/main.cpp",
        BASE + "/draftfile.php/5/user/draft/900/other.cpp",
        BASE + "/pluginfile.php/5/user/draft/900/main.cpp",
    ],
)
def test_native_file_urls_cannot_cross_drafts_or_paths(url):
    with pytest.raises(MoodleMarkupError):
        draft_url(url, base_url=BASE, itemid="900", filename="main.cpp")


class Response:
    def __init__(self, url, content, status=200):
        self.url, self.content, self.status = url, content, status
        self.headers = {"content-length": str(len(content))}
        self.disposed = False

    async def body(self):
        return self.content

    async def dispose(self):
        self.disposed = True


@pytest.mark.asyncio
async def test_native_post_sends_fresh_form_contract_with_no_redirects():
    draft = parse_essay_draft(
        markup(), base_url=BASE, cmid=777, question=QuizAttempt("123", "1", ())
    )
    calls = []

    async def post(url, **kwargs):
        calls.append((url, kwargs))
        return Response(
            url,
            json.dumps(
                {
                    "id": "900",
                    "file": "main.cpp",
                    "url": BASE + "/draftfile.php/5/user/draft/900/main.cpp",
                }
            ).encode(),
        )

    await draft_request(
        SimpleNamespace(post=post),
        base_url=BASE,
        draft=draft,
        action="upload",
        timeout_ms=3000,
        filename="main.cpp",
        artifact=b"int main() {}",
    )
    assert calls[0][1]["max_redirects"] == 0
    fields = calls[0][1]["multipart"]
    assert fields["itemid"] == "900" and fields["overwrite"] == "0"
    assert fields["repo_upload_file"]["buffer"] == b"int main() {}"
    assert parse_qs(urlsplit(calls[0][0]).query)["accepted_types[]"] == [".cpp", ".zip"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (503, b"busy", DraftUnavailable),
        (429, b"busy", DraftUnavailable),
        (401, b"login required", DraftSessionExpired),
        (200, b'{"errorcode":"invalidsesskey"}', DraftSessionExpired),
        (200, b'{"error":"quota exceeded"}', MoodleMarkupError),
        (302, b"", MoodleMarkupError),
        (200, b'{"id":"901","file":"main.cpp"}', MoodleMarkupError),
    ],
)
async def test_native_upload_classifies_rejections_without_replaying_post(status, body, error):
    draft = parse_essay_draft(
        markup(), base_url=BASE, cmid=777, question=QuizAttempt("123", "1", ())
    )
    calls = []
    response = Response(BASE, body, status)

    async def post(url, **kwargs):
        calls.append(kwargs)
        return response

    with pytest.raises(error):
        await draft_request(
            SimpleNamespace(post=post),
            base_url=BASE,
            draft=draft,
            action="upload",
            timeout_ms=3000,
            filename="main.cpp",
            artifact=b"int main() {}",
        )
    assert len(calls) == 1 and calls[0]["max_redirects"] == 0
    assert response.disposed


@pytest.mark.asyncio
async def test_native_upload_never_overwrites_an_unproven_target(monkeypatch):
    connector = MoodleBrowserService(Settings(shared_secret=b"x" * 32, base_url=BASE))
    file = ("main.cpp", BASE + "/draftfile.php/5/user/draft/900/main.cpp")

    class Page:
        _eduprog_quiz_binding = ("549", 777, "123")
        url = BASE + "/mod/quiz/attempt.php?attempt=123&cmid=777&page=0"

        async def content(self):
            return markup(options(files=(file,)))

    with pytest.raises(MoodleProtocolError, match="ownership is unproven"):
        await connector._upload_quiz_draft(
            Page(),
            filename="main.cpp",
            artifact=b"new",
            question_slot="1",
            previous_managed_filename=None,
            previous_managed_sha256=None,
        )


class MoodleFixture:
    """Native Moodle forms and draft storage, with isolated student sessions.

    Chromium still performs all navigation, form submits and slot parsing.
    Only the remote Moodle server is replaced; no production network is used.
    """

    def __init__(self):
        self.drafts = {}
        self.saved = {}
        self.active = {}
        self.finished = set()
        self.sequence = {}
        self.posts = []
        self.requests = []

    def envelope(self, body):
        return (
            '<!doctype html><body class="course-549">'
            '<a href="/login/logout.php?sesskey=fixture">Log out</a>' + body + "</body>"
        )

    def attempt_html(self, owner, attempt, page):
        assert self.active[owner] == attempt and attempt not in self.finished
        slot = str(page + 1)
        item = str(900 + len(self.drafts))
        files = self.saved.get((attempt, slot), {}).copy()
        self.drafts[item] = (owner, attempt, slot, files)
        data = options(
            itemid=item,
            files=tuple(
                (name, BASE + f"/draftfile.php/{owner}/user/draft/{item}/{name}") for name in files
            ),
        )
        source = markup(data, attempt=attempt, slot=slot, sesskey=f"key{owner}")
        nav = "".join(
            f'<a class="qnbutton" id="quiznavbutton{number}" href="{BASE}/mod/quiz/'
            f"attempt.php?attempt={attempt}&amp;cmid=777&amp;page={number - 1}"
            f'#question-88-{number}">{number}</a>'
            for number in (1, 2)
        )
        return source.replace("</body>", nav + "</body>")

    def summary(self, owner, attempt):
        fields = {
            "attempt": attempt,
            "cmid": "777",
            "finishattempt": "1",
            "timeup": "0",
            "slots": "",
            "sesskey": f"key{owner}",
        }
        return self.envelope(
            '<form id="frm-finishattempt" '
            f'action="{BASE}/mod/quiz/processattempt.php" method="post">'
            + "".join(f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items())
            + '<button type="submit" name="submitallandfinish">'
            'Submit all and finish</button></form>'
        )

    async def browser_request(self, owner, route):
        request = route.request
        assert request.url.startswith(BASE + "/"), "Fixture must never access the network"
        path, query = urlsplit(request.url).path, parse_qs(urlsplit(request.url).query)
        self.requests.append((owner, request.method, path))
        if request.resource_type not in {"document", "fetch", "xhr"}:
            await route.abort()
            return
        await asyncio.sleep(0.003)
        if request.method == "POST":
            fields = parse_qs(request.post_data or "", keep_blank_values=True)
            if path == "/mod/quiz/startattempt.php":
                number = self.sequence.get(owner, 0) + 1
                self.sequence[owner] = number
                attempt = str(owner * 100 + number)
                assert owner not in self.active or self.active[owner] in self.finished
                self.active[owner] = attempt
                target = f"/mod/quiz/attempt.php?attempt={attempt}&cmid=777&page=0"
            elif path == "/mod/quiz/processattempt.php":
                attempt = fields["attempt"][0]
                assert self.active[owner] == attempt and fields["sesskey"] == [f"key{owner}"]
                if fields.get("finishattempt") == ["1"]:
                    assert all((attempt, slot) in self.saved for slot in ("1", "2"))
                    assert attempt not in self.finished
                    self.finished.add(attempt)
                    target = f"/mod/quiz/review.php?attempt={attempt}&cmid=777"
                else:
                    slot = fields["slots"][0]
                    item = fields[f"q88:{slot}_attachments"][0]
                    draft_owner, draft_attempt, draft_slot, files = self.drafts[item]
                    assert (draft_owner, draft_attempt, draft_slot) == (owner, attempt, slot)
                    self.saved[(attempt, slot)] = files.copy()
                    number = int(fields["nextpage"][0])
                    target = (
                        f"/mod/quiz/attempt.php?attempt={attempt}&cmid=777&page={number}"
                        if number < 2
                        else f"/mod/quiz/summary.php?attempt={attempt}&cmid=777"
                    )
            else:
                raise AssertionError("Unexpected mutation")
            await route.fulfill(status=302, headers={"Location": BASE + target})
            return
        if path == "/mod/quiz/view.php":
            attempt = self.active.get(owner)
            if attempt and attempt not in self.finished:
                body = self.envelope(
                    f'<a href="{BASE}/mod/quiz/attempt.php?'
                    f'attempt={attempt}&amp;cmid=777">Continue attempt</a>'
                )
            else:
                body = self.envelope(
                    f'<form action="{BASE}/mod/quiz/startattempt.php" method="post">'
                    '<input type="hidden" name="cmid" value="777">'
                    '<button type="submit">Attempt quiz</button></form>'
                )
        elif path == "/mod/quiz/attempt.php":
            body = self.attempt_html(owner, query["attempt"][0], int(query.get("page", ["0"])[0]))
        elif path == "/mod/quiz/summary.php":
            body = self.summary(owner, query["attempt"][0])
        elif path == "/mod/quiz/review.php":
            assert query["attempt"][0] in self.finished
            body = self.envelope(
                '<table class="quizreviewsummary"><tr><th>State</th>'
                "<td>Finished</td></tr><tr><th>Completed on</th>"
                "<td>12 September 2026</td></tr></table>"
            )
        else:
            raise AssertionError(f"Unexpected fixture page {path}")
        await route.fulfill(content_type="text/html", body=body)

    async def api_post(self, owner, url, **kwargs):
        assert kwargs["max_redirects"] == 0
        fields = kwargs.get("multipart", kwargs.get("form"))
        item = fields["itemid"]
        draft_owner, attempt, slot, files = self.drafts[item]
        assert draft_owner == owner and fields["sesskey"] == f"key{owner}"
        action = parse_qs(urlsplit(url).query)["action"][0]
        await asyncio.sleep(0.003)
        if action == "upload":
            upload = fields["repo_upload_file"]
            name = upload["name"]
            assert name not in files or fields["overwrite"] == "1"
            files[name] = upload["buffer"]
            payload = {
                "id": item,
                "file": name,
                "url": BASE + f"/draftfile.php/{owner}/user/draft/{item}/{name}",
            }
        elif action == "delete":
            assert fields["filepath"] == "/"
            del files[fields["filename"]]
            payload = {"filepath": "/"}
        else:
            raise AssertionError("Unexpected draft action")
        self.posts.append((owner, attempt, slot, action))
        return Response(url, json.dumps(payload).encode())

    async def api_get(self, owner, url, **kwargs):
        assert kwargs["max_redirects"] == 0
        path = urlsplit(url).path.split("/")
        assert path[:5] == ["", "draftfile.php", str(owner), "user", "draft"]
        draft_owner, _, _, files = self.drafts[path[5]]
        assert draft_owner == owner
        await asyncio.sleep(0.003)
        return Response(url, files[path[6]])


async def fixture_server(remote, tmp_path):
    """Real loopback TLS/HTTP including redirects and multipart, no external network."""
    cert, key = tmp_path / "fixture-cert.pem", tmp_path / "fixture-key.pem"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-keyout",
            str(key),
            "-out",
            str(cert),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert, key)
    errors = []

    async def serve(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            line, _, header_bytes = head.partition(b"\r\n")
            method, target, _ = line.decode().split(" ", 2)
            headers = BytesParser(policy=policy.default).parsebytes(header_bytes)
            body = await reader.readexactly(int(headers.get("Content-Length", "0")))
            cookie = SimpleCookie(headers.get("Cookie", ""))
            owner = int(cookie["MoodleSession"].value)
            url = BASE + target
            if target.startswith("/repository/"):
                fields = {}
                if str(headers.get("Content-Type", "")).startswith("multipart/form-data"):
                    message = BytesParser(policy=policy.default).parsebytes(header_bytes + body)
                    for part in message.iter_parts():
                        name = part.get_param("name", header="content-disposition")
                        value = part.get_payload(decode=True)
                        fields[name] = (
                            {"name": part.get_filename(), "buffer": value}
                            if part.get_filename()
                            else value.decode()
                        )
                else:
                    fields = {k: v[0] for k, v in parse_qs(body.decode()).items()}
                result = await remote.api_post(owner, url, multipart=fields, max_redirects=0)
                content, status, extra = await result.body(), result.status, {}
                content_type = "application/json"
            elif target.startswith("/draftfile.php/"):
                result = await remote.api_get(owner, url, max_redirects=0)
                content, status, extra = await result.body(), result.status, {}
                content_type = "application/octet-stream"
            else:
                output = {}

                async def fulfill(**kwargs):
                    output.update(kwargs)

                request = SimpleNamespace(
                    url=url,
                    method=method,
                    resource_type="document",
                    post_data=body.decode(),
                )
                await remote.browser_request(
                    owner, SimpleNamespace(request=request, fulfill=fulfill)
                )
                content = output.get("body", "").encode()
                status, extra = output.get("status", 200), output.get("headers", {})
                content_type = output.get("content_type", "text/html")
            response_headers = {
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
                "Connection": "close",
                **extra,
            }
            writer.write(
                f"HTTP/1.1 {status} Response\r\n".encode()
                + "".join(f"{k}: {v}\r\n" for k, v in response_headers.items()).encode()
                + b"\r\n"
                + content
            )
            await writer.drain()
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=tls)
    return server, f"https://127.0.0.1:{server.sockets[0].getsockname()[1]}", errors


@pytest.mark.skipif(os.environ.get("EDUPROG_BROWSER_DOM_TESTS") != "1", reason="opt-in Chromium")
@pytest.mark.asyncio
async def test_twenty_students_open_and_submit_two_questions_while_import_pool_is_full(
    monkeypatch,
    tmp_path,
):
    remote = MoodleFixture()
    server, base, server_errors = await fixture_server(remote, tmp_path)
    monkeypatch.setattr(sys.modules[__name__], "BASE", base)
    original_browser_context = Browser.new_context

    async def trust_fixture_certificate(browser, **kwargs):
        # Test fixture certificate only; production always validates Moodle TLS.
        return await original_browser_context(browser, **kwargs, ignore_https_errors=True)

    monkeypatch.setattr(Browser, "new_context", trust_fixture_certificate)
    connector = MoodleBrowserService(
        Settings(
            shared_secret=b"x" * 32,
            base_url=BASE,
            student_queue_wait_seconds=120,
            navigation_timeout_ms=5000,
        )
    )
    original_context = connector._new_context
    contexts = set()
    peak = 0

    async def new_context(browser, **kwargs):
        nonlocal peak
        assert kwargs.get("html_only") is True
        context = await original_context(browser, **kwargs)
        contexts.add(context)
        peak = max(peak, len(contexts))
        context.on("close", lambda *_: contexts.remove(context))
        return context

    monkeypatch.setattr(connector, "_new_context", new_context)
    save_page = connector._save_quiz_question_page
    lost_save_ack = False

    async def save_with_lost_ack(page, context, request, question):
        nonlocal lost_save_ack
        await save_page(page, context, request, question)
        if request.expected_attempt_id == "201" and not lost_save_ack:
            lost_save_ack = True
            raise BrowserUnavailable("Fixture lost acknowledgement after successful page save")

    monkeypatch.setattr(connector, "_save_quiz_question_page", save_with_lost_ack)
    requests = []

    async def student(owner, repeat=1):
        state = BrowserStorageState.model_validate(
            {
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
            }
        )
        for number in range(repeat):
            prepared = await connector.prepare_quiz_essay(
                QuizEssayPrepareRequest(
                    schema_version="1.0",
                    base_url=BASE,
                    course_id="549",
                    cmid=777,
                    storage_state=state,
                )
            )
            attempt = prepared.preparation.attempt_id
            answers = []
            for slot in ("1", "2"):
                code = (
                    f"// student {owner} attempt {number} question {slot}\n"
                    'int main() {}'
                ).encode()
                filename = "main.cpp"
                if owner == 20 or (owner == 19 and slot == "2"):
                    # Empty source remains exactly empty inside a nonempty ZIP;
                    # Moodle rejects zero-byte raw uploads, not empty solutions.
                    buffer = io.BytesIO()
                    with zipfile.ZipFile(buffer, "w") as archive:
                        archive.writestr("main.cpp", b"")
                    code, filename = buffer.getvalue(), "solution.zip"
                answers.append(
                    {
                        "question_slot": slot,
                        "answer_transport": "ESSAY_ATTACHMENT",
                        "artifact": {
                            "filename": filename,
                            "content_base64": base64.b64encode(code).decode(),
                            "sha256": hashlib.sha256(code).hexdigest(),
                        },
                    }
                )
            request = QuizAnswersSyncRequest(
                schema_version="1.0",
                base_url=BASE,
                course_id="549",
                cmid=777,
                expected_attempt_id=attempt,
                answers=answers,
                finalize=True,
                storage_state=prepared.storage_state,
                idempotency_key=f"submit:{owner}:{attempt}",
            )
            requests.append(request)
            if owner == 1:
                periodic = request.model_copy(
                    update={
                        "finalize": False,
                        "idempotency_key": f"periodic:{attempt}",
                    }
                )
                checkpoint = await connector.sync_quiz_answers(periodic)
                assert checkpoint.status == "DRAFT_SAVED"
                # Final revision is different; prove the old upload by its
                # receipt before overwriting, including on subsequent attempts.
                for answer in request.answers:
                    old = answer.artifact
                    code = base64.b64decode(old.content_base64) + b"\n// final edit"
                    answer.previous_managed_filename = old.filename
                    answer.previous_managed_sha256 = old.sha256
                    answer.artifact = old.model_copy(
                        update={
                            "content_base64": base64.b64encode(code).decode(),
                            "sha256": hashlib.sha256(code).hexdigest(),
                        }
                    )
            if owner == 2:
                with pytest.raises(BrowserUnavailable, match="lost acknowledgement"):
                    await connector.sync_quiz_answers(request)
                # Drop in-memory progress: recovery must re-read/hash Moodle,
                # not depend on the connector process surviving the failure.
                connector._quiz_answers_progress.pop(request.idempotency_key, None)
            result = await connector.sync_quiz_answers(request)
            assert result.status == "FINALIZED" and len(result.receipts) == 2
            for answer in request.answers:
                saved = remote.saved[(attempt, answer.question_slot)][answer.artifact.filename]
                assert hashlib.sha256(saved).hexdigest() == answer.artifact.sha256
                if answer.artifact.filename.endswith(".zip"):
                    with zipfile.ZipFile(io.BytesIO(saved)) as archive:
                        assert archive.read("main.cpp") == b""
            state = result.storage_state

    try:
        await connector.start()
        async with (
            connector._operation(),
            connector._operation(foreground=True),
            connector._operation(interactive=True),
        ):
            await asyncio.wait_for(asyncio.gather(*(student(i) for i in range(1, 21))), 120)
        assert len(remote.finished) == 20
        # Same student can use the next allowed attempt; no reuse of old files/IDs.
        await student(1, repeat=2)
        assert len(remote.finished) == 22
        assert not contexts and peak == connector.settings.max_concurrent_student_operations
        assert connector._pending_student_operations == 0
        assert not connector._student_session_locks
        before = len(remote.posts)
        await connector.sync_quiz_answers(requests[-1])
        assert len(remote.posts) == before  # A retry cannot create/finish another attempt.
        assert not server_errors
    except Exception:
        print("FIXTURE_REQUESTS", remote.requests[-50:])
        print("FIXTURE_ERRORS", server_errors)
        raise
    finally:
        await connector.close()
        server.close()
        await server.wait_closed()
