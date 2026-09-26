from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.delivery_profile import (
    WorkspaceDeliveryProfile,
    project_workspace_delivery_profile,
)
from app.models.courses import Course
from app.models.identity import LMSConnection
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment
from app.services.assessment_mappings import assessment_activity_mappings


@dataclass(frozen=True, slots=True)
class AssessmentWorkspaceDeliveryResolution:
    external: bool
    profile: WorkspaceDeliveryProfile | None
    mapping: ExternalMapping | None = None


async def resolve_assessment_workspace_delivery_profile(
    db: AsyncSession,
    assessment_id: uuid.UUID,
) -> AssessmentWorkspaceDeliveryResolution:
    """Resolve one unambiguous connector-projected workspace profile.

    Database lookup and provider dispatch are kept out of the workspace
    service.  No external mapping permits the local assessment fallback.
    Unsupported or conflicting external mappings are explicitly unresolved so
    attempt creation can stop instead of guessing an editor shape.  Every
    connector still validates the live external form again at delivery.
    """

    context = (
        await db.execute(
            select(Course, LMSConnection)
            .join(Assessment, Assessment.course_id == Course.id)
            .join(LMSConnection, LMSConnection.id == Course.connection_id)
            .where(Assessment.id == assessment_id)
        )
    ).one_or_none()
    if context is None:
        return AssessmentWorkspaceDeliveryResolution(external=False, profile=None)
    course, connection = context
    mappings = await assessment_activity_mappings(
        db, connection_id=course.connection_id, assessment_id=assessment_id,
    )
    if not mappings:
        # A local assessment in an LMS-backed course remains provider-neutral:
        # publication and attempts do not require an external mapping.
        return AssessmentWorkspaceDeliveryResolution(external=False, profile=None)
    if not connection.enabled or len(mappings) != 1:
        return AssessmentWorkspaceDeliveryResolution(external=True, profile=None)
    mapping = mappings[0]
    metadata = mapping.metadata_json if isinstance(mapping.metadata_json, dict) else {}
    # Only the synchronizer may confirm a delivery contract.  Missing legacy
    # markers and stale/error states must be refreshed instead of guessed.
    if metadata.get("sync_state") != "CURRENT":
        return AssessmentWorkspaceDeliveryResolution(external=True, profile=None)
    profile = project_workspace_delivery_profile(
        provider=connection.provider,
        external_type=mapping.external_type,
        metadata=metadata,
    )
    if profile is None:
        return AssessmentWorkspaceDeliveryResolution(external=True, profile=None)
    return AssessmentWorkspaceDeliveryResolution(
        external=True,
        profile=profile,
        mapping=mapping,
    )


__all__ = [
    "AssessmentWorkspaceDeliveryResolution",
    "resolve_assessment_workspace_delivery_profile",
]
