from __future__ import annotations

import base64
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from conftest import BASE_URL, SHARED_SECRET, storage_state

from moodle_browser.assignment import (
    AssignmentSubmissionForm,
    AssignmentSubmissionView,
    parse_assignment_confirmation_page,
    parse_assignment_edit_page,
    parse_assignment_view_page,
)
from moodle_browser.config import Settings
from moodle_browser.models import (
    AssignmentSubmissionPrepareRequest,
    AssignmentSubmissionSyncRequest,
    BrowserStorageState,
)
from moodle_browser.parsers import MoodleMarkupError, parse_activity_settings
from moodle_browser.service import (
    MoodleAttemptFinalized,
    MoodleBrowserService,
    MoodleProtocolError,
    _managed_target_replace_existing,
)

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def request(
    *,
    transport: str = "ASSIGN_FILE",
    finalize: bool = False,
    drafts: bool | None = True,
    statement: bool = False,
    inherited_maxbytes: bool = False,
) -> AssignmentSubmissionSyncRequest:
    artifact = b"int main() { return 0; }\n"
    return AssignmentSubmissionSyncRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "549",
            "cmid": 777,
            "answer_transport": transport,
            "artifact": {
                "filename": "main.cpp",
                "content_base64": base64.b64encode(artifact).decode("ascii"),
                "sha256": hashlib.sha256(artifact).hexdigest(),
            },
            "finalize": finalize,
            "requires_submission_statement": statement,
            "submission_drafts": drafts,
            "max_submission_bytes_inherited": inherited_maxbytes,
            "idempotency_key": "assign:549:777:student:1",
            "storage_state": storage_state(),
        }
    )


@pytest.mark.asyncio
async def test_assignment_prepare_proves_live_student_form_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = AssignmentSubmissionPrepareRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "549",
            "cmid": 777,
            "storage_state": storage_state(),
        }
    )

    class FakeContext:
        async def new_page(self) -> object:
            return object()

        async def close(self) -> None:
            return None

    @asynccontextmanager
    async def operation(**_kwargs: object):  # type: ignore[no-untyped-def]
        yield object()

    async def new_context(*_args: object, **_kwargs: object) -> FakeContext:
        return FakeContext()

    async def open_form(*_args: object) -> AssignmentSubmissionForm:
        return AssignmentSubmissionForm(
            ("ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"),
            (),
            "onlinetext_editor[text]",
        )

    async def state(*_args: object) -> BrowserStorageState:
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_open_assignment_submission", open_form)
    monkeypatch.setattr(service, "_state", state)

    response = await service.prepare_assignment_submission(payload)

    assert response.status == "READY"
    assert response.preparation.answer_transport == "ASSIGN_FILE"
    assert response.preparation.available_answer_transports == [
        "ASSIGN_ONLINE_TEXT",
        "ASSIGN_FILE",
    ]


def test_assignment_settings_choose_file_and_capture_confirmed_constraints() -> None:
    html = """
    <html><body class='path-mod-assign' data-courseid='549'>
      <form class='mform'>
        <input name='coursemodule' value='777'>
        <input name='course' value='549'>
        <input name='modulename' value='assign'>
        <input name='allowsubmissionsfromdate[enabled]' type='checkbox'>
        <input name='duedate[enabled]' type='checkbox'>
        <input name='cutoffdate[enabled]' type='checkbox'>
        <select name='attemptreopenmethod'><option selected value='none'>none</option></select>
        <input type='checkbox' checked name='assignsubmission_onlinetext_enabled' value='1'>
        <input type='checkbox' checked name='assignsubmission_file_enabled' value='1'>
        <input type='checkbox' name='submissiondrafts' value='1'>
        <input type='checkbox' checked name='requiresubmissionstatement' value='1'>
        <input type='checkbox' name='teamsubmission' value='1'>
        <select name='assignsubmission_file_maxfiles'>
          <option selected value='3'>3</option>
        </select>
        <select name='assignsubmission_file_maxsizebytes'>
          <option selected value='1048576'>1 MiB</option>
        </select>
        <input name='assignsubmission_file_filetypes' value='.cpp,.zip'>
      </form>
    </body></html>
    """

    result = parse_activity_settings(html, course_id="549", cmid=777, module="assign")

    assert result["answer_transport"] == "ASSIGN_FILE"
    assert result["available_answer_transports"] == [
        "ASSIGN_ONLINE_TEXT",
        "ASSIGN_FILE",
    ]
    assert result["submission_drafts"] is False
    assert result["requires_submission_statement"] is True
    assert result["team_submission"] is False
    assert result["max_submission_files"] == 3
    assert result["max_submission_bytes"] == 1_048_576
    assert result["accepted_file_types"] == ".cpp,.zip"
    assert result["file_types_confirmed"] is True
    assert result["import_supported"] is True


