from __future__ import annotations

import asyncio
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from pydantic import ValidationError

from moodle_browser import service as service_module
from moodle_browser.config import Settings
from moodle_browser.models import HistoricalArtifact, HistoricalSubmissionsRequest
from moodle_browser.service import MoodleBrowserService

BASE = "https://edu.mmcs.sfedu.ru"
STATE = {
    "cookies": [
        {
            "name": "MoodleSession",
            "value": "fixture",
            "domain": "edu.mmcs.sfedu.ru",
            "path": "/",
            "expires": -1,
        }
    ],
    "origins": [],
}


def ref(attempt: str = "9001", user: str = "77"):
    return {
        "attempt_id": attempt,
        "user_id": user,
        "display_name": "Student",
        "state": "SUBMITTED",
        "submitted_at_epoch": 0,
        "grade": None,
        "grade_max": None,
    }


def request(**values):
    return HistoricalSubmissionsRequest.model_validate(
        {
            "schema_version": "1.0",
            "base_url": BASE,
            "course_id": "549",
            "actor_external_subject": "42",
            "activity": {"module": "quiz", "cmid": 30354},
            "storage_state": STATE,
            **values,
        }
    )


class InventoryService(MoodleBrowserService):
    def __init__(self):
        super().__init__(Settings(shared_secret=b"x" * 32))
        self._browser = SimpleNamespace(is_connected=lambda: True)
        self.urls = []
        self.details = []
        self.closed = 0

    async def _new_context(self, _browser, **kwargs):
        assert kwargs["server_rendered"] != kwargs["html_only"]

        async def new_page():
            return SimpleNamespace(url="")

        async def close():
            self.closed += 1

        return SimpleNamespace(new_page=new_page, close=close)

    async def _goto(self, page, url):
        self.urls.append(url)
        page.url = url
        rows = "".join(
            f'<tr><td><a href="/user/view.php?id={user}">Student {user}</a></td>'
            '<td>Завершены</td><td><a href="/mod/quiz/review.php?'
            f'attempt={9000 + user}&amp;cmid=30354">Review</a></td></tr>'
            for user in range(1, 121)
        )
        return (
            f'<body class="course-549"><table id="attempts"><tbody>{rows}</tbody></table></body>'
        )

    async def _require_authenticated_page(self, *_args):
        pass

    async def _state(self, _context):
        return STATE

    async def _probe_deleted_attempts(self, _context, _report, candidates, **scope):
        assert candidates == ["8888"]
        assert scope == {"course_id": "549", "cmid": 30354}
        return candidates, False

    async def _historical_detail(self, _page, _context, **kwargs):
        self.details.append(kwargs)
        return {"responses": [{"response_id": "1", "answer_text": "int main() {}"}]}, None


@pytest.fixture
def connector(monkeypatch):
    monkeypatch.setattr(service_module, "parse_identity", lambda *_: {"external_subject": "42"})
    monkeypatch.setattr(service_module, "teacher_controls_present", lambda *_: True)
    return InventoryService()


@pytest.mark.asyncio
async def test_inventory_lists_entire_report_and_deletions_without_opening_answers(connector):
    # A report with 120 students must not take 24 repeated course/report reads
    # just to discover who submitted; slow file downloads are independent.
    result = await connector.discover_historical_submissions(
        request(
            scan_only=True,
            priority_only=True,
            limit=5,
            known_attempt_ids=["8888"],
        )
    )
    assert result.scan_only and result.complete
    assert len(result.candidates) == 120 and result.items == []
    assert result.deleted_attempt_ids == ["8888"]
    assert connector.details == [] and len(connector.urls) == 2
    assert connector.closed == 1
    query = parse_qs(urlsplit(connector.urls[-1]).query, keep_blank_values=True)
    assert query["onlygraded"] == ["0"]
    assert query["tifirst"] == query["tilast"] == [""]


