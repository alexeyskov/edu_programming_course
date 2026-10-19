from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from app.db.base import utcnow
from app.models.analysis import AICodeReview, AuthorshipAnalysisJob
from app.models.attempts import Attempt, Submission
from app.models.integration import AuditEntry
from app.models.review import ChatMessage, ChatThread, ReviewClaim, TeacherExperiment
from tests.test_ai_chat_cleanup import _thread
from tests.test_api_integrity_ai_system import _context, _csrf, api_bundle  # noqa: F401

URL = "/api/v1/system/storage"


@pytest_asyncio.fixture
async def storage_api(api_bundle):  # noqa: F811
    app, sessions, settings, seed, holder = api_bundle
    holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        yield client, headers, sessions, settings, seed, holder


async def test_storage_limit_defaults_persists_and_resets(storage_api):
    client, headers, _sessions, _settings, _seed, _holder = storage_api
    initial = (await client.get("/api/v1/system/settings")).json()
    assert initial["storage_limit_gb"] == 128
    changed = await client.patch(
        "/api/v1/system/settings", headers=headers,
        json={"revision": initial["revision"], "storage_limit_gb": 64},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["storage_limit_gb"] == 64
    assert (await client.get(URL)).json()["storage_limit_gb"] == 64
    reset = await client.patch(
        "/api/v1/system/settings", headers=headers,
        json={"revision": changed.json()["revision"], "storage_limit_gb": None},
    )
    assert reset.status_code == 200, reset.text
    assert reset.json()["storage_limit_gb"] == 128


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "128", 65_537])
async def test_storage_limit_rejects_invalid_value(storage_api, value):
    client, headers, _sessions, _settings, _seed, _holder = storage_api
    initial = (await client.get("/api/v1/system/settings")).json()
    response = await client.patch(
        "/api/v1/system/settings", headers=headers,
        json={"revision": initial["revision"], "storage_limit_gb": value},
    )
    assert response.status_code == 422
    assert (await client.get(URL)).json()["storage_limit_gb"] == 128


@pytest.mark.parametrize("role", ["STUDENT", "TEACHER"])
async def test_storage_endpoints_are_admin_only(storage_api, role):
    client, headers, _sessions, _settings, seed, holder = storage_api
    holder["auth"] = _context(seed.teacher_id, roles=(role,))
    assert (await client.get(URL)).status_code == 403
    response = await client.post(
        URL + "/cleanup", headers=headers,
        json={
            "category": "quota", "confirm": True,
            "eligible_before": (utcnow() - timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 403


async def test_storage_preview_is_read_only_and_does_not_expose_content(storage_api):
    client, _headers, sessions, _settings, seed, _holder = storage_api
    await _thread(sessions, seed)
    response = await client.get(URL)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview["used_bytes"] > 0
    assert preview["database_bytes"] is None
    assert [item["category"] for item in preview["categories"]] == [
        "edit_history", "teacher_ai_chats", "synced_submissions",
    ]
    assert preview["categories"][1]["eligible_records"] == 1
    assert "Private transcript" not in response.text
    assert "int main" not in response.text
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatThread.id))) == 1
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


@pytest.mark.parametrize("confirmation", [None, False, "true", 1])
async def test_storage_cleanup_requires_explicit_boolean_confirmation(storage_api, confirmation):
    client, headers, sessions, _settings, seed, _holder = storage_api
    await _thread(sessions, seed)
    preview = (await client.get(URL)).json()
    body = {"category": "teacher_ai_chats", "eligible_before": preview["eligible_before"]}
    if confirmation is not None:
        body["confirm"] = confirmation
    assert (await client.post(URL + "/cleanup", headers=headers, json=body)).status_code == 422
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatMessage.id))) == 2
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


async def test_storage_cleanup_requires_csrf_and_safe_preview(storage_api):
    client, headers, sessions, _settings, seed, _holder = storage_api
    await _thread(sessions, seed)
    preview = (await client.get(URL)).json()
    body = {
        "category": "teacher_ai_chats", "eligible_before": preview["eligible_before"],
        "confirm": True,
    }
    assert (await client.post(URL + "/cleanup", json=body)).status_code == 403
    for extra in (
        {"category": "all"},
        {"eligible_before": (utcnow() + timedelta(days=1)).isoformat()},
        {"eligible_before": "2026-01-01T00:00:00"},
    ):
        response = await client.post(URL + "/cleanup", headers=headers, json={**body, **extra})
        assert response.status_code == 422, response.text
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatMessage.id))) == 2
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


