from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AnyHttpUrl, Field, SecretStr, StringConstraints

from app.models.enums import CourseRole, LMSProvider
from app.schemas.common import JsonObject, MutationModel, ReadModel

AdminToken = Annotated[SecretStr, Field(min_length=1, max_length=1_024)]
TeacherToken = Annotated[SecretStr, Field(min_length=1, max_length=512)]


class CSRFTokenRead(ReadModel):
    csrf_token: Annotated[str, StringConstraints(min_length=32, max_length=512)]


class AuthConnectionRead(ReadModel):
    id: UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    provider: LMSProvider
    enabled: bool
    login_mode: Literal["CREDENTIALS", "REDIRECT"]


class MoodleCredentialLoginRequest(MutationModel):
    username: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
    password: Annotated[SecretStr, Field(min_length=1, max_length=4_096)]
    admin_token: AdminToken | None = None
    teacher_token: TeacherToken | None = None


class AuthConnectionAdminRead(AuthConnectionRead):
    base_url: AnyHttpUrl
    capabilities: JsonObject = Field(default_factory=dict)
    last_health_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class LMSLoginStartRequest(MutationModel):
    admin_token: AdminToken | None = None
    teacher_token: TeacherToken | None = None
    course_id: Annotated[str, StringConstraints(min_length=1, max_length=255)] | None = None


class LMSLoginStartRead(ReadModel):
    redirect_url: AnyHttpUrl


class DevLoginRequest(MutationModel):
    role: CourseRole
    admin_token: AdminToken | None = None
    teacher_token: TeacherToken | None = None


class AdminElevationRequest(MutationModel):
    admin_token: AdminToken


class PrincipalRead(ReadModel):
    id: UUID
    display_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]


class SessionProviderRead(ReadModel):
    id: UUID
    name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    provider: LMSProvider


class CourseMembershipRead(ReadModel):
    course_id: UUID
    course_name: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    role: CourseRole
    group_name: Annotated[str, StringConstraints(max_length=255)] | None = None


class SessionRead(ReadModel):
    principal: PrincipalRead
    provider: SessionProviderRead
    roles: list[CourseRole] = Field(default_factory=list, max_length=2)
    memberships: list[CourseMembershipRead] = Field(default_factory=list, max_length=1_000)
    capabilities: list[Annotated[str, StringConstraints(min_length=1, max_length=100)]] = Field(
        default_factory=list,
        max_length=200,
    )
    admin_elevation_expires_at: datetime | None = None


__all__ = [
    "AdminElevationRequest",
    "AuthConnectionAdminRead",
    "AuthConnectionRead",
    "CSRFTokenRead",
    "CourseMembershipRead",
    "DevLoginRequest",
    "LMSLoginStartRead",
    "LMSLoginStartRequest",
    "MoodleCredentialLoginRequest",
    "PrincipalRead",
    "SessionProviderRead",
    "SessionRead",
]
