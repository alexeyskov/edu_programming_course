from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.integrations.moodle_transport import (
    confirmed_moodle_activity_answer_transport,
    normalize_moodle_answer_transport,
)

if TYPE_CHECKING:
    from app.integrations.delivery_profile import WorkspaceDeliveryProfile


def project_moodle_workspace_profile(
    external_type: str,
    metadata: dict[str, Any],
) -> WorkspaceDeliveryProfile | None:
    """Project Moodle answer plugins into LMS-neutral editor constraints."""

    # Import lazily to keep the generic profile module free of an import cycle.
    from app.integrations.delivery_profile import WorkspaceDeliveryProfile

    normalized_external_type = external_type.lower().replace("-", "_")
    raw_module = str(metadata.get("module", normalized_external_type)).lower().replace("-", "_")
    module = {
        "quiz": "quiz",
        "mod_quiz": "quiz",
        "moodle_quiz": "quiz",
        "moodle_mod_quiz": "quiz",
        "assign": "assign",
        "mod_assign": "assign",
        "moodle_assignment": "assign",
        "moodle_mod_assign": "assign",
    }.get(raw_module)
    transport = normalize_moodle_answer_transport(metadata.get("submission_mode"))
    if (module, transport) in {
        ("quiz", "ESSAY_ONLINE_TEXT"),
        ("assign", "ASSIGN_ONLINE_TEXT"),
    }:
        return WorkspaceDeliveryProfile(multi_file=False)
    if (module, transport) in {
        ("quiz", "ESSAY_ATTACHMENT"),
        ("assign", "ASSIGN_FILE"),
    }:
        return WorkspaceDeliveryProfile(multi_file=True)
    return None


def project_moodle_activity_delivery_transport(
    module: str,
    activity: dict[str, Any] | None,
) -> str | None:
    return confirmed_moodle_activity_answer_transport(activity, module=module)


__all__ = [
    "project_moodle_activity_delivery_transport",
    "project_moodle_workspace_profile",
]
