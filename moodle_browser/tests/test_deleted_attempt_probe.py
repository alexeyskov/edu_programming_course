from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import Error as PlaywrightError
from pydantic import ValidationError

from moodle_browser import service as service_module
from moodle_browser.attempt_probe import (
    ALL_ATTEMPT_STATES,
    READ_ATTEMPT_METHOD,
    missing_quiz_attempt_response,
    parse_unfiltered_attempt_report,
    teacher_session_key,
    unknown_missing_record_response,
)
from moodle_browser.config import Settings
from moodle_browser.models import HistoricalSubmissionsRequest
from moodle_browser.parsers import MoodleMarkupError
from moodle_browser.service import MoodleBrowserService, MoodleSessionExpired

BASE_URL = "https://edu.mmcs.sfedu.ru"
REPORT = f'<a href="{BASE_URL}/login/logout.php?sesskey=abcdefgh12">Logout</a>'


def failure(code: str, message: str, *, module: str = "moodle") -> list[dict[str, Any]]:
    return [
        {
            "error": True,
            "exception": {
                "errorcode": code,
                "message": message,
                "moreinfourl": f"https://docs.moodle.org/402/ru/error/{module}/{code}",
            },
        }
    ]


@pytest.mark.parametrize(
    "message",
    [
        "Can not find data record in database table quiz_attempts.",
        "Не удается найти запись в таблице quiz_attempts базы данных.",
        "Can not find data record in database table <b>quiz_attempts</b>.",
    ],
)
def test_missing_record_is_explicit_and_not_language_dependent(message: str) -> None:
    assert missing_quiz_attempt_response(failure("invalidrecord", message))


@pytest.mark.parametrize(
    "payload",
    [
        failure("invalidrecord", "Cannot find record in question_usages"),
        failure("invalidrecord", "Cannot find record in external_functions"),
        failure("invalidrecord", "Cannot find record in old_quiz_attempts"),
        failure("invalidrecordunknown", "Cannot find data record in database"),
        failure("dmlreadexception", "Database unavailable quiz_attempts"),
        failure("attempterrorcontentchange", "quiz_attempts", module="quiz"),
        failure("attempterrorinvalid", "quiz_attempts", module="quiz"),
        failure("invalidrecord", "quiz_attempts", module="quiz"),
        failure("nopermissions", "quiz_attempts"),
        failure("invalidsesskey", "quiz_attempts"),
        [{"error": False, "data": "quiz_attempts invalidrecord"}],
        [
            {
                "error": "true",
                "exception": failure("invalidrecord", "quiz_attempts")[0]["exception"],
            }
        ],
        {"errorcode": "invalidrecord", "message": "quiz_attempts"},
        [],
        "<div class='errorbox'>invalidrecord quiz_attempts</div>",
    ],
)
def test_ambiguous_missing_or_failed_pages_do_not_prove_deletion(payload: Any) -> None:
    assert not missing_quiz_attempt_response(payload)


def test_teacher_key_requires_unique_same_origin_evidence() -> None:
    assert teacher_session_key(REPORT, base_url=BASE_URL) == "abcdefgh12"
    assert (
        teacher_session_key('<input name="sesskey" value="abcdefgh12">', base_url=BASE_URL)
        == "abcdefgh12"
    )
    assert (
        teacher_session_key(
            REPORT + '<input name="sesskey" value="otherkey12">', base_url=BASE_URL
        )
        is None
    )
    assert (
        teacher_session_key(REPORT.replace(BASE_URL, "https://attacker.test"), base_url=BASE_URL)
        is None
    )
    assert (
        teacher_session_key('<script>var sesskey="abcdefgh12"</script>', base_url=BASE_URL) is None
    )


