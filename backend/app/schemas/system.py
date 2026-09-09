from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AliasChoices,
    AnyHttpUrl,
    Field,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.models.enums import SyncOutboxState
from app.schemas.common import JsonObject, MutationModel, ReadModel

ServiceStatus = Literal["ok", "degraded", "down"]


class ServiceHealthRead(ReadModel):
    name: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    status: ServiceStatus
    detail: Annotated[str, StringConstraints(max_length=2_000)] = ""


class SystemHealthRead(ReadModel):
    status: ServiceStatus
    database: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    build: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    services: list[ServiceHealthRead] = Field(default_factory=list, max_length=100)


class SystemSettingsRead(ReadModel):
    revision: int = Field(ge=1)
    ai_enabled: bool
    student_ai_enabled: bool
    runner_enabled: bool
    runner_cpu_seconds: int = Field(ge=1, le=300)
    runner_memory_mb: int = Field(ge=64, le=65_536)
    retention_days: int = Field(ge=1, le=3_650)
    allowed_lms_origins: list[AnyHttpUrl] = Field(default_factory=list, max_length=100)
    incident_banner: Annotated[str, StringConstraints(max_length=10_000)] = ""
    services: list[ServiceHealthRead] = Field(default_factory=list, max_length=100)


class SystemSettingsUpdateRequest(MutationModel):
    revision: int = Field(ge=1)
    ai_enabled: bool | None = None
    student_ai_enabled: bool | None = None
    runner_enabled: bool | None = None
    runner_cpu_seconds: int | None = Field(default=None, ge=1, le=300)
    runner_memory_mb: int | None = Field(default=None, ge=64, le=65_536)
    retention_days: int | None = Field(default=None, ge=1, le=3_650)
    allowed_lms_origins: list[AnyHttpUrl] | None = Field(default=None, max_length=100)
    incident_banner: Annotated[str, StringConstraints(max_length=10_000)] | None = None

    @model_validator(mode="after")
    def has_setting_change(self) -> SystemSettingsUpdateRequest:
        if self.model_fields_set == {"revision"}:
            raise ValueError("at least one setting must be supplied")
        return self


class SystemSettingRead(ReadModel):
    id: UUID
    key: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    value: JsonObject
    updated_by_id: UUID
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime


class SystemSettingUpdateRequest(MutationModel):
    revision: int = Field(ge=1)
    value: JsonObject


class TeacherAccessTokenCreateRequest(MutationModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]


class TeacherAccessTokenRead(ReadModel):
    id: UUID
    label: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    public_id: Annotated[str, StringConstraints(min_length=8, max_length=24)]
    hash_fingerprint: Annotated[str, StringConstraints(min_length=12, max_length=32)]
    can_reveal: bool
    bound_principal_id: UUID | None = None
    bound_display_name: Annotated[str, StringConstraints(max_length=255)] | None = None
    use_count: int = Field(ge=0)
    last_used_at: datetime | None = None
    created_at: datetime


class TeacherAccessTokenIssuedRead(TeacherAccessTokenRead):
    token: Annotated[str, StringConstraints(min_length=8, max_length=512)]


class TeacherAccessTokenSecretRead(ReadModel):
    token: Annotated[str, StringConstraints(min_length=8, max_length=512)]


class TeacherAccessTokenUpdateRequest(MutationModel):
    token: Annotated[SecretStr, Field(min_length=8, max_length=8)]

    @field_validator("token")
    @classmethod
    def validate_token_alphabet(cls, value: SecretStr) -> SecretStr:
        raw = value.get_secret_value()
        if not raw.isascii() or not raw.isalnum():
            raise ValueError("token must contain exactly eight ASCII letters or digits")
        return value


class SyncOutboxListItemRead(ReadModel):
    id: UUID
    connection_id: UUID
    course_id: UUID | None = None
    attempt_id: UUID | None = None
    event_type: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    aggregate_type: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    aggregate_id: UUID
    state: SyncOutboxState
    attempts: int = Field(ge=0)
    next_attempt_at: datetime
    last_error: Annotated[str, StringConstraints(max_length=20_000)] = ""
    last_attempt_at: datetime | None = None
    delivered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SyncOutboxRead(SyncOutboxListItemRead):
    payload: JsonObject = Field(default_factory=dict)
    idempotency_key: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    receipt: JsonObject = Field(default_factory=dict)
    locked_at: datetime | None = None


class SyncOutboxRetryRequest(MutationModel):
    reason: Annotated[str, StringConstraints(min_length=1, max_length=2_000)]


class AuditEntryRead(ReadModel):
    id: UUID
    actor_id: UUID | None = None
    action: Annotated[str, StringConstraints(min_length=1, max_length=100)]
    object_type: Annotated[str, StringConstraints(max_length=100)] = ""
    object_id: UUID | None = None
    course_id: UUID | None = None
    request_id: Annotated[str, StringConstraints(max_length=100)] = ""
    metadata: JsonObject = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata", "metadata_json"),
    )
    occurred_at: datetime


__all__ = [
    "AuditEntryRead",
    "ServiceHealthRead",
    "ServiceStatus",
    "SyncOutboxListItemRead",
    "SyncOutboxRead",
    "SyncOutboxRetryRequest",
    "SystemHealthRead",
    "SystemSettingRead",
    "SystemSettingUpdateRequest",
    "SystemSettingsRead",
    "SystemSettingsUpdateRequest",
    "TeacherAccessTokenCreateRequest",
    "TeacherAccessTokenIssuedRead",
    "TeacherAccessTokenRead",
    "TeacherAccessTokenSecretRead",
    "TeacherAccessTokenUpdateRequest",
]
