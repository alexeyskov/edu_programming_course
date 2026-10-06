from __future__ import annotations

import math
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.models.enums import ChatMode
from app.models.review import ChatMessage, ChatThread
from app.schemas.system import AIChatCleanupCountsRead, AIChatCleanupPreviewRead
from app.services.common import DomainError

_BATCH_SIZE = 200


def cleanup_grace_seconds(settings: Settings) -> int:
    # Keep both in-flight replies and the message budget window intact.
    return max(
        300,
        math.ceil(settings.ai_timeout_seconds) + 5,
        settings.ai_rate_limit_window_seconds,
    )


def _closed_threads():
    return (
        ChatThread.status == "CLOSED",
        ChatThread.mode.in_((ChatMode.TEACHER.value, ChatMode.STUDENT.value)),
    )


def _eligible_threads(closed_before: datetime):
    # A reply can be persisted after the thread was closed without updating the
    # thread timestamp. Protect recently written messages as well as the thread.
    recent_message = (
        select(ChatMessage.id)
        .where(ChatMessage.thread_id == ChatThread.id, ChatMessage.created_at > closed_before)
        .correlate(ChatThread)
        .exists()
    )
    return (*_closed_threads(), ChatThread.updated_at <= closed_before, ~recent_message)


async def preview_chat_cleanup(db: AsyncSession, settings: Settings) -> AIChatCleanupPreviewRead:
    grace_seconds = cleanup_grace_seconds(settings)
    closed_before = utcnow() - timedelta(seconds=grace_seconds)
    eligible = _eligible_threads(closed_before)
    # Aggregate in the database: neither transcript text nor owner identities
    # should be loaded or disclosed by this administrative action.
    counts = dict(
        (
            await db.execute(
                select(ChatThread.mode, func.count()).where(*eligible).group_by(ChatThread.mode)
            )
        ).all()
    )
    teacher_threads = counts.get(ChatMode.TEACHER.value, 0)
    student_threads = counts.get(ChatMode.STUDENT.value, 0)
    threads = teacher_threads + student_threads
    messages = await db.scalar(
        select(func.count(ChatMessage.id))
        .join(ChatThread, ChatThread.id == ChatMessage.thread_id)
        .where(*eligible)
    )
    closed_threads = await db.scalar(select(func.count(ChatThread.id)).where(*_closed_threads()))
    return AIChatCleanupPreviewRead(
        threads=threads,
        teacher_threads=teacher_threads,
        student_threads=student_threads,
        messages=messages or 0,
        closed_before=closed_before,
        grace_seconds=grace_seconds,
        protected_threads=max(0, (closed_threads or 0) - threads),
    )


async def purge_closed_chats(
    db: AsyncSession,
    settings: Settings,
    *,
    closed_before: datetime,
) -> AIChatCleanupCountsRead:
    """Delete only confirmed, old CLOSED chats; caller owns transaction and audit."""
    safe_cutoff = utcnow() - timedelta(seconds=cleanup_grace_seconds(settings))
    if closed_before > safe_cutoff:
        raise DomainError(
            422,
            "AI_CHAT_CLEANUP_CUTOFF_INVALID",
            "Список для очистки устарел. Обновите его и подтвердите очистку заново.",
        )

    teacher_threads = student_threads = messages = 0
    while True:
        # The chat writer locks the same thread. Skip busy rows and use bounded
        # batches without ever materialising conversation contents in memory.
        rows = (
            await db.execute(
                select(ChatThread.id, ChatThread.mode)
                .where(*_eligible_threads(closed_before))
                .order_by(ChatThread.id)
                .limit(_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not rows:
            break
        ids = [row.id for row in rows]
        removed = await db.execute(delete(ChatMessage).where(ChatMessage.thread_id.in_(ids)))
        messages += removed.rowcount
        await db.execute(delete(ChatThread).where(ChatThread.id.in_(ids)))
        teacher_threads += sum(row.mode == ChatMode.TEACHER.value for row in rows)
        student_threads += sum(row.mode == ChatMode.STUDENT.value for row in rows)

    return AIChatCleanupCountsRead(
        threads=teacher_threads + student_threads,
        teacher_threads=teacher_threads,
        student_threads=student_threads,
        messages=messages,
    )
