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
    READ_ATTEMPT_METHOD,
    missing_quiz_attempt_response,
    teacher_session_key,
)
from moodle_browser.config import Settings
from moodle_browser.models import HistoricalSubmissionsRequest
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
        return json.dumps(self.payload).encode()

    async def dispose(self) -> None:
        self.disposed = True


class FakeRequests:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.calls: list[dict[str, Any]] = []
        self.responses: list[FakeResponse] = []

    async def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        reply = self.replies[len(self.calls) - 1]
        if isinstance(reply, BaseException):
            raise reply
        response = FakeResponse(url, reply)
        self.responses.append(response)
        return response


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
