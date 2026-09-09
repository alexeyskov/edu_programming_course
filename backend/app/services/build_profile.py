from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attempts import Attempt, Workspace
from app.services.common import DomainError


def effective_workspace_build_profile(configured_profile: str, *, multi_file: bool) -> str:
    """Project a version's approved compiler profile to the workspace mode."""

    prefix, separator, configured_mode = configured_profile.rpartition("-")
    if separator and configured_mode in {"single", "multi"}:
        return f"{prefix}-{'multi' if multi_file else 'single'}"
    # Custom/extension profiles are opaque and remain adapter-owned.
    return configured_profile


async def effective_attempt_build_profile(
    db: AsyncSession,
    *,
    attempt: Attempt,
    configured_profile: str,
) -> str:
    multi_file = await db.scalar(
        select(Workspace.multi_file).where(Workspace.attempt_id == attempt.id)
    )
    if multi_file is None:
        raise DomainError(500, "WORKSPACE_MISSING", "Attempt workspace is missing")
    return effective_workspace_build_profile(
        configured_profile,
        multi_file=bool(multi_file),
    )


__all__ = ["effective_attempt_build_profile", "effective_workspace_build_profile"]
