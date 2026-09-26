from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.integrations.errors import IntegrationProtocolError
from app.integrations.moodle_browser import MoodleBrowserClient
from tests.test_moodle_browser_client import settings, storage_state


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {"cmid": 777, "module": "quiz", "name": "Самостоятельная 1"},
    {"cmid": 778, "module": "quiz", "name": "Another activity"},
    {"cmid": 777, "module": "assign", "name": "Another module"},
    None,
])
async def test_manual_inventory_activity_metadata_is_required_and_scoped(metadata):
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["include_activity_metadata"] and payload["scan_only"]
        return httpx.Response(200, json={
            "course_id": "549", "activity": {"module": "quiz", "cmid": 777},
            "activity_metadata": metadata, "scan_only": True, "items": [],
            "complete": True, "storage_state": storage_state(),
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(), client, service_url="http://moodle-browser:8082",
            shared_secret="b" * 32, storage_state=storage_state(),
        )
        kwargs = dict(course_id="549", actor_external_subject="42", module="quiz",
                      cmid=777, scan_only=True, include_activity_metadata=True)
        if metadata and metadata["cmid"] == 777 and metadata["module"] == "quiz":
            result = await browser.discover_historical_submissions(**kwargs)
            assert result.activity_metadata == metadata
        else:
            with pytest.raises(IntegrationProtocolError):
                await browser.discover_historical_submissions(**kwargs)


@pytest.mark.asyncio
async def test_manual_probe_only_batch_is_forwarded_without_requiring_report():
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["probe_only"] and payload["known_attempt_ids"] == ["9001"]
        return httpx.Response(200, json={
            "course_id": "549", "activity": {"module": "quiz", "cmid": 777},
            "scan_only": True, "items": [], "deleted_attempt_ids": ["9001"],
            "complete": True, "storage_state": storage_state(),
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(), client, service_url="http://moodle-browser:8082",
            shared_secret="b" * 32, storage_state=storage_state(),
        )
        result = await browser.discover_historical_submissions(
            course_id="549", actor_external_subject="42", module="quiz", cmid=777,
            scan_only=True, probe_only=True, known_attempt_ids=["9001"],
        )
        assert result.deleted_attempt_ids == ("9001",)


@pytest.mark.asyncio
async def test_roster_retains_teacher_also_enrolled_as_student():
    async with httpx.AsyncClient() as client:
        browser = MoodleBrowserClient(
            settings(), client, service_url="http://moodle-browser:8082",
            shared_secret="b" * 32, storage_state=storage_state(),
        )
        result = browser._normalise_participants([{
            "user_id": "42", "display_name": "Teacher", "role": "TEACHER",
            "roles": ["TEACHER", "STUDENT"], "groups": [],
        }])
        assert result[0]["roles"] == ["TEACHER", "STUDENT"]


@pytest.mark.asyncio
async def test_inventory_can_list_more_attempts_than_detail_limit_without_downloading_answers():
    candidates = [
        {
            "attempt_id": str(9000 + i),
            "user_id": str(i),
            "display_name": f"Student {i}",
            "state": "SUBMITTED",
            "submitted_at_epoch": 0,
            "grade": None,
            "grade_max": None,
        }
        for i in range(1, 121)
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        sent = json.loads(request.content)
        assert sent["scan_only"] is True and sent["limit"] == 5
        return httpx.Response(
            200,
            json={
                "course_id": "549",
                "activity": {"module": "quiz", "cmid": 777},
                "scan_only": True,
                "items": [],
                "candidates": candidates,
                "next_cursor": "1:0",
                "complete": False,
                "warnings": [],
                "storage_state": storage_state(),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.discover_historical_submissions(
            course_id="549",
            actor_external_subject="42",
            module="quiz",
            cmid=777,
            limit=5,
            scan_only=True,
        )
    assert len(result.candidates) == 120 and result.items == ()
    assert result.next_cursor == "1:0" and not result.complete


@pytest.mark.parametrize(
    "refs",
    [
        [{"attempt_id": "../1", "user_id": "42", "display_name": "Student", "state": "SUBMITTED"}],
        [
            {
                "attempt_id": "1",
                "user_id": "42",
                "display_name": "Student",
                "state": "SUBMITTED",
                "grade": float("nan"),
            }
        ],
        [
            {
                "attempt_id": "1",
                "user_id": "42",
                "display_name": "Student",
                "state": "SUBMITTED",
                "submitted_at_epoch": True,
            }
        ],
        "9001",
    ],
)
def test_inventory_references_reject_malformed_values(refs):
    from app.integrations.moodle_browser import normalize_history_attempt_refs

    with pytest.raises(IntegrationProtocolError):
        normalize_history_attempt_refs(refs, "quiz", maximum=500)


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [[], ["9001"], None])
async def test_history_deletion_proof_is_optional_and_scoped(deleted: list[str] | None) -> None:
    captured: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "course_id": "549",
                "activity": {"module": "quiz", "cmid": 777},
                "items": [],
                "next_cursor": None,
                "complete": True,
                "warnings": [],
                "storage_state": storage_state(),
                **({"deleted_attempt_ids": deleted} if deleted is not None else {}),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        result = await browser.discover_historical_submissions(
            course_id="549",
            actor_external_subject="42",
            module="quiz",
            cmid=777,
            known_attempt_ids=["9001", "9002"],
        )
    assert captured[0]["known_attempt_ids"] == ["9001", "9002"]
    assert result.deleted_attempt_ids == tuple(deleted or [])


@pytest.mark.asyncio
@pytest.mark.parametrize("deleted", [["9009"], ["9001", "9001"], ["0"], "9001", None])
async def test_invalid_deletion_evidence_cannot_reach_reconciliation(deleted: Any) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "course_id": "549",
                "activity": {"module": "quiz", "cmid": 777},
                "items": [],
                "next_cursor": None,
                "complete": True,
                "warnings": [],
                "storage_state": storage_state(),
                "deleted_attempt_ids": deleted,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError):
            await browser.discover_historical_submissions(
                course_id="549",
                actor_external_subject="42",
                module="quiz",
                cmid=777,
                known_attempt_ids=["9001", "9002"],
            )


@pytest.mark.asyncio
async def test_report_cannot_claim_same_attempt_is_both_present_and_deleted() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "course_id": "549",
                "activity": {"module": "quiz", "cmid": 777},
                "items": [
                    {
                        "external_id": "quiz:777:9001",
                        "external_revision": "a" * 64,
                        "user_id": "43",
                        "module": "quiz",
                        "cmid": 777,
                        "attempt_id": "9001",
                        "responses": [],
                    }
                ],
                "next_cursor": None,
                "complete": True,
                "warnings": [],
                "storage_state": storage_state(),
                "deleted_attempt_ids": ["9001"],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError):
            await browser.discover_historical_submissions(
                course_id="549",
                actor_external_subject="42",
                module="quiz",
                cmid=777,
                known_attempt_ids=["9001"],
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,ids",
    [
        ("quiz", ["1", "2", "3", "4", "5", "6"]),
        ("quiz", ["1", "1"]),
        ("quiz", ["1&cmid=2"]),
        ("assign", ["1"]),
    ],
)
async def test_invalid_probe_never_sends_request(module: str, ids: list[str]) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("Invalid scope must be rejected before HTTP")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = MoodleBrowserClient(
            settings(),
            client,
            service_url="http://moodle-browser:8082",
            shared_secret="b" * 32,
            storage_state=storage_state(),
        )
        with pytest.raises(IntegrationProtocolError):
            await browser.discover_historical_submissions(
                course_id="549",
                actor_external_subject="42",
                module=module,
                cmid=777,
                known_attempt_ids=ids,
            )
