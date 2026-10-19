"""Read guards for deliberately evicted local payloads, never for LMS access."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attempts import Attempt, Submission
from app.schemas.attempts import AttemptHistoryEventRead
from app.services.common import DomainError

ARCHIVED_PAYLOAD_MESSAGE = (
    "Локальная копия ответа очищена. Восстановите её синхронизацией работы из Moodle."
)


def require_submission_payload(submission: Submission) -> None:
    if submission.payload_evicted_at is not None:
        raise DomainError(410, "SUBMISSION_PAYLOAD_EVICTED", ARCHIVED_PAYLOAD_MESSAGE)


async def lock_submission_payload(db: AsyncSession, submission: Submission) -> Submission:
    """Fence short context collection against retention; authorize before calling.

    The caller must commit/rollback before waiting for an external model/clangd.
    Attempt-before-Submission matches the review and Moodle-sync lock ordering.
    """
    await db.scalar(
        select(Attempt)
        .where(Attempt.id == submission.attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    refreshed = await db.scalar(
        select(Submission)
        .where(Submission.id == submission.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if refreshed is None:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
    return refreshed


def pruned_history_notice(attempt: Attempt, revision: int) -> AttemptHistoryEventRead | None:
    pruned_at = (attempt.integrity_policy or {}).get("history_pruned_at")
    if not pruned_at:
        return None
    try:
        when = datetime.fromisoformat(str(pruned_at).replace("Z", "+00:00"))
    except ValueError:
        when = attempt.updated_at
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return AttemptHistoryEventRead(
        id=uuid.uuid5(attempt.id, "history-pruned"), type="snapshot",
        label="История написания очищена",
        detail=(
            "Удалена администратором или по лимиту хранения; "
            "восстановление из Moodle невозможно"
        ),
        at=when, revision=revision,
    )
