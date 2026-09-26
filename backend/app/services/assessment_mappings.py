"""Separate activity bindings from per-attempt bookkeeping in ExternalMapping."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.integration import ExternalMapping


async def assessment_activity_mappings(
    db: AsyncSession,
    *,
    connection_id: uuid.UUID,
    assessment_id: uuid.UUID,
) -> list[ExternalMapping]:
    # Older rows stored attempt observations/deletion markers with local_type
    # Assessment. They are not competing activity bindings. Exclude only these
    # known internal types: genuinely ambiguous/unsupported mappings must still
    # fail closed in the caller, and deletion evidence must stay intact.
    return list(
        (
            await db.scalars(
                select(ExternalMapping).where(
                    ExternalMapping.connection_id == connection_id,
                    ExternalMapping.local_id == assessment_id,
                    ExternalMapping.local_type.in_(["Assessment", "core.assessment"]),
                    ExternalMapping.external_type.not_in(
                        [
                            "moodle_deleted_quiz_attempt",
                            "moodle_attempt_observation",
                        ]
                    ),
                )
            )
        ).all()
    )
