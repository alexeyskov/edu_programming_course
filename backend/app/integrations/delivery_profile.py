from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.integrations.moodle_delivery_profile import (
    project_moodle_activity_delivery_transport,
    project_moodle_workspace_profile,
)


@dataclass(frozen=True, slots=True)
class WorkspaceDeliveryProfile:
    """LMS-neutral editor constraints projected by a delivery adapter."""

    multi_file: bool


DeliveryProfileProjector = Callable[
    [str, dict[str, Any]],
    WorkspaceDeliveryProfile | None,
]
ActivityDeliveryProjector = Callable[[str, dict[str, Any] | None], str | None]


# Provider selection lives at the adapter boundary.  Adding another LMS means
# registering its projector here; workspace/attempt services remain unchanged.
_PROJECTORS: dict[str, DeliveryProfileProjector] = {
    "MOODLE": project_moodle_workspace_profile,
}
_ACTIVITY_PROJECTORS: dict[str, ActivityDeliveryProjector] = {
    "MOODLE": project_moodle_activity_delivery_transport,
}


def project_workspace_delivery_profile(
    *,
    provider: str,
    external_type: str,
    metadata: dict[str, Any],
) -> WorkspaceDeliveryProfile | None:
    projector = _PROJECTORS.get(provider.upper())
    if projector is None:
        return None
    return projector(external_type, metadata)


def project_activity_delivery_transport(
    *,
    provider: str,
    module: str,
    activity: dict[str, Any] | None,
) -> str | None:
    """Return an adapter-owned opaque transport identifier when proved."""

    projector = _ACTIVITY_PROJECTORS.get(provider.upper())
    if projector is None:
        return None
    return projector(module, activity)


__all__ = [
    "WorkspaceDeliveryProfile",
    "project_activity_delivery_transport",
    "project_workspace_delivery_profile",
]
