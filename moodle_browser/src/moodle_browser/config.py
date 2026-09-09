from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _as_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def exact_https_origin(value: str) -> str:
    """Return a canonical HTTPS origin and reject every URL component beyond it."""

    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise ValueError("Moodle base URL must be an exact HTTPS origin") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Moodle base URL must be an exact HTTPS origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    return f"https://{host}{f':{port}' if port not in {None, 443} else ''}"


@dataclass(frozen=True, slots=True)
class Settings:
    shared_secret: bytes
    base_url: str = "https://edu.mmcs.sfedu.ru"
    headless: bool = True
    # One Chromium process is shared by short interactive logins and longer
    # background crawls.  Two contexts let us reserve capacity for login while
    # still keeping the N150 deployment bounded.
    max_concurrent_operations: int = 3
    queue_wait_seconds: float = 2.0
    navigation_timeout_ms: int = 30_000
    max_login_course_role_pages: int = 64
    login_course_role_budget_seconds: float = 15.0
    login_operation_timeout_seconds: float = 45.0
    activity_detail_budget_seconds: float = 240.0
    max_participant_pages: int = 50
    participants_per_page: int = 100
    max_participants: int = 5_000
    max_course_section_pages: int = 128
    storage_state_max_bytes: int = 256 * 1024
    artifact_max_bytes: int = 4 * 1024 * 1024
    request_body_max_bytes: int = 6 * 1024 * 1024
    idempotency_cache_entries: int = 1_000
    signature_clock_skew_seconds: int = 60
    nonce_ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if len(self.shared_secret) < 32:
            raise ValueError("MOODLE_BROWSER_SHARED_SECRET must contain at least 32 bytes")
        object.__setattr__(self, "base_url", exact_https_origin(self.base_url))
        if not 1 <= self.max_concurrent_operations <= 8:
            raise ValueError("MOODLE_BROWSER_MAX_CONCURRENT_OPERATIONS must be between 1 and 8")
        if not 0.05 <= self.queue_wait_seconds <= 60:
            raise ValueError("MOODLE_BROWSER_QUEUE_WAIT_SECONDS is outside the supported range")
        if not 1_000 <= self.navigation_timeout_ms <= 120_000:
            raise ValueError("MOODLE_BROWSER_NAVIGATION_TIMEOUT_MS is outside the supported range")
        if not 1 <= self.max_login_course_role_pages <= 512:
            raise ValueError(
                "MOODLE_BROWSER_MAX_LOGIN_COURSE_ROLE_PAGES is outside the supported range"
            )
        if not 1 <= self.login_course_role_budget_seconds <= 60:
            raise ValueError(
                "MOODLE_BROWSER_LOGIN_COURSE_ROLE_BUDGET_SECONDS is outside the supported range"
            )
        if not 10 <= self.login_operation_timeout_seconds <= 120:
            raise ValueError(
                "MOODLE_BROWSER_LOGIN_OPERATION_TIMEOUT_SECONDS is outside the supported range"
            )
        if not 30 <= self.activity_detail_budget_seconds <= 480:
            raise ValueError(
                "MOODLE_BROWSER_ACTIVITY_DETAIL_BUDGET_SECONDS is outside the supported range"
            )
        if not 1 <= self.max_participant_pages <= 200:
            raise ValueError("MOODLE_BROWSER_MAX_PARTICIPANT_PAGES is outside the supported range")
        if not 10 <= self.participants_per_page <= 500:
            raise ValueError("MOODLE_BROWSER_PARTICIPANTS_PER_PAGE is outside the supported range")
        if not 1 <= self.max_participants <= 10_000:
            raise ValueError("MOODLE_BROWSER_MAX_PARTICIPANTS is outside the supported range")
        if not 1 <= self.max_course_section_pages <= 512:
            raise ValueError(
                "MOODLE_BROWSER_MAX_COURSE_SECTION_PAGES is outside the supported range"
            )
        if not 16 * 1024 <= self.storage_state_max_bytes <= 1024 * 1024:
            raise ValueError(
                "MOODLE_BROWSER_STORAGE_STATE_MAX_BYTES is outside the supported range"
            )
        if not 1 <= self.artifact_max_bytes <= 4 * 1024 * 1024:
            raise ValueError("MOODLE_BROWSER_ARTIFACT_MAX_BYTES is outside the supported range")
        minimum_request_size = max(
            self.storage_state_max_bytes,
            ((self.artifact_max_bytes + 2) // 3) * 4 + 64 * 1024,
        )
        if not minimum_request_size <= self.request_body_max_bytes <= 8 * 1024 * 1024:
            raise ValueError(
                "MOODLE_BROWSER_REQUEST_BODY_MAX_BYTES is outside the supported range"
            )
        if not 1 <= self.idempotency_cache_entries <= 10_000:
            raise ValueError(
                "MOODLE_BROWSER_IDEMPOTENCY_CACHE_ENTRIES is outside the supported range"
            )
        if not 1 <= self.signature_clock_skew_seconds <= 600:
            raise ValueError("signature clock skew is outside the supported range")
        if not 1 <= self.nonce_ttl_seconds <= 3_600:
            raise ValueError("nonce TTL is outside the supported range")

    def require_base_url(self, value: str) -> None:
        if exact_https_origin(value) != self.base_url:
            raise ValueError("Moodle base URL does not match the configured origin")

    @classmethod
    def from_env(cls) -> Settings:
        direct = os.environ.get("MOODLE_BROWSER_SHARED_SECRET")
        secret_file = os.environ.get("MOODLE_BROWSER_SHARED_SECRET_FILE")
        if direct is not None and secret_file is not None:
            raise RuntimeError("set only one Moodle browser shared-secret source")
        if secret_file is not None:
            path = Path(secret_file)
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("MOODLE_BROWSER_SHARED_SECRET_FILE must be a regular file")
            secret = path.read_bytes().rstrip(b"\r\n")
        elif direct is not None:
            secret = direct.encode("utf-8")
        else:
            raise RuntimeError("MOODLE_BROWSER_SHARED_SECRET is required")
        return cls(
            shared_secret=secret,
            base_url=os.environ.get("MOODLE_BROWSER_BASE_URL", "https://edu.mmcs.sfedu.ru"),
            headless=_as_bool(os.environ.get("MOODLE_BROWSER_HEADLESS"), default=True),
            max_concurrent_operations=int(
                os.environ.get("MOODLE_BROWSER_MAX_CONCURRENT_OPERATIONS", "3")
            ),
            queue_wait_seconds=float(os.environ.get("MOODLE_BROWSER_QUEUE_WAIT_SECONDS", "8")),
            navigation_timeout_ms=int(
                os.environ.get("MOODLE_BROWSER_NAVIGATION_TIMEOUT_MS", "30000")
            ),
            max_login_course_role_pages=int(
                os.environ.get("MOODLE_BROWSER_MAX_LOGIN_COURSE_ROLE_PAGES", "64")
            ),
            login_course_role_budget_seconds=float(
                os.environ.get("MOODLE_BROWSER_LOGIN_COURSE_ROLE_BUDGET_SECONDS", "15")
            ),
            login_operation_timeout_seconds=float(
                os.environ.get("MOODLE_BROWSER_LOGIN_OPERATION_TIMEOUT_SECONDS", "45")
            ),
            activity_detail_budget_seconds=float(
                os.environ.get("MOODLE_BROWSER_ACTIVITY_DETAIL_BUDGET_SECONDS", "240")
            ),
            max_participant_pages=int(
                os.environ.get("MOODLE_BROWSER_MAX_PARTICIPANT_PAGES", "50")
            ),
            participants_per_page=int(
                os.environ.get("MOODLE_BROWSER_PARTICIPANTS_PER_PAGE", "100")
            ),
            max_participants=int(os.environ.get("MOODLE_BROWSER_MAX_PARTICIPANTS", "5000")),
            max_course_section_pages=int(
                os.environ.get("MOODLE_BROWSER_MAX_COURSE_SECTION_PAGES", "128")
            ),
            storage_state_max_bytes=int(
                os.environ.get("MOODLE_BROWSER_STORAGE_STATE_MAX_BYTES", str(256 * 1024))
            ),
            artifact_max_bytes=int(
                os.environ.get("MOODLE_BROWSER_ARTIFACT_MAX_BYTES", str(4 * 1024 * 1024))
            ),
            request_body_max_bytes=int(
                os.environ.get("MOODLE_BROWSER_REQUEST_BODY_MAX_BYTES", str(6 * 1024 * 1024))
            ),
            idempotency_cache_entries=int(
                os.environ.get("MOODLE_BROWSER_IDEMPOTENCY_CACHE_ENTRIES", "1000")
            ),
            signature_clock_skew_seconds=int(
                os.environ.get("MOODLE_BROWSER_SIGNATURE_CLOCK_SKEW_SECONDS", "60")
            ),
            nonce_ttl_seconds=int(os.environ.get("MOODLE_BROWSER_NONCE_TTL_SECONDS", "300")),
        )