def test_mmcs_assignment_settings_accept_nested_filetypes_and_inherited_maxsize() -> None:
    html = """
    <html><body class='path-mod-assign' data-courseid='549'>
      <form class='mform'>
        <input name='coursemodule' value='23461'>
        <input name='course' value='549'>
        <input name='modulename' value='assign'>
        <input name='allowsubmissionsfromdate[enabled]' type='checkbox'>
        <input name='duedate[enabled]' type='checkbox'>
        <input name='cutoffdate[enabled]' type='checkbox'>
        <select name='attemptreopenmethod'><option selected value='none'>none</option></select>
        <input type='hidden' name='assignsubmission_onlinetext_enabled' value='0'>
        <input type='hidden' name='assignsubmission_file_enabled' value='1'>
        <input type='hidden' name='submissiondrafts' value='0'>
        <input type='hidden' name='requiresubmissionstatement' value='0'>
        <select name='requiresubmissionstatement'>
          <option value='0'>Нет</option><option selected value='1'>Да</option>
        </select>
        <input type='hidden' name='teamsubmission' value='0'>
        <select name='assignsubmission_file_maxfiles'>
          <option selected value='2'>2</option>
        </select>
        <select name='assignsubmission_file_maxsizebytes'>
          <option selected value='0'>course limit</option>
        </select>
        <input name='assignsubmission_file_filetypes[filetypes]' value='.c,.cpp,.zip'>
      </form>
    </body></html>
    """

    result = parse_activity_settings(html, course_id="549", cmid=23461, module="assign")

    assert result["answer_transport"] == "ASSIGN_FILE"
    assert result["submission_drafts"] is False
    assert result["requires_submission_statement"] is True
    assert result["max_submission_files"] == 2
    assert "max_submission_bytes" not in result
    assert result["max_submission_bytes_inherited"] is True
    assert result["accepted_file_types"] == ".c,.cpp,.zip"
    assert result["file_types_confirmed"] is True
    assert result["import_supported"] is True