@pytest.mark.asyncio
async def test_manual_initial_inventory_enriches_only_requested_activity(connector, monkeypatch):
    course = {
        "sections": [{
            "external_id": "section-1", "title": "Лабораторные", "position": 0,
            "activities": [
                {"cmid": 30354, "module": "quiz", "name": "Самостоятельная 1"},
                {"cmid": 999, "module": "assign", "name": "Лабораторная 2"},
            ],
        }],
    }
    monkeypatch.setattr(service_module, "parse_course_page", lambda *_: course)
    captured = []

    async def enrich(_page, _context, course_id, scoped, **kwargs):
        assert course_id == "549" and kwargs["budget_seconds"] == 60.0
        captured.extend(scoped["sections"][0]["activities"])
        return scoped

    monkeypatch.setattr(connector, "_enrich_course_activities", enrich)
    result = await connector.discover_historical_submissions(
        request(scan_only=True, include_activity_metadata=True)
    )
    assert [activity["cmid"] for activity in captured] == [30354]
    assert result.activity_metadata.cmid == 30354
    assert "ACTIVITY_METADATA_INCOMPLETE" in result.warnings
    assert result.items == [] and connector.details == []
    assert len(connector.urls) == 2


@pytest.mark.asyncio
async def test_manual_deleted_attempt_batch_never_opens_report_or_answers(connector):
    result = await connector.discover_historical_submissions(
        request(scan_only=True, probe_only=True, known_attempt_ids=["8888"])
    )
    assert result.deleted_attempt_ids == ["8888"]
    assert result.complete and result.next_cursor is None
    assert result.items == result.candidates == []
    assert result.activity_metadata is None
    assert len(connector.urls) == 1 and connector.details == []


@pytest.mark.asyncio
async def test_manual_incomplete_deletion_batch_is_not_reported_as_clean(connector, monkeypatch):
    async def incomplete(*_args, **_kwargs):
        return [], True

    monkeypatch.setattr(connector, "_probe_deleted_attempts", incomplete)
    result = await connector.discover_historical_submissions(
        request(scan_only=True, probe_only=True, known_attempt_ids=["8888"])
    )
    assert result.deleted_attempt_ids == []
    assert result.warnings == ["DELETION_CHECK_INCOMPLETE"]


@pytest.mark.parametrize("values", [
    {"include_activity_metadata": True},
    {"include_activity_metadata": True, "scan_only": True, "cursor": "1:0"},
    {"probe_only": True},
    {"probe_only": True, "scan_only": True},
    {"probe_only": True, "scan_only": True, "known_attempt_ids": ["8888"],
     "include_activity_metadata": True},
    {"probe_only": True, "scan_only": True, "known_attempt_ids": ["8888"],
     "activity": {"module": "assign", "cmid": 30354}},
])
def test_manual_metadata_and_deletion_batches_reject_mixed_modes(values):
    with pytest.raises(ValidationError):
        request(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,attempt,user", [("quiz", "9001", "77"), ("assign", "user-77-attempt-2", "77")]
)
async def test_detail_pins_attempt_instead_of_refetching_moving_report(
    connector, module, attempt, user
):
    result = await connector.discover_historical_submissions(
        request(
            activity={"module": module, "cmid": 30354},
            attempt_refs=[ref(attempt, user)],
            limit=1,
        )
    )
    assert len(connector.urls) == 1  # Only the authentication/course check.
    assert len(connector.details) == 1 and result.complete
    assert result.items[0].attempt_id == attempt
    assert result.items[0].responses[0].answer_text == "int main() {}"
    url = connector.details[0]["detail_url"]
    assert url.startswith(BASE)
    assert ("attempt=9001" if module == "quiz" else "attemptnumber=2") in url


