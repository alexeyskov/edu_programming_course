from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import (
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    if isinstance(value, str):
        stripped = value.strip()
        json_body = stripped[1:].lstrip() if stripped.startswith("[") else ""
        if json_body.startswith(('"', "]")):
            return json.loads(stripped)
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


# pydantic-settings otherwise attempts json.loads() for list fields before the
# Pydantic before-validator can accept the documented comma-separated syntax.
CSVList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "backend/.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Мехмат.Практикум"
    app_build: str = "development"
    api_prefix: str = "/api/v1"
    debug: bool = Field(
        default=False,
        validation_alias="APP_DEBUG",
    )
    secret_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="APP_SECRET_KEY",
    )

    database_url: str = "sqlite+aiosqlite:///./db.sqlite3"
    db_echo: bool = False
    db_pool_size: int = Field(default=10, ge=1, le=100)
    db_max_overflow: int = Field(default=20, ge=0, le=200)
    db_pool_timeout_seconds: int = Field(default=30, ge=1, le=300)
    db_sslmode: str = "prefer"

    allowed_hosts: CSVList = Field(default_factory=lambda: ["localhost", "127.0.0.1", "backend"])
    cors_allowed_origins: CSVList = Field(default_factory=lambda: ["http://localhost:5173"])
    public_base_url: str = "http://localhost:8000"
    frontend_url: str = "http://localhost:5173"
    secure_ssl_redirect: bool = False
    secure_hsts_seconds: int = Field(default=0, ge=0)
    secure_hsts_include_subdomains: bool = False
    secure_hsts_preload: bool = False

    session_cookie_name: str = "course_session"
    session_cookie_secure: bool | None = None
    session_cookie_samesite: Literal["lax", "strict", "none"] = "lax"
    session_ttl_seconds: int = Field(default=43_200, ge=300)
    session_touch_interval_seconds: int = Field(default=300, ge=30)
    csrf_cookie_name: str = "course_csrf"
    csrf_ttl_seconds: int = Field(default=7_200, ge=300)
    login_verifier_cookie_name: str = "course_login_verifier"
    login_verifier_ttl_seconds: int = Field(default=300, ge=60, le=900)

    admin_token_hash: str = ""
    admin_token: SecretStr = SecretStr("")
    admin_elevation_idle_seconds: int = Field(default=1_800, ge=60)
    admin_elevation_absolute_seconds: int = Field(default=7_200, ge=300)

    dev_auth_enabled: bool = False
    moodle_mock_enabled: bool = False
    runner_mock_enabled: bool = False
    ai_enabled: bool = False
    ai_mock_enabled: bool = False
    runner_url: str = ""
    runner_shared_secret: SecretStr = SecretStr("")
    runner_http_timeout_seconds: float = Field(default=65, gt=0, le=600)
    runner_request_body_max_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    runner_response_body_max_bytes: int = Field(default=8 * 1024 * 1024, ge=1024)
    runner_max_output_bytes: int = Field(default=1_000_000, ge=1024)
    runner_max_concurrent_runs_per_user: int = Field(default=2, ge=1, le=20)
    runner_rate_limit_runs: int = Field(default=30, ge=1, le=1_000)
    runner_rate_limit_window_seconds: int = Field(default=60, ge=1, le=3_600)
    runner_running_stale_seconds: int = Field(default=120, ge=30, le=3_600)
    # Evidence runs remain synchronous for the initial baseline, so their own wall
    # clock budget stays safely below the reverse-proxy request timeout.
    evidence_case_timeout_seconds: float = Field(default=8, gt=0, le=30)
    evidence_total_timeout_seconds: float = Field(default=50, gt=0, le=60)
    evidence_cpu_seconds_per_case: int = Field(default=2, ge=1, le=10)
    evidence_max_concurrent_reports_per_teacher: int = Field(default=1, ge=1, le=5)
    evidence_rate_limit_reports: int = Field(default=5, ge=1, le=100)
    evidence_rate_limit_window_seconds: int = Field(default=300, ge=1, le=3_600)
    evidence_running_stale_seconds: int = Field(default=120, ge=60, le=3_600)
    # Legacy fields retain their defaults. New LLM_* values override them below.
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: SecretStr = SecretStr("")
    ai_model: str = "gpt-5-mini"
    llm_api_address: str | None = Field(default=None, validation_alias="LLM_API_ADDRESS")
    llm_api_key: SecretStr | None = Field(default=None, validation_alias="LLM_API_KEY")
    llm_model: str | None = Field(default=None, validation_alias="LLM_MODEL")
    ai_api_style: Literal["auto", "responses", "chat_completions", "ollama"] = "responses"
    llm_thinking: bool | None = None
    ai_timeout_seconds: float = Field(default=45, gt=0, le=600)
    ai_max_context_bytes: int = Field(default=256 * 1024, ge=1024)
    ai_max_response_bytes: int = Field(default=1024 * 1024, ge=1024)
    ai_max_output_chars: int = Field(default=12_000, ge=256)
    ai_history_messages: int = Field(default=20, ge=0, le=100)
    ai_max_history_chars: int = Field(default=128 * 1024, ge=1024)
    ai_student_rate_limit_messages: int = Field(default=10, ge=1, le=1_000)
    ai_teacher_rate_limit_messages: int = Field(default=30, ge=1, le=1_000)
    ai_rate_limit_window_seconds: int = Field(default=60, ge=1, le=86_400)
    moodle_connection_name: str = "University Moodle"
    moodle_base_url: str = ""
    moodle_service_token: SecretStr = SecretStr("")
    moodle_launch_shared_secret: SecretStr = SecretStr("")
    moodle_credential_encryption_key: SecretStr = SecretStr("")
    moodle_http_timeout_seconds: float = Field(default=15, gt=0, le=300)
    # Student editing ends this many seconds before the live Moodle Quiz timer.
    # This is a submission reserve, not an HTTP request timeout.
    moodle_sync_timeout: int = Field(default=300, ge=0, le=86_400)
    moodle_max_response_bytes: int = Field(default=4 * 1024 * 1024, ge=1024)
    moodle_browser_service_url: str = ""
    moodle_browser_shared_secret: SecretStr = SecretStr("")
    moodle_browser_http_timeout_seconds: float = Field(default=300, gt=0, le=600)
    moodle_browser_request_body_max_bytes: int = Field(
        default=6 * 1024 * 1024,
        ge=16 * 1024,
        le=8 * 1024 * 1024,
    )
    moodle_browser_max_response_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=16 * 1024,
        le=8 * 1024 * 1024,
    )
    moodle_browser_storage_state_max_bytes: int = Field(
        default=256 * 1024,
        ge=16 * 1024,
        le=1024 * 1024,
    )
    moodle_login_rate_limit_attempts: int = Field(default=8, ge=1, le=100)
    moodle_login_network_rate_limit_attempts: int = Field(default=256, ge=16, le=10_000)
    moodle_login_rate_limit_window_seconds: int = Field(default=300, ge=30, le=3_600)
    moodle_max_concurrent_logins_per_worker: int = Field(default=2, ge=1, le=10)
    # Temporary deployment escape hatch. This controls only the browser-to-app
    # credential POST; Moodle itself is still contacted through its fixed HTTPS
    # origin. Keep false once TLS is available on the public endpoint.
    moodle_credential_login_allow_insecure_http: bool = False

    scheduler_poll_seconds: float = Field(
        default=5,
        gt=0,
        validation_alias="DEADLINE_POLL_SECONDS",
    )
    sync_poll_seconds: float = Field(
        default=2,
        gt=0,
        validation_alias="LMS_SYNC_WORKER_POLL_SECONDS",
    )
    sync_worker_concurrency: int = Field(
        default=2,
        ge=1,
        le=8,
        validation_alias="LMS_SYNC_WORKER_CONCURRENCY",
    )
    sync_terminal_concurrency: int = Field(
        default=2,
        ge=0,
        le=8,
        validation_alias="LMS_SYNC_TERMINAL_CONCURRENCY",
    )
    # The dedicated sync-worker remains the primary outbox consumer.  The web
    # process also keeps one narrow, terminal-checkpoint-only lane so a missing
    # or restarting worker can never strand a student's final submission.
    sync_embedded_terminal_worker_enabled: bool = Field(
        default=True,
        validation_alias="LMS_EMBEDDED_TERMINAL_WORKER_ENABLED",
    )
    sync_lease_seconds: int = Field(
        default=300,
        ge=5,
        validation_alias="LMS_SYNC_LOCK_TIMEOUT_SECONDS",
    )
    sync_retry_base_seconds: int = Field(
        default=15,
        ge=1,
        validation_alias="LMS_SYNC_RETRY_BASE_SECONDS",
    )
    sync_retry_max_seconds: int = Field(
        default=3_600,
        ge=1,
        validation_alias="LMS_SYNC_RETRY_MAX_SECONDS",
    )
    sync_max_attempts: int = Field(
        default=8,
        ge=1,
        validation_alias="LMS_SYNC_MAX_ATTEMPTS",
    )
    sync_course_interval_seconds: int = Field(
        default=900,
        ge=60,
        validation_alias="LMS_SYNC_COURSE_INTERVAL_SECONDS",
    )
    sync_checkpoint_max_bytes: int = Field(
        default=4 * 1024 * 1024,
        ge=1024,
        validation_alias="LMS_CHECKPOINT_MAX_BYTES",
    )
    sync_checkpoint_max_files: int = Field(
        default=128,
        ge=1,
        le=512,
        validation_alias="LMS_CHECKPOINT_MAX_FILES",
    )
    sync_receipt_max_bytes: int = Field(
        default=64 * 1024,
        ge=1024,
        validation_alias="LMS_SYNC_RECEIPT_MAX_BYTES",
    )

    authorship_analyzer_url: str = ""
    authorship_analyzer_token: SecretStr = SecretStr("")
    authorship_analyzer_shared_secret: SecretStr = SecretStr("")
    authorship_analyzer_timeout_seconds: float = Field(default=30, gt=0, le=600)
    authorship_allow_insecure_http: bool = False
    authorship_pseudonym_secret: SecretStr = SecretStr("")
    authorship_max_export_bytes: int = Field(default=16 * 1024 * 1024, ge=1024)
    authorship_max_response_bytes: int = Field(default=1024 * 1024, ge=1024)

    plagiarism_shingle_size: int = Field(default=7, ge=2, le=50)
    plagiarism_winnow_window: int = Field(default=5, ge=2, le=100)
    plagiarism_min_score: float = Field(default=0.30, ge=0, le=1)
    plagiarism_max_evidence: int = Field(default=20, ge=1, le=1000)
    plagiarism_max_submissions: int = Field(default=300, ge=2)
    plagiarism_max_submission_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    plagiarism_max_scan_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)

    @field_validator("session_cookie_samesite", mode="before")
    @classmethod
    def normalize_same_site(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def resolve_llm_configuration(self) -> Settings:
        if self.llm_api_address is not None:
            self.ai_base_url = self.llm_api_address.strip()
            if "ai_api_style" not in self.model_fields_set:
                self.ai_api_style = "auto"
        if self.llm_api_key is not None:
            # Explicitly empty LLM_API_KEY disables legacy key inheritance.
            self.ai_api_key = self.llm_api_key
        if self.llm_model is not None:
            self.ai_model = self.llm_model.strip()
        return self

    @field_validator("llm_thinking", mode="before")
    @classmethod
    def normalize_llm_thinking(cls, value: object) -> object:
        return None if value == "" else value

    @property
    def ai_provider_configured(self) -> bool:
        if self.ai_mock_enabled:
            return True
        if not self.ai_enabled or not self.ai_base_url.strip() or not self.ai_model.strip():
            return False
        # Local/self-hosted servers may deliberately run without API-key auth.
        # Known hosted providers still require their key.
        try:
            host = urlsplit(self.ai_base_url).hostname
        except ValueError:
            return False
        return bool(host) and (
            host not in {"api.openai.com", "openrouter.ai"}
            or bool(self.ai_api_key.get_secret_value())
        )

    @field_validator("moodle_browser_service_url", mode="before")
    @classmethod
    def normalize_moodle_browser_service_url(cls, value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            parsed = urlsplit(text)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("MOODLE_BROWSER_SERVICE_URL is invalid") from exc
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("MOODLE_BROWSER_SERVICE_URL must be an exact HTTP(S) origin")
        hostname = parsed.hostname.lower()
        hostname_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        netloc = (
            hostname_for_netloc if port in {None, default_port} else f"{hostname_for_netloc}:{port}"
        )
        return urlunsplit((parsed.scheme.lower(), netloc, "", "", ""))

    @model_validator(mode="after")
    def validate_security(self) -> Settings:
        if not self.debug and len(self.secret_key.get_secret_value()) < 32:
            raise ValueError("APP_SECRET_KEY must contain at least 32 characters")
        if not self.debug and self.admin_token.get_secret_value():
            raise ValueError("ADMIN_TOKEN plaintext is allowed only when APP_DEBUG=true")
        if self.admin_token_hash and not self.admin_token_hash.startswith("$argon2id$"):
            raise ValueError(
                "ADMIN_TOKEN_HASH must be an Argon2id verifier generated by "
                "the hash-admin-token command"
            )
        if self.session_cookie_samesite == "none" and not self.cookie_secure:
            raise ValueError("SameSite=None requires SESSION_COOKIE_SECURE=true")
        if self.evidence_running_stale_seconds <= self.evidence_total_timeout_seconds + 15:
            raise ValueError(
                "EVIDENCE_RUNNING_STALE_SECONDS must exceed the total evidence timeout by 15s"
            )
        browser_secret = self.moodle_browser_shared_secret.get_secret_value()
        if bool(self.moodle_browser_service_url) != bool(browser_secret):
            raise ValueError(
                "MOODLE_BROWSER_SERVICE_URL and MOODLE_BROWSER_SHARED_SECRET "
                "must be configured together"
            )
        if browser_secret and len(browser_secret.encode("utf-8")) < 32:
            raise ValueError("MOODLE_BROWSER_SHARED_SECRET must contain at least 32 bytes")
        if self.moodle_browser_storage_state_max_bytes > self.moodle_browser_request_body_max_bytes:
            raise ValueError(
                "MOODLE_BROWSER_STORAGE_STATE_MAX_BYTES must not exceed "
                "MOODLE_BROWSER_REQUEST_BODY_MAX_BYTES"
            )
        return self

    @property
    def cookie_secure(self) -> bool:
        return not self.debug if self.session_cookie_secure is None else self.session_cookie_secure

    @property
    def async_database_url(self) -> str:
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            url = "postgresql://" + url.removeprefix("postgres://")
        if url.startswith("postgresql://"):
            return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
        if url.startswith("sqlite:///"):
            return "sqlite+aiosqlite:///" + url.removeprefix("sqlite:///")
        return url

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