def test_assignment_edit_and_status_pages_are_bound_to_canonical_controls() -> None:
    edit = parse_assignment_edit_page(
        fixture("assignment_edit.html"),
        f"{BASE_URL}/mod/assign/view.php?id=777&action=editsubmission",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert edit == AssignmentSubmissionForm(
        ("ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"),
        ("solution.cpp", "teacher-notes.txt"),
        "onlinetext_editor[text]",
        "submissionstatement",
        2_097_152,
    )
    managed = parse_assignment_edit_page(
        fixture("assignment_edit_managed_attachment.html"),
        f"{BASE_URL}/mod/assign/view.php?id=777&action=editsubmission",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert managed.existing_filenames == ("main.cpp",)
    assert managed.attachment_urls == (
        (
            "main.cpp",
            f"{BASE_URL}/draftfile.php/5/user/draft/321/main.cpp?forcedownload=1",
        ),
    )
    draft = parse_assignment_view_page(
        fixture("assignment_view_draft.html"),
        f"{BASE_URL}/mod/assign/view.php?id=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert draft == AssignmentSubmissionView("DRAFT_SAVED", True)
    submitted = parse_assignment_view_page(
        fixture("assignment_view_submitted.html"),
        f"{BASE_URL}/mod/assign/view.php?id=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )
    assert submitted == AssignmentSubmissionView("FINALIZED", False, True)
    assert parse_assignment_confirmation_page(
        fixture("assignment_confirm.html"),
        f"{BASE_URL}/mod/assign/submissionconfirmform.php?id=777",
        base_url=BASE_URL,
        course_id="549",
        cmid=777,
    )

    with pytest.raises(MoodleMarkupError, match="identifiers changed"):
        parse_assignment_edit_page(
            fixture("assignment_edit.html"),
            f"{BASE_URL}/mod/assign/view.php?id=778&action=editsubmission",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
        )

    conflicting_limit = fixture("assignment_edit.html").replace(
        '<div class="filemanager">',
        '<div class="filemanager" data-maxbytes="1048576">',
        1,
    )
    with pytest.raises(MoodleMarkupError, match="effective file limit is ambiguous"):
        parse_assignment_edit_page(
            conflicting_limit,
            f"{BASE_URL}/mod/assign/view.php?id=777&action=editsubmission",
            base_url=BASE_URL,
            course_id="549",
            cmid=777,
        )


def test_existing_target_requires_a_receipt_for_that_exact_managed_slot() -> None:
    assert _managed_target_replace_existing((), "submission.zip", None) is False
    assert (
        _managed_target_replace_existing(
            ("main.cpp",),
            "submission.zip",
            "main.cpp",
        )
        is False
    )
    assert (
        _managed_target_replace_existing(
            ("main.cpp",),
            "main.cpp",
            "main.cpp",
        )
        is True
    )
    with pytest.raises(MoodleProtocolError, match="ownership is unproven"):
        _managed_target_replace_existing(("main.cpp",), "main.cpp", None)


def test_assignment_settings_require_known_submission_statement_policy() -> None:
    html = """
    <html><body class='path-mod-assign' data-courseid='549'>
      <form class='mform'>
        <input name='coursemodule' value='777'>
        <input name='course' value='549'>
        <input name='modulename' value='assign'>
        <input name='allowsubmissionsfromdate[enabled]' type='checkbox'>
        <input name='duedate[enabled]' type='checkbox'>
        <input name='cutoffdate[enabled]' type='checkbox'>
        <select name='attemptreopenmethod'><option selected value='none'>none</option></select>
        <input type='hidden' name='assignsubmission_onlinetext_enabled' value='1'>
        <input type='hidden' name='assignsubmission_file_enabled' value='0'>
        <input type='hidden' name='submissiondrafts' value='0'>
        <input type='hidden' name='teamsubmission' value='0'>
      </form>
    </body></html>
    """

    result = parse_activity_settings(html, course_id="549", cmid=777, module="assign")

    assert "requires_submission_statement" not in result
    assert result["import_supported"] is False


@pytest.mark.asyncio
async def test_required_statement_on_non_draft_form_is_checked_before_save() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    checked: list[str] = []

    class FakeControl:
        async def count(self) -> int:
            return 1

        async def check(self) -> None:
            checked.append("submissionstatement")

    class FakePage:
        def locator(self, selector: str) -> FakeControl:
            assert selector == "form.mform input[name='submissionstatement'][type='checkbox']"
            return FakeControl()

    submission = AssignmentSubmissionForm(
        ("ASSIGN_FILE",),
        (),
        None,
        "submissionstatement",
    )
    accepted = await service._accept_assignment_submission_statement(
        FakePage(),  # type: ignore[arg-type]
        request(drafts=False, statement=True),
        submission,
    )
    assert accepted is True
    assert checked == ["submissionstatement"]

    assert (
        await service._accept_assignment_submission_statement(
            object(),  # type: ignore[arg-type]
            request(drafts=False, statement=True),
            AssignmentSubmissionForm(("ASSIGN_FILE",), (), None),
        )
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport", "saved_status", "expected_write"),
    [
        ("ASSIGN_FILE", "FINALIZED", "file"),
        ("ASSIGN_ONLINE_TEXT", "DRAFT_SAVED", "online"),
    ],
)
async def test_assignment_workflow_accepts_submitted_save_without_drafts(
    monkeypatch: pytest.MonkeyPatch,
    transport: str,
    saved_status: str,
    expected_write: str,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(
        transport=transport,
        drafts=saved_status != "FINALIZED",
    )
    artifact = service._decode_artifact(payload)
    events: list[str] = []
    form = AssignmentSubmissionForm(
        (transport,),  # type: ignore[arg-type]
        (),
        "onlinetext_editor[text]" if transport == "ASSIGN_ONLINE_TEXT" else None,
    )

    async def open_form(*_args: object) -> AssignmentSubmissionForm:
        events.append("open")
        return form

    async def statement(*_args: object) -> bool:
        events.append("statement")
        return False

    async def file_write(*_args: object, **_kwargs: object) -> None:
        events.append("file")

    async def online_write(*_args: object) -> None:
        events.append("online")

    async def save(*_args: object) -> AssignmentSubmissionView:
        events.append("save")
        return AssignmentSubmissionView(saved_status, saved_status == "DRAFT_SAVED")  # type: ignore[arg-type]

    async def read_back(*_args: object) -> None:
        events.append("read-back")

    async def state(*_args: object) -> BrowserStorageState:
        events.append("state")
        return BrowserStorageState.model_validate(storage_state())

    monkeypatch.setattr(service, "_open_assignment_submission", open_form)
    monkeypatch.setattr(service, "_accept_assignment_submission_statement", statement)
    monkeypatch.setattr(service, "_replace_assignment_file", file_write)
    monkeypatch.setattr(service, "_replace_assignment_online_text", online_write)
    monkeypatch.setattr(service, "_save_assignment_submission", save)
    monkeypatch.setattr(service, "_return_to_assignment_submission", read_back)
    monkeypatch.setattr(service, "_state", state)

    response = await service._execute_assignment_submission_sync(
        object(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        payload,
        artifact,
    )

    assert response.status == saved_status
    expected_events = ["open", "statement", expected_write, "save"]
    if saved_status != "FINALIZED":
        expected_events.append("read-back")
    expected_events.append("state")
    assert events == expected_events


@pytest.mark.asyncio
async def test_assignment_transport_is_revalidated_on_the_actual_student_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale course projection must fail before Moodle is mutated.

    Publication is intentionally transport-agnostic.  The browser connector is
    the boundary that proves the current response controls when a real attempt
    is saved.
    """

    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(transport="ASSIGN_FILE", drafts=True)
    artifact = service._decode_artifact(payload)
    writes: list[str] = []

    async def open_form(*_args: object) -> AssignmentSubmissionForm:
        return AssignmentSubmissionForm(
            ("ASSIGN_ONLINE_TEXT",),
            (),
            "onlinetext_editor[text]",
        )

    async def file_write(*_args: object, **_kwargs: object) -> None:
        writes.append("file")

    async def online_write(*_args: object) -> None:
        writes.append("online")

    monkeypatch.setattr(service, "_open_assignment_submission", open_form)
    monkeypatch.setattr(service, "_replace_assignment_file", file_write)
    monkeypatch.setattr(service, "_replace_assignment_online_text", online_write)

    with pytest.raises(MoodleProtocolError, match="response format no longer matches"):
        await service._execute_assignment_submission_sync(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
            artifact,
        )
    assert writes == []


@pytest.mark.asyncio
async def test_finalized_assignment_preflight_always_stops_before_editing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))

    class FakePage:
        url = f"{BASE_URL}/mod/assign/view.php?id=777"

    async def goto(page: FakePage, url: str) -> str:
        page.url = url
        return fixture("assignment_view_submitted.html")

    async def authenticated(*_args: object) -> None:
        return None

    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)

    with pytest.raises(MoodleAttemptFinalized, match="already finalized"):
        await service._open_assignment_submission(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            request(drafts=True),
        )
    managed = request(drafts=False).model_copy(
        update={
            "previous_managed_filename": "main.cpp",
            "previous_managed_sha256": request(drafts=False).artifact.sha256,
        }
    )
    with pytest.raises(MoodleAttemptFinalized, match="already finalized"):
        await service._open_assignment_submission(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            managed,
        )


@pytest.mark.asyncio
async def test_assignment_finalized_during_readback_is_terminal() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(drafts=True)
    artifact = service._decode_artifact(payload)

    class FakePage:
        url = f"{BASE_URL}/mod/assign/view.php?id=777"

        async def content(self) -> str:
            return fixture("assignment_view_submitted.html")

    with pytest.raises(MoodleAttemptFinalized, match="finalized while"):
        await service._return_to_assignment_submission(
            FakePage(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
            AssignmentSubmissionForm(("ASSIGN_FILE",), ("main.cpp",), None),
            artifact,
        )


@pytest.mark.asyncio
async def test_managed_filename_change_fails_before_upload_when_old_file_remains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    base = request(drafts=False)
    payload = base.model_copy(
        update={
            "artifact": base.artifact.model_copy(update={"filename": "submission.zip"}),
            "previous_managed_filename": "main.cpp",
            "previous_managed_sha256": base.artifact.sha256,
        }
    )
    artifact = service._decode_artifact(payload)
    wrote = False

    async def open_form(*_args: object) -> AssignmentSubmissionForm:
        return AssignmentSubmissionForm(
            ("ASSIGN_FILE",),
            ("main.cpp",),
            None,
        )

    async def statement(*_args: object) -> bool:
        return False

    async def write(*_args: object, **_kwargs: object) -> None:
        nonlocal wrote
        wrote = True

    monkeypatch.setattr(service, "_open_assignment_submission", open_form)
    monkeypatch.setattr(service, "_accept_assignment_submission_statement", statement)
    monkeypatch.setattr(service, "_replace_assignment_file", write)

    with pytest.raises(MoodleProtocolError, match="safe replacement"):
        await service._execute_assignment_submission_sync(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
            artifact,
        )
    assert wrote is False


class _FakeArtifactResponse:
    def __init__(self, url: str, content: bytes, *, status: int = 200) -> None:
        self.url = url
        self.status = status
        self.headers = {"content-length": str(len(content))}
        self._content = content
        self.disposed = False

    async def body(self) -> bytes:
        return self._content

    async def dispose(self) -> None:
        self.disposed = True


class _FakeArtifactRequest:
    def __init__(self, response: _FakeArtifactResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> _FakeArtifactResponse:
        self.calls.append((url, kwargs))
        return self.response


class _FakeArtifactPage:
    def __init__(self, request_context: _FakeArtifactRequest) -> None:
        self.context = type("FakeContext", (), {"request": request_context})()


@pytest.mark.asyncio
async def test_managed_assignment_overwrite_requires_matching_remote_digest() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    content = b"int main() { return 0; }\n"
    url = f"{BASE_URL}/draftfile.php/5/user/draft/321/main.cpp?forcedownload=1"
    response = _FakeArtifactResponse(url, content)
    request_context = _FakeArtifactRequest(response)

    await service._verify_assignment_managed_file(
        _FakeArtifactPage(request_context),  # type: ignore[arg-type]
        filename="main.cpp",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        attachment_urls=(("main.cpp", url),),
    )

    assert len(request_context.calls) == 1
    assert request_context.calls[0][1]["max_redirects"] == 0
    assert response.disposed is True

    changed = _FakeArtifactResponse(url, b"changed outside the connector\n")
    with pytest.raises(MoodleProtocolError, match="changed after"):
        await service._verify_assignment_managed_file(
            _FakeArtifactPage(_FakeArtifactRequest(changed)),  # type: ignore[arg-type]
            filename="main.cpp",
            expected_sha256=hashlib.sha256(content).hexdigest(),
            attachment_urls=(("main.cpp", url),),
        )
    assert changed.disposed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "urls",
    [
        (),
        (
            ("main.cpp", f"{BASE_URL}/draftfile.php/a/main.cpp"),
            ("main.cpp", f"{BASE_URL}/draftfile.php/b/main.cpp"),
        ),
    ],
)
async def test_managed_assignment_overwrite_rejects_absent_or_ambiguous_url(
    urls: tuple[tuple[str, str], ...],
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    response = _FakeArtifactResponse(f"{BASE_URL}/unused", b"unused")
    request_context = _FakeArtifactRequest(response)

    with pytest.raises(MoodleProtocolError, match="unambiguous download URL"):
        await service._verify_assignment_managed_file(
            _FakeArtifactPage(request_context),  # type: ignore[arg-type]
            filename="main.cpp",
            expected_sha256="a" * 64,
            attachment_urls=urls,
        )
    assert request_context.calls == []


@pytest.mark.asyncio
async def test_managed_assignment_overwrite_rejects_download_failure() -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    url = f"{BASE_URL}/draftfile.php/5/user/draft/321/main.cpp"
    response = _FakeArtifactResponse(url, b"error", status=503)

    with pytest.raises(MoodleProtocolError, match="downloaded safely"):
        await service._verify_assignment_managed_file(
            _FakeArtifactPage(_FakeArtifactRequest(response)),  # type: ignore[arg-type]
            filename="main.cpp",
            expected_sha256="a" * 64,
            attachment_urls=(("main.cpp", url),),
        )
    assert response.disposed is True


@pytest.mark.asyncio
async def test_inherited_assignment_limit_must_be_resolved_before_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=SHARED_SECRET))
    payload = request(inherited_maxbytes=True)
    artifact = service._decode_artifact(payload)
    wrote = False

    async def open_form(*_args: object) -> AssignmentSubmissionForm:
        return AssignmentSubmissionForm(("ASSIGN_FILE",), (), None)

    async def write(*_args: object, **_kwargs: object) -> None:
        nonlocal wrote
        wrote = True

    monkeypatch.setattr(service, "_open_assignment_submission", open_form)
    monkeypatch.setattr(service, "_replace_assignment_file", write)

    with pytest.raises(MoodleProtocolError, match="inherited file limit"):
        await service._execute_assignment_submission_sync(
            object(),  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            payload,
            artifact,
        )
    assert wrote is False