@pytest.mark.asyncio
async def test_reference_delivery_returns_every_attachment_without_spending_binary_budget(
    connector, monkeypatch
):
    async def detail(*_args, **_kwargs):
        return {
            "responses": [
                {
                    "response_id": str(slot),
                    "_artifact_links": [{
                        "external_id": str(slot) * 64,
                        "filename": f"solution-{slot}.7z",
                        "url": f"{BASE}/pluginfile.php/1/solution-{slot}.7z",
                    }],
                }
                for slot in (1, 2)
            ]
        }, None

    async def unexpected_download(*_args, **_kwargs):
        pytest.fail("reference delivery must not load binary archives into the connector")

    monkeypatch.setattr(connector, "_historical_detail", detail)
    monkeypatch.setattr(connector, "_historical_artifact", unexpected_download)
    result = await connector.discover_historical_submissions(
        request(attempt_refs=[ref()], limit=1, attachment_delivery="reference")
    )
    assert result.complete and not result.warnings
    assert len(result.items[0].responses) == 2
    for response in result.items[0].responses:
        artifact = response.artifacts[0]
        assert artifact.omission_reason == "DEFERRED_DOWNLOAD" and not artifact.downloaded
        assert artifact.download_url.startswith(f"{BASE}/pluginfile.php/")
        assert artifact.content_base64 == artifact.sha256 == ""


@pytest.mark.parametrize("fields", [
    {"downloaded": False, "omission_reason": "DEFERRED_DOWNLOAD"},
    {"downloaded": False, "omission_reason": "FILE_TOO_LARGE", "download_url": BASE},
    {"downloaded": True, "omission_reason": "DEFERRED_DOWNLOAD", "download_url": BASE},
])
def test_attachment_reference_requires_a_consistent_non_inline_contract(fields):
    with pytest.raises(ValidationError):
        HistoricalArtifact(external_id="a" * 64, filename="test.7z", **fields)


@pytest.mark.asyncio
async def test_history_pool_remains_available_during_heavy_and_foreground_course_reads(connector):
    async with connector._operation(), connector._course_discovery_operation(foreground=True):
        result = await connector.discover_historical_submissions(request(scan_only=True))
        assert len(result.candidates) == 120


@pytest.mark.asyncio
async def test_cancelled_history_returns_its_slot(connector):
    entered = asyncio.Event()

    async def hold():
        async with connector._history_operation():
            entered.set()
            await asyncio.Event().wait()

    running = asyncio.create_task(hold())
    await entered.wait()
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    assert connector._history_read_semaphore._value == 2


@pytest.mark.asyncio
async def test_history_waits_for_a_short_read_instead_of_rejecting_after_two_seconds(connector):
    entered = 0
    occupied = asyncio.Event()
    release = asyncio.Event()
    third_entered = asyncio.Event()

    async def hold():
        nonlocal entered
        async with connector._history_operation():
            entered += 1
            if entered == 2:
                occupied.set()
            await release.wait()

    async def queued():
        async with connector._history_operation():
            third_entered.set()

    tasks = [asyncio.create_task(hold()) for _ in range(2)]
    await asyncio.wait_for(occupied.wait(), timeout=1)
    tasks.append(asyncio.create_task(queued()))
    try:
        await asyncio.sleep(2.1)
        assert not tasks[-1].done()
        assert not third_entered.is_set()
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert third_entered.is_set()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert connector._history_read_semaphore._value == 2


@pytest.mark.asyncio
async def test_cancelled_queued_history_does_not_leak_or_inflate_slots(connector):
    async with connector._history_operation(), connector._history_operation():
        async def queued():
            async with connector._history_operation():
                pytest.fail("The two history slots are still occupied")

        waiting = asyncio.create_task(queued())
        await asyncio.sleep(0)
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        assert connector._history_read_semaphore._value == 0
    assert connector._history_read_semaphore._value == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"scan_only": True, "attempt_refs": [ref()]},
        {"attempt_refs": [ref("../../9001")]},
        {"attempt_refs": [ref(), ref()]},
        {"attempt_refs": [{**ref(), "url": "https://attacker.test/"}]},
        {
            "activity": {"module": "assign", "cmid": 30354},
            "attempt_refs": [ref("user-78-attempt-1")],
        },
    ],
)
def test_inventory_contract_rejects_ambiguous_and_foreign_targets(changes):
    with pytest.raises(ValidationError):
        request(**changes)
