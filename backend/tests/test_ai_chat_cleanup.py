from __future__ import annotations

from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select, update

from app.db.base import utcnow
from app.models.analysis import AICodeReview
from app.models.attempts import Submission
from app.models.integration import AuditEntry
from app.models.review import ChatMessage, ChatThread, ReviewDecision
from app.services.ai_chat_cleanup import cleanup_grace_seconds
from tests.test_api_integrity_ai_system import _context, _csrf, api_bundle  # noqa: F401

URL = "/api/v1/system/ai-chat-cleanup"


@pytest_asyncio.fixture
async def cleanup_bundle(api_bundle):  # noqa: F811
    app, sessions, settings, seed, holder = api_bundle
    holder["auth"] = _context(seed.admin_id, capabilities=("SYSTEM_SETTINGS",))
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        headers = await _csrf(client)
        yield client, headers, sessions, settings, seed, holder


async def _thread(
    sessions,
    seed,
    *,
    mode="TEACHER",
    status="CLOSED",
    minutes=60,
    message_minutes=None,
    messages=2,
    other_course=False,
):
    when = utcnow() - timedelta(minutes=minutes)
    message_when = utcnow() - timedelta(
        minutes=minutes if message_minutes is None else message_minutes,
    )
    async with sessions() as db, db.begin():
        thread = ChatThread(
            owner_id=seed.teacher_id if mode == "TEACHER" else seed.student_id,
            course_id=seed.other_course_id if other_course else seed.course_id,
            mode=mode,
            status=status,
            created_at=when,
            updated_at=when,
        )
        db.add(thread)
        await db.flush()
        db.add_all(
            [
                ChatMessage(
                    thread_id=thread.id,
                    role="USER" if index % 2 == 0 else "ASSISTANT",
                    content="Private transcript must not appear in preview or audit",
                    created_at=message_when,
                    updated_at=message_when,
                )
                for index in range(messages)
            ]
        )
        return thread.id


async def test_cleanup_counts_and_removes_only_old_closed_chats(cleanup_bundle):
    client, headers, sessions, _settings, seed, _holder = cleanup_bundle
    teacher = await _thread(sessions, seed)
    student = await _thread(sessions, seed, mode="STUDENT", other_course=True)
    empty = await _thread(sessions, seed, messages=0)
    preserved = {
        await _thread(sessions, seed, status="OPEN"),
        await _thread(sessions, seed, mode="STUDENT", status="OPEN"),
        await _thread(sessions, seed, minutes=1),
        await _thread(sessions, seed, message_minutes=1),
        await _thread(sessions, seed, status="UNKNOWN"),
    }
    async with sessions() as db, db.begin():
        submission = await db.get(Submission, seed.submission_id)
        report = AICodeReview(
            submission_id=submission.id,
            snapshot_id=submission.snapshot_id,
            requested_by_id=seed.teacher_id,
            manifest_hash="a" * 64,
            state="COMPLETED",
            summary="Keep analysis",
            recommended_grade=7,
        )
        db.add(report)
        await db.flush()
        report_id = report.id
        decisions_before = list((await db.scalars(select(ReviewDecision.id))).all())

    response = await client.get(URL)
    assert response.status_code == 200
    preview = response.json()
    assert preview == {
        "threads": 3,
        "teacher_threads": 2,
        "student_threads": 1,
        "messages": 4,
        "protected_threads": 2,
        "grace_seconds": 300,
        "closed_before": preview["closed_before"],
    }
    assert "Private transcript" not in response.text
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatThread.id))) == 8  # GET is read-only

    response = await client.post(
        URL,
        headers=headers,
        json={
            "confirm": True,
            "closed_before": preview["closed_before"],
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "threads": 3,
        "teacher_threads": 2,
        "student_threads": 1,
        "messages": 4,
    }
    async with sessions() as db:
        assert set((await db.scalars(select(ChatThread.id))).all()) == preserved
        assert await db.scalar(select(func.count(ChatMessage.id))) == 10
        assert (
            await db.scalar(
                select(func.count(ChatMessage.id)).where(
                    ChatMessage.thread_id.in_([teacher, student, empty]),
                )
            )
            == 0
        )
        assert (await db.get(AICodeReview, report_id)).summary == "Keep analysis"
        assert list((await db.scalars(select(ReviewDecision.id))).all()) == decisions_before
        assert await db.get(Submission, seed.submission_id) is not None
        audit = await db.scalar(
            select(AuditEntry).where(AuditEntry.action == "ai_chats.cleaned_up"),
        )
        assert audit.actor_id == seed.admin_id
        assert audit.metadata_json == {
            **response.json(),
            "closed_before": preview["closed_before"].replace("Z", "+00:00"),
        }

    repeated = await client.post(
        URL,
        headers=headers,
        json={
            "confirm": True,
            "closed_before": preview["closed_before"],
        },
    )
    assert repeated.status_code == 200
    assert repeated.json() == {
        "threads": 0,
        "teacher_threads": 0,
        "student_threads": 0,
        "messages": 0,
    }


@pytest.mark.parametrize("role", ["STUDENT", "TEACHER"])
async def test_cleanup_requires_current_administrative_capability(cleanup_bundle, role):
    client, headers, sessions, _settings, seed, holder = cleanup_bundle
    await _thread(sessions, seed)
    holder["auth"] = _context(seed.teacher_id, roles=(role,))
    assert (await client.get(URL)).status_code == 403
    response = await client.post(
        URL,
        headers=headers,
        json={
            "confirm": True,
            "closed_before": (utcnow() - timedelta(days=1)).isoformat(),
        },
    )
    assert response.status_code == 403
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatThread.id))) == 1