async def test_manual_teacher_cleanup_preserves_student_open_and_recent_chats(storage_api):
    client, headers, sessions, _settings, seed, _holder = storage_api
    removed_id = await _thread(sessions, seed)
    preserved = {
        await _thread(sessions, seed, mode="STUDENT"),
        await _thread(sessions, seed, status="OPEN"),
        await _thread(sessions, seed, minutes=1),
    }
    preview = (await client.get(URL)).json()
    response = await client.post(
        URL + "/cleanup", headers=headers,
        json={
            "category": "teacher_ai_chats", "eligible_before": preview["eligible_before"],
            "confirm": True,
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["removed_records"] == 1
    assert result["reclaimed_bytes"] > 0
    async with sessions() as db:
        assert await db.get(ChatThread, removed_id) is None
        assert set((await db.scalars(select(ChatThread.id))).all()) == preserved
        audit = await db.scalar(select(AuditEntry).where(AuditEntry.action == "storage.cleaned_up"))
        assert audit.actor_id == seed.admin_id
        assert audit.metadata_json["category"] == "teacher_ai_chats"
        assert audit.metadata_json["removed_records"] == 1
        assert "Private transcript" not in str(audit.metadata_json)
    assert (await client.get(URL)).json()["last_cleanup_at"] is not None


async def test_quota_cleanup_below_limit_keeps_data(storage_api):
    client, headers, sessions, _settings, seed, _holder = storage_api
    thread_id = await _thread(sessions, seed)
    preview = (await client.get(URL)).json()
    response = await client.post(
        URL + "/cleanup", headers=headers,
        json={"category": "quota", "eligible_before": preview["eligible_before"], "confirm": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["removed_records"] == 0
    async with sessions() as db:
        assert await db.get(ChatThread, thread_id) is not None


async def test_archived_submission_keeps_metadata_but_cannot_be_used_as_empty_source(storage_api):
    client, headers, sessions, _settings, seed, holder = storage_api
    async with sessions() as db, db.begin():
        submission = await db.get(Submission, seed.submission_id)
        # Keep stale snapshot data deliberately: serialization must use the
        # archive marker, not expose or run an evicted/incomplete source.
        submission.payload_evicted_at = utcnow()
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
    response = await client.get(f"/api/v1/submissions/{seed.submission_id}")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == str(seed.submission_id)
    assert data["payload_evicted_at"] is not None
    assert data["files"] == []
    for path in (
        f"/api/v1/submissions/{seed.submission_id}/claims",
        f"/api/v1/submissions/{seed.submission_id}/teacher-experiments",
        f"/api/v1/submissions/{seed.submission_id}/authorship-analyses",
    ):
        blocked = await client.post(path, headers=headers, json={})
        assert blocked.status_code == 410, (path, blocked.text)
        assert blocked.json()["code"] == "SUBMISSION_PAYLOAD_EVICTED"
    chat = await client.post(
        "/api/v1/ai/teacher-threads", headers=headers,
        json={"submission": str(seed.submission_id), "course": str(seed.course_id)},
    )
    assert chat.status_code == 410, chat.text
    review_path = f"/api/v1/ai/submissions/{seed.submission_id}/code-review"
    for response in (
        await client.get(review_path),
        await client.post(review_path, headers=headers, json={}),
    ):
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "UNAVAILABLE"
        assert response.json()["enabled"] is False
    async with sessions() as db:
        for model in (
            ChatThread, AICodeReview, AuthorshipAnalysisJob, ReviewClaim, TeacherExperiment,
        ):
            assert await db.scalar(select(func.count(model.id))) == 0
    holder["auth"] = _context(seed.other_teacher_id, roles=("TEACHER",))
    hidden = await client.get(f"/api/v1/submissions/{seed.submission_id}")
    assert hidden.status_code == 403
    assert "очищена" not in hidden.text


async def test_pruned_history_is_visible_and_cannot_produce_new_authorship_analysis(storage_api):
    client, headers, sessions, _settings, seed, holder = storage_api
    async with sessions() as db, db.begin():
        attempt = await db.get(Attempt, seed.attempt_id)
        attempt.integrity_policy = {
            **(attempt.integrity_policy or {}), "history_available": False,
            "history_pruned_at": utcnow().isoformat(),
        }
    holder["auth"] = _context(seed.teacher_id, roles=("TEACHER",))
    response = await client.get(f"/api/v1/submissions/{seed.submission_id}")
    assert response.status_code == 200, response.text
    assert any(item["label"] == "История написания очищена" for item in response.json()["history"])
    blocked = await client.post(
        f"/api/v1/submissions/{seed.submission_id}/authorship-analyses", headers=headers, json={},
    )
    assert blocked.status_code == 410, blocked.text
    assert blocked.json()["code"] == "EDIT_HISTORY_PRUNED"
    holder["auth"] = _context(seed.student_id, roles=("STUDENT",))
    student_history = await client.get(f"/api/v1/attempts/{seed.attempt_id}/history")
    assert student_history.status_code == 200, student_history.text
    assert any(item["label"] == "История написания очищена" for item in student_history.json())