class FakeResponse:
    def __init__(self, url: str, payload: Any, status: int = 200) -> None:
        self.url = url
        self.payload = payload
        self.status = status
        self.disposed = False

    async def body(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()

    async def dispose(self) -> None:
        self.disposed = True


class FakeRequests:
    def __init__(self, replies: list[Any], pages: list[Any] | None = None) -> None:
        self.replies = replies
        self.pages = pages or []
        self.calls: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []
        self.responses: list[FakeResponse] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        reply = self.replies[len(self.calls) - 1]
        if isinstance(reply, BaseException):
            raise reply
        response = FakeResponse(url, reply)
        self.responses.append(response)
        return response

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.reads.append({"url": url, **kwargs})
        reply = self.pages[len(self.reads) - 1]
        if isinstance(reply, BaseException):
            raise reply
        response = FakeResponse(url, reply.encode())
        self.responses.append(response)
        return response


def full_report(*attempts: str, next_page: int | None = None) -> str:
    states = "".join(
        f'<input type="checkbox" name="state{state}" checked>'
        for state in ALL_ATTEMPT_STATES.split("-")
    )
    rows = "".join(
        f'<tr><td><a href="/user/view.php?id=77">Student</a></td><td>Finished</td>'
        f'<td><a href="/mod/quiz/review.php?attempt={attempt}&amp;cmid=31529">'
        'Review</a></td></tr>' for attempt in attempts
    )
    results = (
        f'<table id="attempts"><tbody>{rows}</tbody></table>' if rows
        else '<div class="alert-info">Нечего показывать</div>'
    )
    paging = (
        f'<a href="/mod/quiz/report.php?id=31529&amp;page={next_page}">Next</a>'
        if next_page is not None else ""
    )
    return f'''<body class="course-508">{REPORT}<main>
        <select name="group"><option value="0" selected>All groups</option></select>
        <form><input name="id" value="31529"><input name="mode" value="overview">
        <select name="attempts"><option value="all_with" selected>All</option></select>
        {states}<input name="onlygraded" value="0"><input name="onlyregraded" value="0">
        <input type="checkbox" name="onlyregraded" value="1"></form>
        <div class="initialbar firstinitial"><li class="initialbarall active">All</li></div>
        <div class="initialbar lastinitial"><li class="initialbarall active">All</li></div>
        {results}{paging}</main></body>'''


def test_generic_missing_record_needs_separate_evidence() -> None:
    generic = failure("invalidrecordunknown", "Cannot find data record in database")
    assert unknown_missing_record_response(generic)
    assert not missing_quiz_attempt_response(generic)
    assert not unknown_missing_record_response(failure("nopermissions", "Not found"))
    assert not unknown_missing_record_response(failure("invalidrecordunknown", "x", module="quiz"))


@pytest.mark.parametrize("before,after", [
    ('value="all_with"', 'value="enrolled_with"'),
    ('value="0" selected', 'value="7" selected'),
    ('name="stateabandoned" checked', 'name="stateabandoned"'),
    ('name="stateinprogress"', 'name="unsupported"'),
    ('name="onlygraded" value="0"', 'name="onlygraded" value="1"'),
    ('name="onlyregraded" value="1"', 'name="onlyregraded" value="1" checked'),
    ('initialbarall active', 'initialbarletter active'),
    ('class="initialbar lastinitial"', 'class="unknown"'),
    ('name="id" value="31529"', 'name="id" value="999"'),
    ('name="mode" value="overview"', 'name="mode" value="responses"'),
    ('course-508', 'course-999'),
    ('class="alert-info"', 'class="alert-danger"'),
    ('Нечего показывать', 'Unknown or incomplete report'),
])
def test_filtered_or_unverified_report_cannot_corroborate_deletion(before, after) -> None:
    with pytest.raises(MoodleMarkupError):
        parse_unfiltered_attempt_report(
            full_report().replace(before, after), base_url=BASE_URL,
            course_id="508", cmid=31529, page_number=0,
        )


def test_report_rejects_unidentified_attempts() -> None:
    with pytest.raises(MoodleMarkupError):
        parse_unfiltered_attempt_report(
            full_report("9001").replace('/user/view.php?id=77', '/unknown'),
            base_url=BASE_URL, course_id="508", cmid=31529, page_number=0,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("pages,expected", [
    ([full_report()], (["9001"], False)),
    ([full_report("9001")], ([], False)),
    ([full_report("9002", next_page=1), full_report("9001")], ([], False)),
    ([full_report("9002", next_page=1), full_report("9003")], (["9001"], False)),
    ([full_report("9002", next_page=1), PlaywrightError("Timeout")], ([], True)),
    ([full_report().replace('all_with', 'enrolled_with')], ([], True)),
    ([full_report("9002", next_page=1), full_report("9002")], ([], True)),
    ([full_report(str(10 + page), next_page=page + 1) for page in range(5)], ([], True)),
])
async def test_generic_missing_record_requires_a_complete_global_report(
    monkeypatch, pages, expected
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([failure("invalidrecordunknown", "Not found")], pages)

    async def authenticated(*_args):
        pass

    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    assert await service._probe_deleted_attempts(
        SimpleNamespace(request=requests), REPORT, ["9001"], course_id="508", cmid=31529
    ) == expected
    assert len(requests.reads) == len(pages)
    for page, call in enumerate(requests.reads):
        params = parse_qs(urlsplit(call["url"]).query, keep_blank_values=True)
        assert params == {
            "id": ["31529"], "mode": ["overview"], "attempts": ["all_with"],
            "group": ["0"], "onlygraded": ["0"], "onlyregraded": ["0"],
            "states": [ALL_ATTEMPT_STATES], "tifirst": [""], "tilast": [""],
            "pagesize": ["500"], "page": [str(page)],
        }
        assert call["max_redirects"] == 0 and call["timeout"] == 7_000
    assert all(response.disposed for response in requests.responses)


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["invalidrecordunknown", "nopermissions", "dmlreadexception"])
async def test_generic_failure_without_scope_never_uses_report_absence(code) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([failure(code, "Not found")])
    assert await service._probe_deleted_attempts(
        SimpleNamespace(request=requests), REPORT, ["9001"]
    ) == ([], True)
    assert requests.reads == []


@pytest.mark.asyncio
async def test_fallback_expired_session_never_confirms_deletion(monkeypatch) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([failure("invalidrecordunknown", "Not found")], [full_report()])

    async def expired(*_args):
        raise MoodleSessionExpired("Expired")

    monkeypatch.setattr(service, "_require_authenticated_page", expired)
    with pytest.raises(MoodleSessionExpired):
        await service._probe_deleted_attempts(
            SimpleNamespace(request=requests), REPORT, ["9001"], course_id="508", cmid=31529
        )
    assert all(response.disposed for response in requests.responses)


@pytest.mark.asyncio
async def test_report_is_read_once_per_batch_and_never_substitutes_for_missing_record(monkeypatch):
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([
        failure("invalidrecordunknown", "Not found"),
        failure("invalidrecordunknown", "Not found"),
        failure("nopermissions", "Not found"),
        failure("invalidrecord", "Missing question_usages"),
    ], [full_report()])

    async def authenticated(*_args):
        pass

    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    assert await service._probe_deleted_attempts(
        SimpleNamespace(request=requests), REPORT, ["9001", "9002", "9003", "9004"],
        course_id="508", cmid=31529,
    ) == (["9001", "9002"], True)
    assert len(requests.reads) == 1


@pytest.mark.asyncio
async def test_probes_only_known_ids_using_read_only_not_reopen_operation() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests(
        [
            failure("invalidrecord", "Missing quiz_attempts record"),
            failure("reopenattemptwrongstate", "Attempt is finished", module="quiz"),
            failure("nopermissions", "No permission"),
        ]
    )
    deleted, incomplete = await service._probe_deleted_attempts(
        SimpleNamespace(request=requests), REPORT, ["9001", "9002", "9003"]
    )
    assert deleted == ["9001"]
    assert incomplete is True
    for attempt_id, call in zip([9001, 9002, 9003], requests.calls, strict=True):
        target = urlsplit(call["url"])
        assert target.scheme == "https" and target.netloc == "edu.mmcs.sfedu.ru"
        assert target.path == "/lib/ajax/service.php"
        assert parse_qs(target.query) == {"sesskey": ["abcdefgh12"], "info": [READ_ATTEMPT_METHOD]}
        assert call["data"] == [
            {
                "index": 0,
                "methodname": READ_ATTEMPT_METHOD,
                "args": {"attemptid": attempt_id},
            }
        ]
        assert call["max_redirects"] == 0
        assert call["timeout"] == 3_000
    assert all(response.disposed for response in requests.responses)


@pytest.mark.asyncio
async def test_network_failure_or_missing_key_cannot_delete_local_attempts() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([PlaywrightError("private transport details")])
    context = SimpleNamespace(request=requests)
    assert await service._probe_deleted_attempts(context, REPORT, ["9001"]) == ([], True)
    assert await service._probe_deleted_attempts(context, "", ["9001"]) == ([], True)
    assert await service._probe_deleted_attempts(context, REPORT, []) == ([], False)
    assert len(requests.calls) == 1


@pytest.mark.asyncio
async def test_expired_session_propagates_and_response_is_closed() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([failure("invalidsesskey", "Expired")])
    with pytest.raises(MoodleSessionExpired):
        await service._probe_deleted_attempts(SimpleNamespace(request=requests), REPORT, ["9001"])
    assert requests.responses[0].disposed


@pytest.mark.asyncio
async def test_cancelled_probe_is_not_a_deleted_attempt() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests([asyncio.CancelledError()])
    with pytest.raises(asyncio.CancelledError):
        await service._probe_deleted_attempts(SimpleNamespace(request=requests), REPORT, ["9001"])


@pytest.mark.parametrize(
    "module,ids",
    [
        ("quiz", ["1", "2", "3", "4", "5", "6"]),
        ("quiz", ["1", "1"]),
        ("quiz", ["0"]),
        ("quiz", ["1&cmid=2"]),
        ("assign", ["1"]),
    ],
)
def test_probe_request_is_bounded_and_quiz_scoped(module: str, ids: list[str]) -> None:
    with pytest.raises(ValidationError):
        HistoricalSubmissionsRequest.model_validate(
            {
                "schema_version": "1.0",
                "base_url": BASE_URL,
                "course_id": "508",
                "actor_external_subject": "4376",
                "activity": {"module": module, "cmid": 31529},
                "known_attempt_ids": ids,
                "storage_state": {"cookies": [], "origins": []},
            }
        )


@pytest.mark.asyncio
async def test_each_known_id_is_checked_even_when_earlier_requests_fail() -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests(
        [
            *[PlaywrightError("Timeout") for _ in range(4)],
            failure("invalidrecord", "Missing quiz_attempts record"),
        ]
    )
    result = await service._probe_deleted_attempts(
        SimpleNamespace(request=requests), REPORT, ["1", "2", "3", "4", "5"]
    )
    assert result == (["5"], True)
    assert len(requests.calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("unavailable", [False, True])
async def test_history_runs_scoped_probe_only_after_teacher_and_report_validation(
    monkeypatch: pytest.MonkeyPatch,
    unavailable: bool,
) -> None:
    service = MoodleBrowserService(Settings(shared_secret=b"x" * 32))
    requests = FakeRequests(
        [
            PlaywrightError("Unavailable")
            if unavailable
            else failure("invalidrecord", "Missing quiz_attempts record"),
        ]
    )
    state = {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": "opaque",
                "domain": "edu.mmcs.sfedu.ru",
                "path": "/",
                "expires": -1.0,
            }
        ],
        "origins": [],
    }
    request = HistoricalSubmissionsRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE_URL,
            "course_id": "508",
            "actor_external_subject": "4376",
            "activity": {"module": "quiz", "cmid": 31529},
            "known_attempt_ids": ["9001"],
            "storage_state": state,
        }
    )
    page = SimpleNamespace(url="")
    closed = False

    async def new_page() -> Any:
        return page

    async def close() -> None:
        nonlocal closed
        closed = True

    context = SimpleNamespace(request=requests, new_page=new_page, close=close)

    @asynccontextmanager
    async def operation():
        yield object()

    async def new_context(*_args: Any, **_kwargs: Any) -> Any:
        return context

    async def goto(_page: Any, url: str) -> str:
        page.url = url
        assert not requests.calls
        return (
            REPORT + '<body class="course-508"><table id="attempts"><tbody></tbody></table></body>'
        )

    async def authenticated(*_args: Any) -> None:
        assert not requests.calls

    async def get_state(_context: Any) -> Any:
        return request.storage_state

    monkeypatch.setattr(service, "_history_operation", operation)
    monkeypatch.setattr(service, "_new_context", new_context)
    monkeypatch.setattr(service, "_goto", goto)
    monkeypatch.setattr(service, "_require_authenticated_page", authenticated)
    monkeypatch.setattr(service, "_state", get_state)
    monkeypatch.setattr(service_module, "parse_identity", lambda *_: {"external_subject": "4376"})
    monkeypatch.setattr(service_module, "teacher_controls_present", lambda *_: True)
    result = await service.discover_historical_submissions(request)
    assert result.items == []
    assert result.deleted_attempt_ids == ([] if unavailable else ["9001"])
    assert result.warnings == []
    assert result.complete is True
    assert len(requests.calls) == 1
    assert closed is True

    # A failed teacher gate must never probe or retire previously known IDs.
    requests.calls.clear()
    monkeypatch.setattr(service_module, "teacher_controls_present", lambda *_: False)
    with pytest.raises(service_module.TeacherMembershipRequired):
        await service.discover_historical_submissions(request)
    assert requests.calls == []