@pytest.mark.parametrize("confirmation", [False, None, "true", 1])
async def test_cleanup_rejects_missing_or_non_boolean_confirmation(cleanup_bundle, confirmation):
    client, headers, sessions, _settings, seed, _holder = cleanup_bundle
    await _thread(sessions, seed)
    preview = (await client.get(URL)).json()
    body = {"closed_before": preview["closed_before"]}
    if confirmation is not None:
        body["confirm"] = confirmation
    assert (await client.post(URL, headers=headers, json=body)).status_code == 422
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatMessage.id))) == 2


async def test_cleanup_requires_csrf_and_safe_aware_cutoff(cleanup_bundle):
    client, headers, sessions, _settings, seed, _holder = cleanup_bundle
    await _thread(sessions, seed)
    preview = (await client.get(URL)).json()
    assert (
        await client.post(
            URL,
            json={
                "confirm": True,
                "closed_before": preview["closed_before"],
            },
        )
    ).status_code == 403
    for cutoff in [utcnow().isoformat(), "2099-01-01T00:00:00Z", "2020-01-01T00:00:00"]:
        response = await client.post(
            URL,
            headers=headers,
            json={
                "confirm": True,
                "closed_before": cutoff,
            },
        )
        assert response.status_code == 422
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatThread.id))) == 1
        assert await db.scalar(select(func.count(AuditEntry.id))) == 0


async def test_preview_cutoff_does_not_expand_to_newly_closed_chats(cleanup_bundle, monkeypatch):
    client, headers, sessions, _settings, seed, _holder = cleanup_bundle
    await _thread(sessions, seed)
    new = await _thread(sessions, seed, status="OPEN")
    preview = (await client.get(URL)).json()
    async with sessions() as db, db.begin():
        await db.execute(update(ChatThread).where(ChatThread.id == new).values(status="CLOSED"))
    # Even if confirmation is left open for a day, it retains its original cutoff.
    later = utcnow() + timedelta(days=1)
    monkeypatch.setattr("app.services.ai_chat_cleanup.utcnow", lambda: later)
    response = await client.post(
        URL,
        headers=headers,
        json={
            "confirm": True,
            "closed_before": preview["closed_before"],
        },
    )
    assert response.status_code == 200
    assert response.json()["threads"] == 1
    async with sessions() as db:
        assert await db.get(ChatThread, new) is not None


async def test_cleanup_protects_configured_rate_limit_window(cleanup_bundle):
    client, headers, sessions, settings, seed, _holder = cleanup_bundle
    settings.ai_rate_limit_window_seconds = 7200
    await _thread(sessions, seed, minutes=60)
    preview = (await client.get(URL)).json()
    assert preview["grace_seconds"] == 7200
    assert preview["threads"] == 0
    assert preview["protected_threads"] == 1
    response = await client.post(
        URL,
        headers=headers,
        json={
            "confirm": True,
            "closed_before": preview["closed_before"],
        },
    )
    assert response.json()["threads"] == 0
    settings.ai_rate_limit_window_seconds = 60
    settings.ai_timeout_seconds = 600
    assert cleanup_grace_seconds(settings) == 605


async def test_cleanup_uses_multiple_batches_and_rolls_back_with_transaction(cleanup_bundle):
    client, headers, sessions, settings, seed, _holder = cleanup_bundle
    from app.services.ai_chat_cleanup import purge_closed_chats

    when = utcnow() - timedelta(days=1)
    async with sessions() as db, db.begin():
        db.add_all(
            [
                ChatThread(
                    owner_id=seed.teacher_id,
                    course_id=seed.course_id,
                    mode="TEACHER",
                    status="CLOSED",
                    updated_at=when,
                )
                for _ in range(205)
            ]
        )
    async with sessions() as db:
        result = await purge_closed_chats(db, settings, closed_before=when)
        assert result.threads == 205
        await db.rollback()
    async with sessions() as db:
        assert await db.scalar(select(func.count(ChatThread.id))) == 205
    preview = (await client.get(URL)).json()
    response = await client.post(
        URL,
        headers=headers,
        json={
            "confirm": True,
            "closed_before": preview["closed_before"],
        },
    )
    assert response.status_code == 200
    assert response.json()["threads"] == 205
