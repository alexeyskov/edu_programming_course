from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import re
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import (
    Browser,
    BrowserContext,
    Locator,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from .assignment import (
    ASSIGNMENT_FILEMANAGER_SELECTOR,
    ASSIGNMENT_ONLINE_TEXT_SELECTOR,
    ASSIGNMENT_SAVE_SELECTOR,
    AssignmentSubmissionForm,
    AssignmentSubmissionView,
    parse_assignment_confirmation_page,
    parse_assignment_edit_page,
    parse_assignment_view_page,
)
from .config import Settings, exact_https_origin
from .historical import (
    _trim_empty_boundary_lines,
    finalize_historical_submission,
    parse_assignment_grader_page,
    parse_assignment_grading_page,
    parse_quiz_report_page,
    parse_quiz_review_page,
    prioritize_historical_attempts,
    quiz_review_navigation_urls,
)
from .models import (
    AssignmentSubmissionPrepareRequest,
    AssignmentSubmissionPrepareResponse,
    AssignmentSubmissionSyncRequest,
    AssignmentSubmissionSyncResponse,
    BrowserStorageState,
    CourseDiscoverRequest,
    CourseDiscoverResponse,
    GradeRequest,
    GradeResponse,
    HistoricalSubmissionsRequest,
    HistoricalSubmissionsResponse,
    LoginRequest,
    LoginResponse,
    QuizEssayPrepareRequest,
    QuizEssayPrepareResponse,
    QuizEssaySyncRequest,
    QuizEssaySyncResponse,
)
from .parsers import (
    MoodleMarkupError,
    canonical_hash,
    has_authenticated_markup,
    merge_course_sections,
    parse_activity_settings,
    parse_activity_user_override_edit,
    parse_activity_user_override_index,
    parse_course_links,
    parse_course_page,
    parse_course_section_links,
    parse_identity,
    parse_participants_page,
    parse_quiz_essay_question_edit,
    parse_quiz_question_summary,
    parse_quiz_random_question_bank,
    teacher_controls_present,
)
from .quiz import (
    ESSAY_SELECTOR,
    FILE_ADD_SELECTOR,
    FILE_INPUT_SELECTOR,
    FILE_OVERWRITE_SELECTOR,
    FILE_UPLOAD_SELECTOR,
    FILEMANAGER_SELECTOR,
    FINALIZE_ATTEMPT_SELECTOR,
    FINALIZE_CMID_SELECTOR,
    FINALIZE_FINISH_SELECTOR,
    FINALIZE_FORM_SELECTOR,
    FINALIZE_SESSKEY_SELECTOR,
    FINALIZE_TIMEUP_SELECTOR,
    FINALIZE_TRIGGER_SELECTOR,
    NEXT_NAV_SELECTOR,
    ONLINE_TEXT_SELECTOR,
    QUIZ_CONTINUE_LINK_SELECTOR,
    QUIZ_DIRECT_START_FORM_SELECTOR,
    QUIZ_PREFLIGHT_FORM_SELECTOR,
    QUIZ_PREFLIGHT_START_SELECTOR,
    QuizAttempt,
    QuizAttemptNotActive,
    QuizAttemptUnavailable,
    QuizLaunch,
    QuizSummary,
    finalize_text_matches,
    parse_attempt_page,
    parse_quiz_view,
    parse_summary_page,
    validate_final_page,
)
from .storage import InvalidStorageState, has_moodle_session, sanitize_storage_state

if TYPE_CHECKING:
    from playwright.async_api import Request, Route


_LOGIN_ROLE_PAGE_MAX_BYTES = 2 * 1024 * 1024
_LOGIN_ROLE_FETCH_TIMEOUT_MS = 3_000
_ACTIVITY_DETAIL_FETCH_TIMEOUT_MS = 2_000
_ACTIVITY_DETAIL_MAX_PAGES = 256
_ASSESSMENT_DETAIL_TYPE_WORDS: tuple[tuple[str, ...], ...] = (
    ("экзамен", "exam"),
    ("самостоятель", "independent"),
    ("контрольн", "проверочн", "control"),
    ("лаборатор", "практич", "lab"),
)
_ASSESSMENT_DETAIL_ARCHIVE_WORDS = ("архив", "archive")
_QUESTION_MARK_MAX = re.compile(
    r"(?:(?:out\s+of|из)\s*|/\s*)([0-9]{1,7}(?:[.,][0-9]{1,6})?)",
    re.I,
)
_QUIZ_MARK_QUANTUM = Decimal("0.0000000001")
# Keep the serialized JSON safely below the core client's default 4 MiB
# response ceiling after storage_state, JSON escaping and fixed metadata.
_HISTORICAL_CONTENT_BUDGET_BYTES = 2 * 1024 * 1024
_HISTORICAL_QUIZ_REVIEW_MAX_PAGES = 64
_HISTORICAL_QUIZ_REVIEW_MAX_RESPONSES = 32
_MANAGED_SUBMISSION_FILENAMES = frozenset(
    {"solution.c", "solution.cpp", "main.c", "main.cpp", "submission.zip"}
)


def _activity_needs_assessment_detail(
    activity: dict[str, Any],
    section_title: str,
) -> bool:
    """Select activities the core can materialize as programming assessments.

    Discovery still returns every course-page activity. Fetching Moodle edit,
    override, and question-bank pages is reserved for the exact title/section
    families accepted by ``backend.app.services.moodle_materialization``. This
    keeps unsupported and archived activities fail-closed without spending the
    bounded browser session on details the core deliberately discards.
    """

    if str(activity.get("module", "")) not in {"assign", "quiz"}:
        return False
    evidence = re.sub(
        r"\s+",
        " ",
        f"{section_title} {activity.get('name', '')}".casefold(),
    ).strip()
    if any(marker in evidence for marker in _ASSESSMENT_DETAIL_ARCHIVE_WORDS):
        return False
    return any(
        marker in evidence for markers in _ASSESSMENT_DETAIL_TYPE_WORDS for marker in markers
    )


def _managed_target_replace_existing(
    existing_filenames: tuple[str, ...],
    target_filename: str,
    previous_managed_filename: str | None,
) -> bool:
    """Return whether the target is a proven connector-owned overwrite slot.

    A Moodle filename is not ownership evidence.  A pre-existing target may be
    overwritten only when a durable receipt for this attempt names that exact
    target.  A previous artifact with another name is deliberately retained:
    deleting by a rendered basename cannot prove that the entry is a root file
    rather than a nested or user-owned attachment.
    """

    if target_filename not in existing_filenames:
        return False
    if previous_managed_filename != target_filename:
        raise MoodleProtocolError(
            "Moodle artifact target already exists but connector ownership is unproven"
        )
    return True


_LOGIN_ROLE_FETCH_SCRIPT = """
async ({url, timeoutMs, maxBytes}) => {
  const target = new URL(url);
  if (target.origin !== window.location.origin) {
    return {error: "foreign-origin"};
  }

  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(target.href, {
      cache: "no-store",
      credentials: "include",
      redirect: "manual",
      signal: controller.signal,
      headers: {Accept: "text/html,application/xhtml+xml"},
    });
    const declaredLength = Number(response.headers.get("content-length") || "0");
    if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
      return {status: response.status, url: response.url, tooLarge: true};
    }
    if (!response.body) {
      return {status: response.status, url: response.url, html: ""};
    }

    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        return {status: response.status, url: response.url, tooLarge: true};
      }
      chunks.push(value);
    }
    const body = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) {
      body.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return {
      status: response.status,
      url: response.url,
      html: new TextDecoder("utf-8", {fatal: false}).decode(body),
    };
  } catch (error) {
    return {error: error instanceof Error ? error.name : "fetch-failed"};
  } finally {
    window.clearTimeout(timer);
  }
}
"""
_HISTORICAL_BINARY_FETCH_SCRIPT = """
async ({url, maxBytes}) => {
  const target = new URL(url);
  if (
    target.origin !== window.location.origin ||
    !target.pathname.startsWith('/pluginfile.php/')
  ) {
    return {error: 'invalid-target'};
  }
  try {
    const response = await fetch(target.href, {
      cache: 'no-store',
      credentials: 'include',
      redirect: 'follow',
    });
    const finalUrl = new URL(response.url);
    if (finalUrl.origin !== window.location.origin || !response.ok) {
      return {status: response.status, error: 'download-failed'};
    }
    const declaredLength = Number(response.headers.get('content-length') || '0');
    if (Number.isFinite(declaredLength) && declaredLength > maxBytes) {
      return {status: response.status, tooLarge: true, size: declaredLength};
    }
    if (!response.body) {
      return {status: response.status, error: 'empty-body'};
    }
    const reader = response.body.getReader();
    const chunks = [];
    let total = 0;
    while (true) {
      const {done, value} = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > maxBytes) {
        await reader.cancel();
        return {status: response.status, tooLarge: true, size: total};
      }
      chunks.push(value);
    }
    let binary = '';
    for (const chunk of chunks) {
      for (let offset = 0; offset < chunk.length; offset += 32768) {
        binary += String.fromCharCode(...chunk.subarray(offset, offset + 32768));
      }
    }
    return {
      status: response.status,
      size: total,
      contentType: (response.headers.get('content-type') || '').slice(0, 255),
      contentBase64: btoa(binary),
    };
  } catch (error) {
    return {error: error instanceof Error ? error.name : 'download-failed'};
  }
}
"""


def _dashboard_courses_in_catalog(
    html: str,
    base_url: str,
    allowed_course_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Return only courses evidenced by both Moodle and the application catalogue."""

    allowed = set(allowed_course_ids)
    return {
        course["external_id"]: course
        for course in parse_course_links(html, base_url, maximum=512)
        if course["external_id"] in allowed
    }


class MoodleBrowserError(RuntimeError):
    pass


class BrowserUnavailable(MoodleBrowserError):
    pass


class BrowserBusy(MoodleBrowserError):
    pass


class MoodleCredentialsRejected(MoodleBrowserError):
    pass


class MoodleSessionExpired(MoodleCredentialsRejected):
    pass


class MoodleAttemptFinalized(MoodleBrowserError):
    pass


class MoodleActivityUnavailable(MoodleBrowserError):
    pass


class MoodleProtocolError(MoodleBrowserError):
    pass


class MoodleContractError(MoodleBrowserError):
    pass


class IdempotencyConflict(MoodleBrowserError):
    pass


class QuizPreviewRejected(MoodleBrowserError):
    pass


class TeacherMembershipRequired(MoodleBrowserError):
    pass


@dataclass(slots=True)
class _StudentSessionLock:
    lock: asyncio.Lock
    borrowers: int = 0


class MoodleBrowserService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_operations)
        # Every non-login operation also acquires this semaphore.  It leaves one
        # Chromium context permanently available for credential login.
        self._non_login_semaphore = asyncio.Semaphore(
            max(1, settings.max_concurrent_operations - 1)
        )
        # Background history/discovery work leaves a second slot available for
        # an explicit user-triggered course refresh on the default N=3 setup.
        self._background_semaphore = asyncio.Semaphore(
            max(1, settings.max_concurrent_operations - 2)
        )
        self._lifecycle_lock = asyncio.Lock()
        # Browser state belongs to one Moodle login. Mutations made through
        # the same MoodleSession must stay sequential, but unrelated students
        # must not share one global queue. Only an irreversible digest is kept
        # while an operation is active or waiting.
        self._student_session_locks: dict[bytes, _StudentSessionLock] = {}
        self._student_session_locks_guard = asyncio.Lock()
        self._quiz_sync_cache: OrderedDict[str, tuple[str, QuizEssaySyncResponse]] = OrderedDict()
        # A Quiz save and its final submission are two separate Moodle
        # mutations.  Retain the exact saved attempt while an idempotent
        # terminal request is in flight so a transport timeout retries only
        # the finalization step instead of uploading another file version.
        self._quiz_sync_progress: OrderedDict[
            str,
            tuple[str, str, str],
        ] = OrderedDict()
        self._assignment_sync_cache: OrderedDict[
            str,
            tuple[str, AssignmentSubmissionSyncResponse],
        ] = OrderedDict()

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._browser is not None and self._browser.is_connected():
                return
            await self._close_unlocked()
            try:
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.launch(
                    headless=self.settings.headless,
                )
            except Exception as exc:
                await self._close_unlocked()
                raise BrowserUnavailable("Chromium could not be started") from exc

    async def close(self) -> None:
        async with self._lifecycle_lock:
            await self._close_unlocked()

    async def _close_unlocked(self) -> None:
        browser, playwright = self._browser, self._playwright
        self._browser = None
        self._playwright = None
        if browser is not None:
            with suppress(Exception):
                await browser.close()
        if playwright is not None:
            with suppress(Exception):
                await playwright.stop()

    def readiness(self) -> dict[str, object]:
        if self._browser is None:
            browser = "not_started"
        elif self._browser.is_connected():
            browser = "connected"
        else:
            browser = "disconnected"
        ready = browser == "connected"
        return {"status": "ok" if ready else "unavailable", "browser": browser, "ready": ready}

    def _require_origin(self, supplied: str) -> None:
        try:
            self.settings.require_base_url(supplied)
        except ValueError as exc:
            raise MoodleContractError("Moodle origin is not configured") from exc

    @asynccontextmanager
    async def _operation(  # type: ignore[no-untyped-def]
        self,
        *,
        interactive: bool = False,
        foreground: bool = False,
    ):
        background_acquired = False
        non_login_acquired = False
        total_acquired = False
        try:
            if not interactive and not foreground:
                # Acquire the narrowest (background-only) permit first.  A
                # queued background crawl must not hold one of the permits
                # reserved for foreground synchronization while it merely
                # waits for another background crawl to finish.
                await asyncio.wait_for(
                    self._background_semaphore.acquire(),
                    timeout=self.settings.queue_wait_seconds,
                )
                background_acquired = True
            if not interactive:
                await asyncio.wait_for(
                    self._non_login_semaphore.acquire(),
                    timeout=self.settings.queue_wait_seconds,
                )
                non_login_acquired = True
            await asyncio.wait_for(
                self._semaphore.acquire(),
                timeout=self.settings.queue_wait_seconds,
            )
            total_acquired = True
        except BaseException as exc:
            if background_acquired:
                self._background_semaphore.release()
            if non_login_acquired:
                self._non_login_semaphore.release()
            if isinstance(exc, TimeoutError):
                raise BrowserBusy("Moodle browser is busy") from exc
            raise
        try:
            browser = self._browser
            if browser is None or not browser.is_connected():
                raise BrowserUnavailable("Chromium is not connected")
            yield browser
        finally:
            if total_acquired:
                self._semaphore.release()
            if background_acquired:
                self._background_semaphore.release()
            if non_login_acquired:
                self._non_login_semaphore.release()

    @staticmethod
    def _student_session_key(state: BrowserStorageState) -> bytes:
        """Derive a non-reversible identity without retaining the session secret."""

        session_values = sorted(
            {
                cookie.value
                for cookie in state.cookies
                if cookie.name == "MoodleSession" and cookie.value
            }
        )
        if not session_values:
            raise MoodleSessionExpired("Moodle browser session is missing")
        digest = hashlib.sha256()
        for value in session_values:
            encoded = value.encode("utf-8")
            digest.update(len(encoded).to_bytes(4, "big"))
            digest.update(encoded)
        return digest.digest()

    async def _borrow_student_session_lock(self, key: bytes) -> _StudentSessionLock:
        async with self._student_session_locks_guard:
            entry = self._student_session_locks.get(key)
            if entry is None:
                entry = _StudentSessionLock(asyncio.Lock())
                self._student_session_locks[key] = entry
            entry.borrowers += 1
            return entry

    async def _return_student_session_lock(
        self,
        key: bytes,
        entry: _StudentSessionLock,
    ) -> None:
        async with self._student_session_locks_guard:
            current = self._student_session_locks.get(key)
            if current is not entry:
                return
            entry.borrowers -= 1
            if entry.borrowers == 0:
                self._student_session_locks.pop(key, None)

    @asynccontextmanager
    async def _student_session_operation(
        self,
        state: BrowserStorageState,
        *,
        terminal: bool = False,
    ) -> AsyncIterator[None]:
        """Serialize one student's Moodle writes without blocking other students."""

        key = self._student_session_key(state)
        entry = await self._borrow_student_session_lock(key)
        # A final submission may arrive while that student's last periodic
        # checkpoint is still saving. Give that bounded operation one
        # navigation window to finish instead of bouncing the final request
        # through the retry queue every few seconds.
        wait_seconds = self.settings.queue_wait_seconds
        if terminal:
            wait_seconds = max(
                wait_seconds,
                self.settings.navigation_timeout_ms / 1_000,
            )
        acquired = False
        try:
            try:
                await asyncio.wait_for(entry.lock.acquire(), timeout=wait_seconds)
                acquired = True
            except TimeoutError as exc:
                raise BrowserBusy("Moodle student browser session is busy") from exc
            yield
        finally:
            if acquired:
                entry.lock.release()
            await asyncio.shield(self._return_student_session_lock(key, entry))

    async def _new_context(
        self,
        browser: Browser,
        *,
        storage_state: BrowserStorageState | None = None,
    ) -> BrowserContext:
        options: dict[str, Any] = {
            "accept_downloads": False,
            "service_workers": "block",
            "viewport": {"width": 1365, "height": 900},
        }
        if storage_state is not None:
            options["storage_state"] = storage_state.model_dump(mode="json")
        context = await browser.new_context(**options)
        context.set_default_timeout(self.settings.navigation_timeout_ms)
        context.set_default_navigation_timeout(self.settings.navigation_timeout_ms)

        async def restrict_route(route: Route, request: Request) -> None:
            try:
                parsed = urlsplit(request.url)
                origin = exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
            except ValueError:
                await route.abort("blockedbyclient")
                return
            if origin != self.settings.base_url:
                await route.abort("blockedbyclient")
                return
            await route.continue_()

        await context.route("**/*", restrict_route)
        return context

    async def _goto(self, page: Page, url: str) -> str:
        try:
            response = await page.goto(url, wait_until="domcontentloaded")
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle navigation failed") from exc
        if response is not None and response.status >= 500:
            raise BrowserUnavailable("Moodle returned a server error")
        try:
            current = urlsplit(page.url)
            current_origin = exact_https_origin(f"{current.scheme}://{current.netloc}")
        except ValueError as exc:
            raise MoodleProtocolError("Moodle redirected outside its configured origin") from exc
        if current_origin != self.settings.base_url:
            raise MoodleProtocolError("Moodle redirected outside its configured origin")
        return await page.content()

    async def _state(self, context: BrowserContext) -> BrowserStorageState:
        try:
            raw = await context.storage_state()
            return sanitize_storage_state(
                raw,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=False,
            )
        except (InvalidStorageState, PlaywrightError) as exc:
            raise MoodleProtocolError("Moodle browser state is invalid") from exc

    async def _authenticated(
        self,
        context: BrowserContext,
        html: str,
    ) -> tuple[bool, BrowserStorageState]:
        state = await self._state(context)
        return has_moodle_session(state) and has_authenticated_markup(
            html, self.settings.base_url
        ), state

    async def login(self, request: LoginRequest) -> LoginResponse:
        self._require_origin(request.base_url)
        async with self._operation(interactive=True) as browser:
            context = await self._new_context(browser)
            try:
                page = await context.new_page()
                await self._goto(page, f"{self.settings.base_url}/login/index.php")
                try:
                    await page.locator("#username").fill(request.username)
                    await page.locator("#password").fill(request.password.get_secret_value())
                    await page.locator("#loginbtn").click()
                    await page.wait_for_load_state("domcontentloaded")
                    html = await page.content()
                except (PlaywrightTimeoutError, PlaywrightError) as exc:
                    raise BrowserUnavailable("Moodle login page could not be operated") from exc

                authenticated, state = await self._authenticated(context, html)
                if not authenticated:
                    path = urlsplit(page.url).path.rstrip("/")
                    if path == "/login/index.php" or 'id="loginbtn"' in html:
                        raise MoodleCredentialsRejected("Moodle credentials were not accepted")
                    raise MoodleProtocolError("Moodle login response has no authenticated session")

                dashboard_html = await self._goto(page, f"{self.settings.base_url}/my/")
                authenticated, _ = await self._authenticated(context, dashboard_html)
                if not authenticated:
                    raise MoodleSessionExpired("Moodle browser session expired")
                # A configured course id is only an application allow-list,
                # not evidence that this account is enrolled.  Intersect it
                # with Moodle's own dashboard before opening course pages, so
                # a public/guest-readable course can never manufacture a
                # STUDENT membership.
                courses_by_id = _dashboard_courses_in_catalog(
                    dashboard_html,
                    self.settings.base_url,
                    request.allowed_course_ids,
                )

                # Several Moodle themes omit the current user's numeric id from
                # the user-menu profile URL.  Open the server-selected current
                # profile first and prefer its identity evidence over links on
                # the landing page, which can refer to unrelated users.
                profile_html = await self._goto(
                    page,
                    f"{self.settings.base_url}/user/profile.php",
                )
                authenticated, state = await self._authenticated(context, profile_html)
                if not authenticated:
                    raise MoodleSessionExpired("Moodle browser session expired")

                identity: dict[str, str] | None = None
                last_identity_error: MoodleMarkupError | None = None
                for candidate_html in (profile_html, dashboard_html, html):
                    try:
                        identity = parse_identity(candidate_html, self.settings.base_url)
                        break
                    except MoodleMarkupError as exc:
                        last_identity_error = exc
                if identity is None:
                    raise MoodleProtocolError(
                        str(last_identity_error or "Moodle profile identity is unavailable")
                    ) from last_identity_error

                # Re-open the canonical profile URL built exclusively from the
                # id parsed from Moodle and verify that identity cannot change
                # between the two pages.
                canonical_profile_html = await self._goto(
                    page,
                    f"{self.settings.base_url}/user/profile.php?"
                    + urlencode({"id": identity["external_subject"]}),
                )
                authenticated, state = await self._authenticated(context, canonical_profile_html)
                if not authenticated:
                    raise MoodleSessionExpired("Moodle browser session expired")
                try:
                    profile = parse_identity(canonical_profile_html, self.settings.base_url)
                except MoodleMarkupError:
                    profile = identity
                if profile["external_subject"] != identity["external_subject"]:
                    raise MoodleProtocolError("Moodle profile identity changed during login")
                if not profile.get("email"):
                    profile["email"] = identity.get("email", "")
                await self._classify_login_course_roles(page, courses_by_id)
                state = await self._state(context)
                return LoginResponse.model_validate(
                    {
                        "identity": {**profile, "courses": list(courses_by_id.values())},
                        "storage_state": state,
                    }
                )
            finally:
                await context.close()

    async def _classify_login_course_roles(
        self,
        page: Page,
        courses_by_id: dict[str, dict[str, str]],
    ) -> None:
        """Confirm roles from bounded, canonical course-page responses.

        A dashboard card proves only that a course link exists.  Teacher access
        is granted only when the matching course page exposes a privileged
        control for that same numeric course id.  The HTML is fetched without a
        full navigation/render inside the already authenticated browser page.
        Successfully loaded pages without such controls are student evidence;
        redirects, timeouts and pages beyond either configured bound stay
        UNKNOWN and cannot elevate privileges or make an otherwise valid login
        fail.
        """

        try:
            async with asyncio.timeout(self.settings.login_course_role_budget_seconds):
                for index, (course_id, course) in enumerate(courses_by_id.items()):
                    if index >= self.settings.max_login_course_role_pages:
                        break
                    course_url = f"{self.settings.base_url}/course/view.php?" + urlencode(
                        {"id": course_id}
                    )
                    try:
                        result = await page.evaluate(
                            _LOGIN_ROLE_FETCH_SCRIPT,
                            {
                                "url": course_url,
                                "timeoutMs": _LOGIN_ROLE_FETCH_TIMEOUT_MS,
                                "maxBytes": _LOGIN_ROLE_PAGE_MAX_BYTES,
                            },
                        )
                    except (PlaywrightTimeoutError, PlaywrightError):
                        continue
                    if not isinstance(result, dict) or result.get("status") != 200:
                        continue
                    html = result.get("html")
                    response_url = result.get("url")
                    if not isinstance(html, str) or not isinstance(response_url, str):
                        continue
                    current = urlsplit(response_url)
                    try:
                        response_origin = exact_https_origin(
                            f"{current.scheme}://{current.netloc}"
                        )
                    except ValueError:
                        continue
                    query = parse_qs(current.query, keep_blank_values=True)
                    if (
                        response_origin != self.settings.base_url
                        or current.path.rstrip("/") != "/course/view.php"
                        or query.get("id") != [course_id]
                        or not has_authenticated_markup(html, self.settings.base_url)
                    ):
                        continue
                    course["role"] = (
                        "TEACHER"
                        if teacher_controls_present(html, self.settings.base_url, course_id)
                        else "STUDENT"
                    )
        except TimeoutError:
            # Role enrichment is optional.  The authenticated login response is
            # returned with any unconfirmed courses left fail-closed as UNKNOWN.
            return

    async def discover_course(
        self,
        request: CourseDiscoverRequest,
    ) -> CourseDiscoverResponse:
        self._require_origin(request.base_url)
        try:
            input_state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(input_state):
            raise MoodleSessionExpired("Moodle browser session is missing")

        async with self._operation(foreground=request.interactive) as browser:
            context = await self._new_context(browser, storage_state=input_state)
            try:
                page = await context.new_page()
                course_url = f"{self.settings.base_url}/course/view.php?" + urlencode(
                    {"id": request.external_id}
                )
                course_html = await self._goto(page, course_url)
                authenticated, _ = await self._authenticated(context, course_html)
                if not authenticated:
                    raise MoodleSessionExpired("Moodle browser session expired")
                try:
                    identity = parse_identity(course_html, self.settings.base_url)
                    course = parse_course_page(
                        course_html,
                        self.settings.base_url,
                        request.external_id,
                    )
                except MoodleMarkupError as exc:
                    raise MoodleProtocolError(str(exc)) from exc
                if identity["external_subject"] != request.actor_external_subject:
                    raise MoodleProtocolError("Moodle identity does not match the course actor")
                course = await self._crawl_course_section_pages(
                    page,
                    context,
                    request.external_id,
                    course_html,
                    course,
                )
                # The roster is required for authorization and historical
                # import.  Collect it before optional per-activity settings so
                # a slow Moodle settings page cannot postpone all useful
                # synchronization work until the outer request timeout.
                members, roster_complete = await self._participants(
                    page, context, request.external_id
                )
                course = await self._enrich_course_activities(
                    page,
                    context,
                    request.external_id,
                    course,
                )
                actor = next(
                    (
                        member
                        for member in members
                        if member["user_id"] == request.actor_external_subject
                    ),
                    None,
                )
                controls_confirm_teacher = bool(course.pop("teacher_controls"))
                grade_controls = bool(course.pop("grade_controls"))
                actor_is_teacher = (
                    bool(actor is not None and "TEACHER" in actor.get("roles", []))
                    or controls_confirm_teacher
                )
                if not actor_is_teacher:
                    raise TeacherMembershipRequired(
                        "Moodle did not confirm teacher membership in the course"
                    )
                if actor is None:
                    actor = {
                        "user_id": request.actor_external_subject,
                        "display_name": identity["display_name"],
                        "email": identity.get("email", ""),
                        "suspended": False,
                        "role": "TEACHER",
                        "roles": ["TEACHER"],
                        "groups": [],
                    }
                    members.append(actor)
                    # A hidden actor row means the roster cannot be considered complete.
                    roster_complete = False
                elif controls_confirm_teacher and "TEACHER" not in actor["roles"]:
                    actor["roles"] = ["TEACHER", *actor["roles"]][:2]
                    actor["role"] = "TEACHER"

                members.sort(key=lambda member: int(member["user_id"]))
                groups_by_id: dict[str, dict[str, str]] = {}
                for member in members:
                    for group in member.get("groups", []):
                        groups_by_id[group["external_id"]] = group
                membership_snapshot = {
                    "complete": roster_complete,
                    "members": members,
                }
                membership_revision = canonical_hash(membership_snapshot)
                course_projection = {
                    **course,
                    "groups": sorted(groups_by_id.values(), key=lambda item: item["external_id"]),
                }
                external_revision = canonical_hash(course_projection)
                preview = {
                    **course_projection,
                    "external_revision": external_revision,
                    "membership_revision": membership_revision,
                    "membership_snapshot": membership_snapshot,
                }
                state = await self._state(context)
                return CourseDiscoverResponse.model_validate(
                    {
                        "discovery": {
                            "external_id": request.external_id,
                            "actor_role": "TEACHER",
                            "preview": preview,
                            "capabilities": {
                                "roster": roster_complete,
                                "groups": roster_complete,
                                "grades": grade_controls,
                                "comments": grade_controls,
                                "checkpoints": False,
                                "task_bank_mirror": False,
                                "native_question_bank_write": False,
                            },
                        },
                        "storage_state": state,
                    }
                )
            finally:
                await context.close()

    async def discover_historical_submissions(
        self,
        request: HistoricalSubmissionsRequest,
    ) -> HistoricalSubmissionsResponse:
        """Read one bounded chunk of teacher-visible historical submissions."""

        self._require_origin(request.base_url)
        try:
            input_state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(input_state):
            raise MoodleSessionExpired("Moodle browser session is missing")

        page_number, offset = (int(value) for value in request.cursor.split(":"))
        async with self._operation() as browser:
            context = await self._new_context(browser, storage_state=input_state)
            try:
                page = await context.new_page()
                course_url = f"{self.settings.base_url}/course/view.php?" + urlencode(
                    {"id": request.course_id}
                )
                course_html = await self._goto(page, course_url)
                await self._require_authenticated_page(context, course_html)
                try:
                    identity = parse_identity(course_html, self.settings.base_url)
                except MoodleMarkupError as exc:
                    raise MoodleProtocolError(str(exc)) from exc
                if identity["external_subject"] != request.actor_external_subject:
                    raise MoodleProtocolError("Moodle identity does not match the course actor")
                if not teacher_controls_present(
                    course_html, self.settings.base_url, request.course_id
                ):
                    raise TeacherMembershipRequired(
                        "Moodle did not confirm teacher membership in the course"
                    )

                module = request.activity.module
                cmid = request.activity.cmid
                if module == "quiz":
                    report_url = f"{self.settings.base_url}/mod/quiz/report.php?" + urlencode(
                        {
                            "id": cmid,
                            "mode": "overview",
                            "attempts": "enrolled_with",
                            "onlyregraded": 0,
                            "slotmarks": 1,
                            "group": 0,
                            # Moodle otherwise reuses the teacher's small table
                            # preference (30 rows on the production course),
                            # placing fresh submissions on page four or later.
                            "pagesize": min(self.settings.max_participants, 500),
                            "page": page_number,
                        }
                    )
                    report_html = await self._goto(page, report_url)
                    await self._require_authenticated_page(context, report_html)
                    report_location = urlsplit(page.url)
                    report_query = parse_qs(report_location.query, keep_blank_values=True)
                    if (
                        report_location.path.rstrip("/") != "/mod/quiz/report.php"
                        or report_query.get("id") != [str(cmid)]
                        or report_query.get("mode") != ["overview"]
                    ):
                        raise MoodleProtocolError("Moodle quiz report navigation changed target")
                    try:
                        index = parse_quiz_report_page(
                            report_html,
                            base_url=self.settings.base_url,
                            course_id=request.course_id,
                            cmid=cmid,
                            page_number=page_number,
                        )
                    except MoodleMarkupError as exc:
                        raise MoodleProtocolError(str(exc)) from exc
                else:
                    report_url = f"{self.settings.base_url}/mod/assign/view.php?" + urlencode(
                        {
                            "id": cmid,
                            "action": "grading",
                            "page": page_number,
                            "perpage": 100,
                        }
                    )
                    report_html = await self._goto(page, report_url)
                    await self._require_authenticated_page(context, report_html)
                    report_location = urlsplit(page.url)
                    report_query = parse_qs(report_location.query, keep_blank_values=True)
                    if (
                        report_location.path.rstrip("/") != "/mod/assign/view.php"
                        or report_query.get("id") != [str(cmid)]
                        or report_query.get("action") != ["grading"]
                    ):
                        raise MoodleProtocolError(
                            "Moodle assignment grading navigation changed target"
                        )
                    try:
                        index = parse_assignment_grading_page(
                            report_html,
                            base_url=self.settings.base_url,
                            course_id=request.course_id,
                            cmid=cmid,
                            page_number=page_number,
                        )
                    except MoodleMarkupError as exc:
                        raise MoodleProtocolError(str(exc)) from exc

                index_items = prioritize_historical_attempts(
                    index.items,
                    pending_only=request.priority_only,
                )
                selected = index_items[offset : offset + request.limit]
                warnings: list[str] = []
                if index.skipped_rows:
                    warnings.append(
                        f"SKIPPED_UNIDENTIFIED_ROWS:{min(index.skipped_rows, 999_999)}"
                    )
                remaining_content_bytes = _HISTORICAL_CONTENT_BUDGET_BYTES
                public_items: list[dict[str, Any]] = []
                for raw_item in selected:
                    item = dict(raw_item)
                    detail_url = str(item.pop("_detail_url"))
                    if module == "quiz" and item.get("state") == "IN_PROGRESS":
                        # An active Quiz has no immutable review page yet.  Its
                        # exact remote attempt id is still valuable as a
                        # supersession marker, but opening attempt.php from a
                        # teacher context would neither prove a submission nor
                        # yield a safe historical response.
                        detail = self._omitted_historical_detail("ATTEMPT_IN_PROGRESS")
                        detail_warning = f"ATTEMPT_IN_PROGRESS:{item['attempt_id']}"
                    else:
                        detail, detail_warning = await self._historical_detail(
                            page,
                            context,
                            module=module,
                            course_id=request.course_id,
                            cmid=cmid,
                            item=item,
                            detail_url=detail_url,
                        )
                    if detail_warning:
                        warnings.append(detail_warning)

                    responses = list(detail.get("responses", []))
                    for response in responses:
                        answer = str(response.get("answer_text", ""))
                        answer_bytes = len(answer.encode("utf-8"))
                        if answer_bytes > remaining_content_bytes:
                            response["answer_text"] = ""
                            response["answer_complete"] = False
                            response["answer_omission_reason"] = "RESPONSE_BUDGET"
                            warnings.append(
                                f"ANSWER_OMITTED:{item['attempt_id']}:"
                                f"{response.get('response_id', '')}"
                            )
                        else:
                            remaining_content_bytes -= answer_bytes
                        for text_field in ("comment", "question_text"):
                            value = str(response.get(text_field, ""))
                            value_bytes = len(value.encode("utf-8"))
                            if value_bytes > remaining_content_bytes:
                                response[text_field] = ""
                                warnings.append(
                                    f"FIELD_OMITTED:{item['attempt_id']}:"
                                    f"{response.get('response_id', '')}:{text_field}"
                                )
                            else:
                                remaining_content_bytes -= value_bytes
                        links = list(response.pop("_artifact_links", []))
                        artifacts: list[dict[str, Any]] = []
                        for link in links:
                            maximum_source_bytes = min(
                                self.settings.artifact_max_bytes,
                                (remaining_content_bytes // 4) * 3,
                            )
                            artifact, consumed = await self._historical_artifact(
                                page,
                                link,
                                maximum_bytes=maximum_source_bytes,
                            )
                            remaining_content_bytes -= consumed
                            artifacts.append(artifact)
                            if not artifact["downloaded"]:
                                warnings.append(
                                    f"ARTIFACT_OMITTED:{item['attempt_id']}:"
                                    f"{artifact['external_id'][:12]}"
                                )
                        response["artifacts"] = artifacts
                    item["responses"] = responses
                    item["responses_complete"] = bool(detail.get("responses_complete", True))
                    if detail.get("grade") is not None:
                        item["grade"] = detail["grade"]
                    if detail.get("grade_max") is not None:
                        item["grade_max"] = detail["grade_max"]
                    if detail.get("comment"):
                        item["comment"] = detail["comment"]
                    elif module == "quiz":
                        response_comments = [
                            str(response.get("comment", "")).strip()
                            for response in responses
                            if str(response.get("comment", "")).strip()
                        ]
                        if response_comments:
                            item["comment"] = "\n\n".join(response_comments)[:20_000]
                    if item.get("grade") is None and module == "quiz":
                        grades = [
                            response.get("grade")
                            for response in responses
                            if response.get("grade") is not None
                        ]
                        maxima = [
                            response.get("grade_max")
                            for response in responses
                            if response.get("grade_max") is not None
                        ]
                        if grades:
                            item["grade"] = float(sum(grades))
                        if maxima:
                            item["grade_max"] = float(sum(maxima))
                    if item.get("grade") is not None:
                        item["state"] = "GRADED"
                    public_items.append(finalize_historical_submission(item))

                next_offset = offset + len(selected)
                if next_offset < len(index_items):
                    next_cursor = f"{page_number}:{next_offset}"
                elif index.has_next:
                    next_cursor = f"{page_number + 1}:0"
                else:
                    next_cursor = None
                state = await self._state(context)
                return HistoricalSubmissionsResponse.model_validate(
                    {
                        "course_id": request.course_id,
                        "activity": request.activity.model_dump(mode="json"),
                        "items": public_items,
                        "next_cursor": next_cursor,
                        "complete": next_cursor is None,
                        "warnings": list(dict.fromkeys(warnings))[:32],
                        "storage_state": state,
                    }
                )
            finally:
                await context.close()

    async def _historical_detail(
        self,
        page: Page,
        context: BrowserContext,
        *,
        module: str,
        course_id: str,
        cmid: int,
        item: dict[str, Any],
        detail_url: str,
    ) -> tuple[dict[str, Any], str | None]:
        """Read one attempt detail without sacrificing the rest of its report page.

        A report row remains useful evidence of a submitted or graded attempt even
        when Moodle transiently fails to render that attempt's review/grader page.
        Preserve the row and make the missing source explicit so the core creates
        a visible placeholder.  Authentication and identity/target validation are
        intentionally outside this recovery path and remain fail-closed.
        """

        attempt_id = str(item["attempt_id"])
        try:
            detail_html = await self._goto(page, detail_url)
        except BrowserUnavailable:
            return self._omitted_historical_detail("DETAIL_NAVIGATION_FAILED"), (
                f"DETAIL_NAVIGATION_FAILED:{attempt_id}"
            )

        # An expired session must invalidate the operation rather than turn every
        # response into an apparently harmless omission.
        await self._require_authenticated_page(context, detail_html)
        try:
            if module == "quiz":
                detail = await self._historical_quiz_review_detail(
                    page,
                    context,
                    detail_html,
                    page.url,
                    course_id=course_id,
                    cmid=cmid,
                    attempt_id=attempt_id,
                    user_id=str(item["user_id"]),
                )
            else:
                detail = parse_assignment_grader_page(
                    detail_html,
                    page.url,
                    base_url=self.settings.base_url,
                    course_id=course_id,
                    cmid=cmid,
                    user_id=str(item["user_id"]),
                )
                # Assignment online-text responses need the same browser DOM
                # treatment as Quiz Essay answers.  BeautifulSoup cannot
                # reproduce the whitespace semantics of Moodle's rendered
                # rich-text container, which used to collapse indentation in
                # imported source code.
                rendered_answer = await self._historical_assignment_inner_text(page)
                responses = detail.get("responses", [])
                if rendered_answer and responses:
                    responses[0]["answer_text"] = rendered_answer
        except MoodleMarkupError as exc:
            detail_error = str(exc).casefold()
            if "identifiers changed" in detail_error or "another user" in detail_error:
                raise MoodleProtocolError(str(exc)) from exc
            return self._omitted_historical_detail("DETAIL_MARKUP_UNSUPPORTED"), (
                f"DETAIL_MARKUP_UNSUPPORTED:{attempt_id}"
            )
        warning = None
        if module == "quiz" and detail.get("responses_complete") is False:
            warning = f"DETAIL_PAGINATION_INCOMPLETE:{attempt_id}"
        return detail, warning

    async def _historical_quiz_review_detail(
        self,
        page: Page,
        context: BrowserContext,
        initial_html: str,
        initial_url: str,
        *,
        course_id: str,
        cmid: int,
        attempt_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Collect every Essay question from one bounded Moodle Quiz review.

        Moodle may paginate a review by question.  Prefer its explicit
        ``showall=1`` link; otherwise traverse the origin-validated ``page=N``
        links.  Every visited document is parsed with the same attempt, course,
        activity and student invariants.  This prevents a navigation control
        from smuggling another attempt into the historical import.
        """

        async def parse_page(
            html: str, url: str
        ) -> tuple[list[dict[str, Any]], str | None, list[str]]:
            parsed = parse_quiz_review_page(
                html,
                url,
                base_url=self.settings.base_url,
                course_id=course_id,
                cmid=cmid,
                attempt_id=attempt_id,
                user_id=user_id,
                allow_empty=True,
            )
            # Match the proven behaviour of the previous connector: read the
            # browser-rendered ``innerText`` while the corresponding review
            # page is still active.  BeautifulSoup cannot reproduce CSS
            # whitespace semantics of Moodle editors.
            rendered_answers = await self._historical_quiz_inner_text(page)
            for response, rendered in zip(
                parsed.get("responses", []), rendered_answers, strict=False
            ):
                if rendered:
                    response["answer_text"] = rendered
            show_all, pages = quiz_review_navigation_urls(
                html,
                url,
                base_url=self.settings.base_url,
                cmid=cmid,
                attempt_id=attempt_id,
            )
            return list(parsed.get("responses", [])), show_all, pages

        def response_sort_key(entry: tuple[int, dict[str, Any]]) -> tuple[int, int]:
            order, response = entry
            response_id = str(response.get("response_id", ""))
            if response_id.isdigit():
                return 0, int(response_id)
            return 1, order

        collected: dict[str, tuple[int, dict[str, Any]]] = {}
        next_order = 0
        response_limit_hit = False

        def merge(responses: list[dict[str, Any]]) -> None:
            nonlocal next_order, response_limit_hit
            for response in responses:
                response_id = str(response.get("response_id", "")).strip()
                if not response_id:
                    continue
                existing = collected.get(response_id)
                if existing is None:
                    if len(collected) >= _HISTORICAL_QUIZ_REVIEW_MAX_RESPONSES:
                        response_limit_hit = True
                        continue
                    collected[response_id] = (next_order, response)
                    next_order += 1
                    continue
                # Pagination controls can repeat a boundary question.  Retain
                # the first stable identity while filling any evidence that a
                # theme omitted on one representation.
                target = existing[1]
                for field in (
                    "question_text",
                    "answer_text",
                    "comment",
                    "reviewer_name",
                    "grade",
                    "grade_max",
                ):
                    if target.get(field) in {None, ""} and response.get(field) not in {None, ""}:
                        target[field] = response[field]
                artifacts = list(target.get("_artifact_links", []))
                known = {str(link.get("external_id", "")) for link in artifacts}
                for link in response.get("_artifact_links", []):
                    if str(link.get("external_id", "")) not in known:
                        artifacts.append(link)
                target["_artifact_links"] = artifacts[:16]

        initial_responses, show_all_url, initial_pages = await parse_page(
            initial_html, initial_url
        )

        # The show-all representation is authoritative and avoids both extra
        # latency and accidental question loss when Moodle's paginator exposes
        # only neighbouring pages.  Keep evidence from the initial question
        # page, though: several Moodle themes render grades on ``showall=1``
        # but expose response attachments only on the per-question page.
        if show_all_url:
            try:
                show_all_html = await self._goto(page, show_all_url)
                await self._require_authenticated_page(context, show_all_html)
                show_all_responses, _ignored_show_all, _ignored_pages = await parse_page(
                    show_all_html, page.url
                )
                if show_all_responses:
                    merge(initial_responses)
                    merge(show_all_responses)
                    authoritative_ids = {
                        str(response.get("response_id", "")).strip()
                        for response in show_all_responses
                        if str(response.get("response_id", "")).strip()
                    }
                    ordered = [
                        response
                        for _, response in sorted(collected.values(), key=response_sort_key)
                        if str(response.get("response_id", "")).strip() in authoritative_ids
                    ]
                    return {
                        "responses": ordered,
                        "responses_complete": not response_limit_hit,
                    }
            except BrowserUnavailable:
                # Fall through to explicit page traversal using the links from
                # the already validated initial document.
                pass
            except MoodleMarkupError as exc:
                detail_error = str(exc).casefold()
                if "identifiers changed" in detail_error or "another user" in detail_error:
                    raise
                # A theme-specific show-all rendering must not discard the
                # validated per-page representation.
                pass

        merge(initial_responses)
        queue = list(initial_pages)
        current_query = parse_qs(urlsplit(initial_url).query, keep_blank_values=True)
        current_page = current_query.get("page", ["0"])[0]
        visited_pages = {int(current_page) if current_page.isdigit() else 0}
        complete = True

        while queue:
            target_url = queue.pop(0)
            target_query = parse_qs(urlsplit(target_url).query, keep_blank_values=True)
            raw_page = target_query.get("page", [""])[0]
            if not raw_page.isdigit():
                continue
            page_number = int(raw_page)
            if page_number in visited_pages:
                continue
            if len(visited_pages) >= _HISTORICAL_QUIZ_REVIEW_MAX_PAGES:
                complete = False
                break
            visited_pages.add(page_number)
            try:
                child_html = await self._goto(page, target_url)
            except BrowserUnavailable:
                complete = False
                continue
            await self._require_authenticated_page(context, child_html)
            try:
                responses, _show_all, discovered_pages = await parse_page(child_html, page.url)
            except MoodleMarkupError as exc:
                detail_error = str(exc).casefold()
                if "identifiers changed" in detail_error or "another user" in detail_error:
                    raise
                complete = False
                continue
            merge(responses)
            for discovered in discovered_pages:
                discovered_query = parse_qs(urlsplit(discovered).query, keep_blank_values=True)
                raw_discovered_page = discovered_query.get("page", [""])[0]
                if (
                    raw_discovered_page.isdigit()
                    and int(raw_discovered_page) not in visited_pages
                    and discovered not in queue
                ):
                    queue.append(discovered)

        if not collected:
            raise MoodleMarkupError("Moodle quiz review has no essay responses")
        ordered = [response for _, response in sorted(collected.values(), key=response_sort_key)]
        return {
            "responses": ordered,
            "responses_complete": complete and not response_limit_hit,
        }

    async def _historical_quiz_inner_text(self, page: Page) -> list[str]:
        """Read Essay code exactly as Chromium renders it, bounded and ordered."""

        if not hasattr(page, "locator"):
            return []
        try:
            questions = page.locator(".que.essay")
            count = min(await questions.count(), 100)
            result: list[str] = []
            for index in range(count):
                response = (
                    questions.nth(index)
                    .locator(".answer .qtype_essay_response, .qtype_essay_response")
                    .first
                )
                if await response.count() != 1:
                    result.append("")
                    continue
                value = await response.inner_text()
                value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
                # The old Selenium implementation trimmed only the boundaries;
                # indentation, tabs, blank lines and trailing spaces inside the
                # program remain untouched.
                result.append(_trim_empty_boundary_lines(value, maximum=1_000_000))
            return result
        except (PlaywrightTimeoutError, PlaywrightError):
            # BeautifulSoup parsing above remains a deterministic fallback for
            # older Moodle themes or transient DOM failures.
            return []

    async def _historical_assignment_inner_text(self, page: Page) -> str:
        """Read one Assignment online-text response as Chromium renders it."""

        if not hasattr(page, "locator"):
            return ""
        try:
            for selector in (
                ".assignsubmission_onlinetext",
                "[data-region='assignsubmission_onlinetext']",
                ".onlinetextsubmission",
            ):
                response = page.locator(selector).first
                if await response.count() != 1:
                    continue
                value = await response.inner_text()
                value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
                return _trim_empty_boundary_lines(value, maximum=1_000_000)
            return ""
        except (PlaywrightTimeoutError, PlaywrightError):
            # The parser output remains a bounded deterministic fallback when
            # the current Moodle theme does not expose a stable DOM container.
            return ""

    @staticmethod
    def _omitted_historical_detail(reason: str) -> dict[str, Any]:
        return {
            "responses_complete": False,
            "responses": [
                {
                    "response_id": "moodle-detail",
                    "question_text": "",
                    "answer_text": "",
                    "answer_complete": False,
                    "answer_omission_reason": reason,
                    "grade": None,
                    "grade_max": None,
                    "comment": "",
                    "artifacts": [],
                }
            ],
        }

    async def _historical_artifact(
        self,
        page: Page,
        link: dict[str, str],
        *,
        maximum_bytes: int,
    ) -> tuple[dict[str, Any], int]:
        base = {
            "external_id": link["external_id"],
            "filename": link["filename"],
            "mime_type": "",
            "size_bytes": 0,
            "sha256": "",
            "content_base64": "",
        }
        if maximum_bytes <= 0:
            return {**base, "downloaded": False, "omission_reason": "RESPONSE_BUDGET"}, 0
        result: dict[str, Any] | None = None

        # This mirrors the proven behaviour of the former Selenium connector:
        # download attachments through an HTTP client carrying the browser
        # session cookies.  A page-level ``fetch`` can be blocked by Moodle's
        # CSP or by a forced-download response even when the same link opens
        # normally in the authenticated browser.
        context = getattr(page, "context", None)
        request = getattr(context, "request", None)
        target = urlsplit(link["url"])
        base_origin = urlsplit(self.settings.base_url)
        if (
            request is not None
            and target.scheme == base_origin.scheme
            and target.netloc == base_origin.netloc
            and target.path.startswith("/pluginfile.php/")
        ):
            response = None
            try:
                response = await request.get(link["url"], timeout=10_000)
                final = urlsplit(str(response.url))
                if (
                    bool(response.ok)
                    and final.scheme == base_origin.scheme
                    and final.netloc == base_origin.netloc
                    and final.path.startswith("/pluginfile.php/")
                ):
                    headers = {
                        str(key).casefold(): str(value)
                        for key, value in dict(response.headers).items()
                    }
                    raw_length = headers.get("content-length", "")
                    declared_length = int(raw_length) if raw_length.isdigit() else None
                    if declared_length is not None and declared_length > maximum_bytes:
                        result = {
                            "status": int(response.status),
                            "tooLarge": True,
                            "size": declared_length,
                        }
                    else:
                        content = await response.body()
                        if len(content) > maximum_bytes:
                            result = {
                                "status": int(response.status),
                                "tooLarge": True,
                                "size": len(content),
                            }
                        else:
                            result = {
                                "status": int(response.status),
                                "size": len(content),
                                "contentType": headers.get("content-type", ""),
                                "contentBase64": base64.b64encode(content).decode("ascii"),
                            }
            except (PlaywrightTimeoutError, PlaywrightError, ValueError, TypeError):
                result = None
            finally:
                if response is not None:
                    with suppress(PlaywrightError):
                        await response.dispose()

        # Keep the bounded streaming implementation as a compatibility
        # fallback for test doubles and older Playwright builds.
        if result is None:
            try:
                result = await page.evaluate(
                    _HISTORICAL_BINARY_FETCH_SCRIPT,
                    {"url": link["url"], "maxBytes": maximum_bytes},
                )
            except (PlaywrightTimeoutError, PlaywrightError, AttributeError):
                result = None
        if not isinstance(result, dict):
            return {**base, "downloaded": False, "omission_reason": "DOWNLOAD_FAILED"}, 0
        if result.get("tooLarge"):
            declared = result.get("size")
            size = (
                int(declared)
                if isinstance(declared, int) and 0 <= declared <= 4 * 1024 * 1024
                else 0
            )
            return {
                **base,
                "size_bytes": size,
                "downloaded": False,
                "omission_reason": "FILE_TOO_LARGE",
            }, 0
        encoded = result.get("contentBase64")
        size = result.get("size")
        if (
            not isinstance(encoded, str)
            or not isinstance(size, int)
            or not 0 <= size <= maximum_bytes
        ):
            return {**base, "downloaded": False, "omission_reason": "DOWNLOAD_FAILED"}, 0
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return {**base, "downloaded": False, "omission_reason": "DOWNLOAD_FAILED"}, 0
        if len(content) != size:
            return {**base, "downloaded": False, "omission_reason": "DOWNLOAD_FAILED"}, 0
        return {
            **base,
            "mime_type": str(result.get("contentType", ""))[:255],
            "size_bytes": size,
            "sha256": hashlib.sha256(content).hexdigest(),
            "content_base64": encoded,
            "downloaded": True,
            "omission_reason": "",
        }, len(encoded)

    async def _crawl_course_section_pages(
        self,
        page: Page,
        context: BrowserContext,
        course_id: str,
        main_html: str,
        course: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            links = parse_course_section_links(
                main_html,
                self.settings.base_url,
                course_id,
                maximum=self.settings.max_course_section_pages,
            )
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc
        section_pages: list[dict[str, Any]] = []
        for link in links:
            html = await self._goto(page, link.url)
            await self._require_authenticated_page(context, html)
            parsed_url = urlsplit(page.url)
            query = parse_qs(parsed_url.query, keep_blank_values=True)
            expected_url = urlsplit(link.url)
            expected_query = parse_qs(expected_url.query, keep_blank_values=True)
            if parsed_url.path.rstrip("/") != expected_url.path.rstrip("/") or any(
                query.get(key) != value for key, value in expected_query.items()
            ):
                raise MoodleProtocolError("Moodle course section navigation changed target")
            try:
                section_pages.append(
                    parse_course_page(
                        html,
                        self.settings.base_url,
                        course_id,
                        expected_section_record_id=link.section_record_id,
                    )
                )
            except MoodleMarkupError as exc:
                raise MoodleProtocolError(str(exc)) from exc
        try:
            return merge_course_sections(course, section_pages)
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc

    async def _enrich_course_activities(
        self,
        page: Page,
        context: BrowserContext,
        course_id: str,
        course: dict[str, Any],
        *,
        participant_ids: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        """Fetch bounded, read-only settings for supported Moodle activities.

        A failed detail request does not erase the activity found on the course
        page.  Both activity types fail closed for student publication until
        the connector proves a supported answer transport.  At present that is
        a Quiz containing exactly one Essay question; Assignment discovery is
        read-only until its student submission form is captured and verified.
        """

        # Kept as a compatibility-only argument for callers deployed before
        # per-user override discovery was removed.
        del participant_ids
        result = dict(course)
        sections: list[dict[str, Any]] = []
        detail_count = 0
        try:
            async with asyncio.timeout(self.settings.activity_detail_budget_seconds):
                for raw_section in course.get("sections", []):
                    section = dict(raw_section)
                    activities: list[dict[str, Any]] = []
                    for raw_activity in raw_section.get("activities", []):
                        activity = dict(raw_activity)
                        module = str(activity.get("module", ""))
                        cmid = int(activity.get("cmid", 0) or 0)
                        activity["import_supported"] = False
                        activity["title_confirmed"] = bool(str(activity.get("name", "")).strip())
                        if (
                            not _activity_needs_assessment_detail(
                                activity,
                                str(section.get("title", "")),
                            )
                            or cmid <= 0
                            or detail_count >= _ACTIVITY_DETAIL_MAX_PAGES
                        ):
                            activities.append(activity)
                            continue
                        detail_count += 1
                        settings_url = f"{self.settings.base_url}/course/modedit.php?" + urlencode(
                            {"update": cmid, "return": 1}
                        )
                        settings_html = await self._fetch_bounded_html(
                            page,
                            context,
                            settings_url,
                            expected_path="/course/modedit.php",
                            expected_query={"update": [str(cmid)]},
                        )
                        if settings_html is not None:
                            with suppress(MoodleMarkupError):
                                activity.update(
                                    parse_activity_settings(
                                        settings_html,
                                        course_id=course_id,
                                        cmid=cmid,
                                        module=module,
                                    )
                                )
                        if module == "quiz" and detail_count < _ACTIVITY_DETAIL_MAX_PAGES:
                            detail_count += 1
                            edit_url = f"{self.settings.base_url}/mod/quiz/edit.php?" + urlencode(
                                {"cmid": cmid}
                            )
                            edit_html = await self._fetch_bounded_html(
                                page,
                                context,
                                edit_url,
                                expected_path="/mod/quiz/edit.php",
                                expected_query={"cmid": [str(cmid)]},
                            )
                            if edit_html is not None:
                                with suppress(MoodleMarkupError):
                                    summary = parse_quiz_question_summary(
                                        edit_html,
                                        course_id=course_id,
                                        cmid=cmid,
                                        base_url=self.settings.base_url,
                                    )
                                    question_edit_url = str(summary.pop("_essay_edit_url", ""))
                                    question_id = str(summary.pop("_essay_question_id", ""))
                                    question_query_name = str(
                                        summary.pop("_essay_question_query_name", "")
                                    )
                                    random_qbank_url = str(summary.pop("_random_qbank_url", ""))
                                    # A single Essay slot proves only structure.
                                    # Publication remains closed until the
                                    # question editor proves the concrete Moodle
                                    # response transport for this question.
                                    summary["import_supported"] = False
                                    activity.update(summary)
                                    if (
                                        question_edit_url
                                        and question_id
                                        and question_query_name in {"id", "questionid"}
                                        and detail_count < _ACTIVITY_DETAIL_MAX_PAGES
                                    ):
                                        detail_count += 1
                                        parsed_question_url = urlsplit(question_edit_url)
                                        question_html = await self._fetch_bounded_html(
                                            page,
                                            context,
                                            question_edit_url,
                                            expected_path=parsed_question_url.path.rstrip("/"),
                                            expected_query={
                                                "cmid": [str(cmid)],
                                                question_query_name: [question_id],
                                            },
                                        )
                                        if question_html is not None:
                                            statement = parse_quiz_essay_question_edit(
                                                question_html,
                                                course_id=course_id,
                                                cmid=cmid,
                                                question_id=question_id,
                                            )
                                            if statement["description"]:
                                                activity.update(statement)
                                            transport = statement.get("answer_transport")
                                            if transport in {
                                                "ESSAY_ONLINE_TEXT",
                                                "ESSAY_ATTACHMENT",
                                            }:
                                                activity["answer_transport"] = transport
                                                activity["import_supported"] = True
                                    if (
                                        random_qbank_url
                                        and summary.get("question_count") == 1
                                        and summary.get("random_question_count") == 1
                                        and detail_count < _ACTIVITY_DETAIL_MAX_PAGES
                                    ):
                                        detail_count += 1
                                        parsed_qbank_url = urlsplit(random_qbank_url)
                                        qbank_query = parse_qs(
                                            parsed_qbank_url.query,
                                            keep_blank_values=True,
                                        )
                                        expected_qbank_query = {
                                            key: qbank_query[key]
                                            for key in ("cmid", "filter")
                                            if key in qbank_query
                                        }
                                        qbank_html = await self._fetch_bounded_html(
                                            page,
                                            context,
                                            random_qbank_url,
                                            expected_path="/question/edit.php",
                                            expected_query=expected_qbank_query,
                                        )
                                        if qbank_html is not None:
                                            with suppress(MoodleMarkupError):
                                                bank = parse_quiz_random_question_bank(
                                                    qbank_html,
                                                    base_url=self.settings.base_url,
                                                    course_id=course_id,
                                                )
                                                if (
                                                    bank["complete"]
                                                    and bank["question_count"] > 0
                                                    and bank["all_essay"]
                                                ):
                                                    activity.update(
                                                        {
                                                            "random_essay_confirmed": True,
                                                            "statement_deferred": True,
                                                            "import_supported": True,
                                                        }
                                                    )
                        activities.append(activity)
                    section["activities"] = activities
                    sections.append(section)
        except TimeoutError:
            # Keep all course-page activities even when the optional detail
            # budget is exhausted.  The backend will materialize only safe
            # configuration-required drafts.
            seen = {str(item.get("external_id", "")) for item in sections}
            sections.extend(
                dict(item)
                for item in course.get("sections", [])
                if str(item.get("external_id", "")) not in seen
            )
        result["sections"] = sections
        return result

    async def _activity_user_overrides(
        self,
        page: Page,
        context: BrowserContext,
        *,
        course_id: str,
        module: str,
        cmid: int,
        participant_ids: frozenset[str],
        detail_count: int,
    ) -> tuple[list[dict[str, Any]], bool, int]:
        """Read one bounded, complete set of user overrides for an activity."""

        if detail_count >= _ACTIVITY_DETAIL_MAX_PAGES:
            return [], False, detail_count
        detail_count += 1
        index_url = f"{self.settings.base_url}/mod/{module}/overrides.php?" + urlencode(
            {"cmid": cmid, "mode": "user"}
        )
        index_html = await self._fetch_bounded_html(
            page,
            context,
            index_url,
            expected_path=f"/mod/{module}/overrides.php",
            expected_query={"cmid": [str(cmid)], "mode": ["user"]},
        )
        if index_html is None:
            return [], False, detail_count
        try:
            references = parse_activity_user_override_index(
                index_html,
                base_url=self.settings.base_url,
                course_id=course_id,
                cmid=cmid,
                module=module,
            )
        except MoodleMarkupError:
            return [], False, detail_count
        references = [
            reference
            for reference in references
            if str(reference.get("user_id", "")) in participant_ids
        ]
        if detail_count + len(references) > _ACTIVITY_DETAIL_MAX_PAGES:
            return [], False, detail_count
        overrides: list[dict[str, Any]] = []
        for reference in references:
            detail_count += 1
            override_id = int(reference["override_id"])
            edit_url = str(reference["edit_url"])
            edit_html = await self._fetch_bounded_html(
                page,
                context,
                edit_url,
                expected_path=f"/mod/{module}/overrideedit.php",
                expected_query={"id": [str(override_id)]},
            )
            if edit_html is None:
                return [], False, detail_count
            try:
                overrides.append(
                    parse_activity_user_override_edit(
                        edit_html,
                        base_url=self.settings.base_url,
                        course_id=course_id,
                        cmid=cmid,
                        module=module,
                        override_id=override_id,
                        user_id=str(reference["user_id"]),
                        display_name=str(reference["display_name"]),
                    )
                )
            except MoodleMarkupError:
                return [], False, detail_count
        return overrides, True, detail_count

    async def _fetch_bounded_html(
        self,
        page: Page,
        context: BrowserContext,
        url: str,
        *,
        expected_path: str,
        expected_query: dict[str, list[str]],
    ) -> str | None:
        try:
            response = await page.evaluate(
                _LOGIN_ROLE_FETCH_SCRIPT,
                {
                    "url": url,
                    "timeoutMs": _ACTIVITY_DETAIL_FETCH_TIMEOUT_MS,
                    "maxBytes": _LOGIN_ROLE_PAGE_MAX_BYTES,
                },
            )
        except (PlaywrightTimeoutError, PlaywrightError):
            return None
        if not isinstance(response, dict) or response.get("status") != 200:
            return None
        html = response.get("html")
        response_url = response.get("url")
        if not isinstance(html, str) or not isinstance(response_url, str):
            return None
        parsed = urlsplit(response_url)
        try:
            origin = exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
        except ValueError:
            return None
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (
            origin != self.settings.base_url
            or parsed.path.rstrip("/") != expected_path
            or any(query.get(key) != value for key, value in expected_query.items())
            or not has_authenticated_markup(html, self.settings.base_url)
        ):
            return None
        authenticated, _ = await self._authenticated(context, html)
        return html if authenticated else None

    async def _participants(
        self,
        page: Page,
        context: BrowserContext,
        course_id: str,
    ) -> tuple[list[dict[str, Any]], bool]:
        members_by_id: dict[str, dict[str, Any]] = {}
        table_seen = False
        complete = False
        all_pages_classified = True
        for page_number in range(self.settings.max_participant_pages):
            query = urlencode(
                {
                    "id": course_id,
                    "perpage": self.settings.participants_per_page,
                    "page": page_number,
                }
            )
            html = await self._goto(
                page,
                f"{self.settings.base_url}/user/index.php?{query}",
            )
            authenticated, _ = await self._authenticated(context, html)
            if not authenticated:
                raise MoodleSessionExpired("Moodle browser session expired")
            parsed = parse_participants_page(html, self.settings.base_url, course_id)
            if not parsed.table_present:
                return list(members_by_id.values()), False
            table_seen = True
            all_pages_classified = all_pages_classified and parsed.all_rows_classified
            for member in parsed.members:
                members_by_id[member["user_id"]] = member
                if len(members_by_id) > self.settings.max_participants:
                    raise MoodleProtocolError(
                        "Moodle participant list exceeds the configured limit"
                    )
            if not parsed.has_next:
                complete = all_pages_classified
                break
        return list(members_by_id.values()), bool(table_seen and complete)

    async def grade_assignment(self, request: GradeRequest) -> GradeResponse:
        self._require_origin(request.base_url)
        try:
            state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(state):
            raise MoodleSessionExpired("Moodle browser session is missing")
        async with self._operation() as browser:
            context = await self._new_context(browser, storage_state=state)
            try:
                page = await context.new_page()
                quiz_receipt: dict[str, float] = {}
                if request.payload.module == "quiz":
                    target_path, question_max, submitted_mark = await self._grade_quiz_essay(
                        page, context, request
                    )
                    quiz_receipt = {
                        "grade_scale_max": float(request.payload.grade_scale_max or 0),
                        "quiz_overall_grade_max": float(
                            request.payload.quiz_overall_grade_max or 0
                        ),
                        "question_max": float(question_max),
                        "submitted_mark": float(submitted_mark),
                    }
                else:
                    target_path = await self._grade_assignment_submission(page, context, request)
                refreshed_state = await self._state(context)
            finally:
                await context.close()
        return GradeResponse.model_validate(
            {
                "status": "DELIVERED",
                "receipt": {
                    "module": request.payload.module,
                    "course_id": request.payload.course_id,
                    "cmid": request.payload.cmid,
                    "user_id": request.payload.user_id,
                    "attempt_id": request.payload.attempt_id,
                    "question_slot": request.payload.question_slot,
                    "attempt_number": request.payload.attempt_number,
                    "grade": request.payload.grade,
                    "comment_sha256": hashlib.sha256(
                        request.payload.comment.encode("utf-8")
                    ).hexdigest(),
                    "idempotency_key": request.idempotency_key,
                    "target_path": target_path,
                    **quiz_receipt,
                },
                "storage_state": refreshed_state,
            }
        )

    async def _require_course_context(self, page: Page, course_id: str) -> None:
        hrefs = await page.locator("a[href*='/course/view.php']").evaluate_all(
            "elements => elements.map(element => element.href)"
        )
        for href in hrefs:
            if not isinstance(href, str):
                continue
            parsed = urlsplit(href)
            if parsed.path.rstrip("/") == "/course/view.php" and parse_qs(
                parsed.query, keep_blank_values=True
            ).get("id") == [course_id]:
                return
        raise MoodleProtocolError("Moodle grading form has another course context")

    async def _one_visible(self, page: Page, selector: str, label: str) -> Locator:
        candidates = page.locator(selector)
        visible: list[Locator] = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if await candidate.is_visible():
                visible.append(candidate)
        if len(visible) != 1:
            raise MoodleProtocolError(f"Moodle {label} control is ambiguous")
        return visible[0]

    async def _require_hidden_value(self, page: Page, name: str, expected: str) -> None:
        control = page.locator(f"input[name='{name}']")
        if await control.count() != 1 or await control.get_attribute("value") != expected:
            raise MoodleProtocolError("Moodle grading form identifiers changed")

    async def _set_editor(self, page: Page, textarea_selector: str, value: str) -> None:
        textarea = page.locator(textarea_selector)
        if await textarea.count() != 1:
            raise MoodleProtocolError("Moodle grading comment control is ambiguous")
        paragraphs = value.splitlines() or [""]
        html_value = "".join(
            f"<p>{html.escape(line) if line else '<br>'}</p>" for line in paragraphs
        )
        await textarea.evaluate(
            """(element, value) => {
                element.value = value;
                for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                    element.dispatchEvent(new Event(eventName, {bubbles: true}));
                }
            }""",
            html_value,
        )
        editor = page.locator("div.editor_atto_content[contenteditable='true']")
        if await editor.count() == 1:
            await editor.evaluate(
                """(element, value) => {
                    element.innerText = value;
                    for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                        element.dispatchEvent(new Event(eventName, {bubbles: true}));
                    }
                }""",
                value,
            )

    async def _set_grade(self, control: Locator, grade: float | Decimal) -> None:
        try:
            decimal_grade = Decimal(str(grade))
        except InvalidOperation as exc:
            raise MoodleContractError("Moodle grade is not a finite decimal") from exc
        if not decimal_grade.is_finite() or decimal_grade < 0:
            raise MoodleContractError("Moodle grade is not a finite decimal")
        value = format(decimal_grade, "f")
        if "." in value:
            value = value.rstrip("0").rstrip(".")
        value = value or "0"
        tag = (await control.evaluate("element => element.tagName")).lower()
        if tag == "select":
            try:
                await control.select_option(value=value)
            except PlaywrightError as exc:
                raise MoodleProtocolError("Moodle grade scale does not contain the value") from exc
        else:
            await control.fill(value)

    async def _quiz_question_mark_max(self, control: Locator) -> Decimal:
        try:
            context_text = await control.evaluate(
                """element => {
                    const root = element.closest(
                        '.fitem, .form-group, .mb-3, [data-region="grade"]'
                    ) || element.parentElement;
                    return root ? root.innerText : '';
                }"""
            )
        except PlaywrightError as exc:
            raise MoodleProtocolError("Moodle quiz mark scale could not be inspected") from exc
        if not isinstance(context_text, str):
            raise MoodleProtocolError("Moodle quiz mark scale is unavailable")
        values = {
            Decimal(match.group(1).replace(",", "."))
            for match in _QUESTION_MARK_MAX.finditer(context_text[:4_000])
        }
        values = {
            value
            for value in values
            if value.is_finite() and Decimal(0) < value <= Decimal(1_000_000)
        }
        if len(values) != 1:
            raise MoodleProtocolError("Moodle quiz mark scale is ambiguous")
        return values.pop()

    async def _grade_assignment_submission(
        self,
        page: Page,
        context: BrowserContext,
        request: GradeRequest,
    ) -> str:
        query: dict[str, str | int] = {
            "id": request.payload.cmid,
            "action": "grader",
            "userid": request.payload.user_id,
        }
        if request.payload.attempt_number is not None:
            query["attemptnumber"] = request.payload.attempt_number
        target = f"{self.settings.base_url}/mod/assign/view.php?{urlencode(query)}"
        markup = await self._goto(page, target)
        await self._require_authenticated_page(context, markup)
        parsed = urlsplit(page.url)
        actual = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.path.rstrip("/") != "/mod/assign/view.php"
            or actual.get("id") != [str(request.payload.cmid)]
            or actual.get("action", [""])[0] not in {"grader", "grade"}
            or actual.get("userid") != [request.payload.user_id]
        ):
            raise MoodleProtocolError("Moodle assignment grader changed target")
        await self._require_course_context(page, request.payload.course_id)
        await self._require_hidden_value(page, "userid", request.payload.user_id)
        grade = await self._one_visible(
            page,
            "input[name='grade'], select[name='grade']",
            "assignment grade",
        )
        await self._set_grade(grade, request.payload.grade)
        await self._set_editor(
            page,
            "textarea[name='assignfeedbackcomments_editor[text]']",
            request.payload.comment,
        )
        await self._require_live_assignment_grade_preconditions(context, request)
        submit = await self._one_visible(
            page,
            "#id_savegrade, button[name='savegrade'], input[name='savegrade'], #id_submitbutton",
            "assignment save",
        )
        await submit.click()
        saved_markup = await page.content()
        await self._require_authenticated_page(context, saved_markup)
        if urlsplit(page.url).path.rstrip("/") != "/mod/assign/view.php":
            raise MoodleProtocolError("Moodle assignment save changed target")
        return "/mod/assign/view.php"

    async def _require_live_assignment_grade_preconditions(
        self,
        context: BrowserContext,
        request: GradeRequest,
    ) -> None:
        """Prove the Assignment target is the student's latest terminal attempt."""

        guard_page = await context.new_page()
        try:
            matches: list[dict[str, Any]] = []
            completed = False
            for page_number in range(self.settings.max_participant_pages):
                target = f"{self.settings.base_url}/mod/assign/view.php?" + urlencode(
                    {
                        "id": request.payload.cmid,
                        "action": "grading",
                        "page": page_number,
                        "perpage": self.settings.participants_per_page,
                    }
                )
                markup = await self._goto(guard_page, target)
                await self._require_authenticated_page(context, markup)
                parsed = urlsplit(guard_page.url)
                query = parse_qs(parsed.query, keep_blank_values=True)
                if (
                    parsed.path.rstrip("/") != "/mod/assign/view.php"
                    or query.get("id") != [str(request.payload.cmid)]
                    or query.get("action") != ["grading"]
                    or query.get("page", ["0"]) != [str(page_number)]
                ):
                    raise MoodleProtocolError("Moodle assignment report changed target")
                try:
                    index = parse_assignment_grading_page(
                        markup,
                        base_url=self.settings.base_url,
                        course_id=request.payload.course_id,
                        cmid=request.payload.cmid,
                        page_number=page_number,
                    )
                except MoodleMarkupError as exc:
                    raise MoodleProtocolError(str(exc)) from exc
                matches.extend(
                    item
                    for item in index.items
                    if str(item.get("user_id")) == request.payload.user_id
                )
                if not index.has_next:
                    completed = True
                    break
            if not completed or len(matches) != 1:
                raise MoodleProtocolError("Moodle assignment attempt history is incomplete")
            latest = matches[0]
            attempt_id = str(latest.get("attempt_id", ""))
            suffix = f"user-{request.payload.user_id}-attempt-"
            if not attempt_id.startswith(suffix):
                raise MoodleProtocolError("Moodle assignment attempt identity changed")
            raw_number = attempt_id.removeprefix(suffix)
            if not raw_number.isdigit():
                raise MoodleProtocolError("Moodle assignment attempt identity changed")
            if (
                request.payload.attempt_number is not None
                and int(raw_number) != request.payload.attempt_number
            ):
                raise MoodleProtocolError(
                    "Moodle assignment grade target is not the latest attempt"
                )
            if str(latest.get("state")) not in {"SUBMITTED", "GRADED"}:
                raise MoodleProtocolError("Moodle assignment grade target is not finalized")
        finally:
            await guard_page.close()

    async def _grade_quiz_essay(
        self,
        page: Page,
        context: BrowserContext,
        request: GradeRequest,
    ) -> tuple[str, Decimal, Decimal]:
        assert request.payload.attempt_id is not None
        assert request.payload.question_slot is not None
        target = f"{self.settings.base_url}/mod/quiz/comment.php?" + urlencode(
            {
                "attempt": request.payload.attempt_id,
                "slot": request.payload.question_slot,
            }
        )
        markup = await self._goto(page, target)
        await self._require_session_without_global_navigation(context, markup, page.url)
        parsed = urlsplit(page.url)
        actual = parse_qs(parsed.query, keep_blank_values=True)
        if (
            parsed.path.rstrip("/")
            not in {"/mod/quiz/comment.php", "/mod/quiz/reviewquestion.php"}
            or actual.get("attempt") != [request.payload.attempt_id]
            or actual.get("slot") != [str(request.payload.question_slot)]
        ):
            raise MoodleProtocolError("Moodle quiz grading form changed target")
        # Moodle 5.2 renders this standalone manual-grading endpoint without
        # global navigation or a course link.  The exact hidden attempt/slot
        # below bind the form, while the fresh settings and overview checks in
        # _require_live_quiz_grade_preconditions prove course, activity, user,
        # latest-attempt state and grading policy immediately before Save.
        await self._require_hidden_value(page, "attempt", request.payload.attempt_id)
        await self._require_hidden_value(page, "slot", str(request.payload.question_slot))
        form = page.locator("form#manualgradingform")
        if await form.count() != 1:
            raise MoodleProtocolError("Moodle manual grading form is ambiguous")
        grade = await self._one_visible(page, "input[name$='-mark']", "quiz mark")
        question_max = await self._quiz_question_mark_max(grade)
        assert request.payload.grade_scale_max is not None
        local_grade = Decimal(str(request.payload.grade))
        local_scale = Decimal(str(request.payload.grade_scale_max))
        submitted_mark = (local_grade / local_scale * question_max).quantize(
            _QUIZ_MARK_QUANTUM,
            rounding=ROUND_HALF_EVEN,
        )
        round_trip = submitted_mark / question_max * local_scale
        tolerance = max(Decimal("0.000000001"), local_scale * Decimal("0.000000001"))
        if abs(round_trip - local_grade) > tolerance:
            raise MoodleContractError("Moodle quiz mark cannot preserve the local grade")
        await self._set_grade(grade, submitted_mark)
        await self._set_editor(page, "textarea[name$='-comment']", request.payload.comment)
        await self._require_live_quiz_grade_preconditions(context, request)
        submit = await self._one_visible(
            page,
            "form#manualgradingform #id_submitbutton",
            "quiz save",
        )
        await submit.click()
        saved_markup = await page.content()
        await self._require_session_without_global_navigation(context, saved_markup, page.url)
        saved = urlsplit(page.url)
        saved_query = parse_qs(saved.query, keep_blank_values=True)
        if saved.path.rstrip("/") not in {
            "/mod/quiz/comment.php",
            "/mod/quiz/reviewquestion.php",
            "/mod/quiz/review.php",
        }:
            raise MoodleProtocolError("Moodle quiz save changed target")
        if saved.path.rstrip("/") in {
            "/mod/quiz/comment.php",
            "/mod/quiz/reviewquestion.php",
        }:
            saved_attempt = saved_query.get("attempt")
            saved_slot = saved_query.get("slot")
            # Moodle 5.2 posts the manual-grading form to comment.php without
            # a query string.  In that response the exact identifiers remain
            # in the single canonical form's hidden controls, which are
            # checked below.  Reject partial or conflicting query evidence.
            identifiers_absent = saved_attempt is None and saved_slot is None
            identifiers_exact = saved_attempt == [request.payload.attempt_id] and saved_slot == [
                str(request.payload.question_slot)
            ]
            if not identifiers_absent and not identifiers_exact:
                raise MoodleProtocolError("Moodle quiz save identifiers changed")
            await self._require_hidden_value(page, "attempt", request.payload.attempt_id)
            await self._require_hidden_value(page, "slot", str(request.payload.question_slot))
            if await page.locator("form#manualgradingform").count() != 1:
                raise MoodleProtocolError("Moodle manual grading save form is ambiguous")
        else:
            if saved_query.get("attempt") != [request.payload.attempt_id] or saved_query.get(
                "cmid"
            ) not in (None, [str(request.payload.cmid)]):
                raise MoodleProtocolError("Moodle quiz save identifiers changed")
        return "/mod/quiz/comment.php", question_max, submitted_mark

    async def _require_live_quiz_grade_preconditions(
        self,
        context: BrowserContext,
        request: GradeRequest,
    ) -> None:
        """Re-read mutable Moodle policy and attempt order immediately before save."""

        guard_page = await context.new_page()
        try:
            await self._require_live_quiz_last_attempt_grading(guard_page, context, request)
            await self._require_latest_terminal_quiz_attempt(guard_page, context, request)
        finally:
            await guard_page.close()

    async def _require_latest_terminal_quiz_attempt(
        self,
        page: Page,
        context: BrowserContext,
        request: GradeRequest,
    ) -> None:
        """Prove the selected Quiz attempt is this student's latest terminal one."""

        assert request.payload.attempt_id is not None
        attempts: dict[str, str] = {}
        completed = False
        for page_number in range(self.settings.max_participant_pages):
            target = f"{self.settings.base_url}/mod/quiz/report.php?" + urlencode(
                {
                    "id": request.payload.cmid,
                    "mode": "overview",
                    "attempts": "enrolled_with",
                    "onlyregraded": 0,
                    "slotmarks": 1,
                    "group": 0,
                    "page": page_number,
                }
            )
            markup = await self._goto(page, target)
            await self._require_authenticated_page(context, markup)
            parsed = urlsplit(page.url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            if (
                parsed.path.rstrip("/") != "/mod/quiz/report.php"
                or query.get("id") != [str(request.payload.cmid)]
                or query.get("mode") != ["overview"]
                or query.get("page", ["0"]) != [str(page_number)]
            ):
                raise MoodleProtocolError("Moodle quiz report changed target")
            try:
                index = parse_quiz_report_page(
                    markup,
                    base_url=self.settings.base_url,
                    course_id=request.payload.course_id,
                    cmid=request.payload.cmid,
                    page_number=page_number,
                )
            except MoodleMarkupError as exc:
                raise MoodleProtocolError(str(exc)) from exc
            if index.skipped_rows:
                raise MoodleProtocolError("Moodle quiz report contains unidentified attempts")
            for item in index.items:
                if str(item.get("user_id")) != request.payload.user_id:
                    continue
                attempt_id = str(item.get("attempt_id", ""))
                state = str(item.get("state", ""))
                existing = attempts.setdefault(attempt_id, state)
                if existing != state:
                    raise MoodleProtocolError("Moodle quiz attempt state is inconsistent")
            if not index.has_next:
                completed = True
                break
        if not completed or not attempts:
            raise MoodleProtocolError("Moodle quiz attempt history is incomplete")
        latest_attempt_id = max(attempts, key=int)
        if latest_attempt_id != request.payload.attempt_id:
            raise MoodleProtocolError("Moodle quiz grade target is not the latest attempt")
        if attempts[latest_attempt_id] not in {"SUBMITTED", "GRADED"}:
            raise MoodleProtocolError("Moodle quiz grade target is not finalized")

    async def _require_live_quiz_last_attempt_grading(
        self,
        page: Page,
        context: BrowserContext,
        request: GradeRequest,
    ) -> None:
        """Fail closed if Moodle no longer grades this Quiz by its last attempt."""

        target = f"{self.settings.base_url}/course/modedit.php?" + urlencode(
            {"update": request.payload.cmid, "return": 1}
        )
        markup = await self._goto(page, target)
        await self._require_authenticated_page(context, markup)
        parsed = urlsplit(page.url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path.rstrip("/") != "/course/modedit.php" or query.get("update") != [
            str(request.payload.cmid)
        ]:
            raise MoodleProtocolError("Moodle quiz settings changed target")
        try:
            activity = parse_activity_settings(
                markup,
                course_id=request.payload.course_id,
                cmid=request.payload.cmid,
                module="quiz",
            )
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc
        if (
            activity.get("quiz_grading_method_confirmed") is not True
            or activity.get("quiz_grading_method") != "LAST"
        ):
            raise MoodleProtocolError(
                "Moodle quiz must use the latest attempt for its final grade"
            )
        live_grade_max = activity.get("grade_max")
        if (
            activity.get("grade_confirmed") is not True
            or isinstance(live_grade_max, bool)
            or not isinstance(live_grade_max, int | float)
            or not Decimal(str(live_grade_max)).is_finite()
            or Decimal(str(live_grade_max)) <= 0
            or request.payload.quiz_overall_grade_max is None
            or Decimal(str(live_grade_max)) != Decimal(str(request.payload.quiz_overall_grade_max))
        ):
            raise MoodleProtocolError("Moodle quiz overall grade scale changed")

    async def prepare_assignment_submission(
        self,
        request: AssignmentSubmissionPrepareRequest,
    ) -> AssignmentSubmissionPrepareResponse:
        """Prove that the current student has a writable Assignment form."""

        self._require_origin(request.base_url)
        try:
            input_state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(input_state):
            raise MoodleSessionExpired("Moodle browser session is missing")
        async with self._student_session_operation(input_state):
            # Student admission is latency-sensitive and must not queue behind
            # a long course/history crawl.
            async with self._operation(foreground=True) as browser:
                context = await self._new_context(browser, storage_state=input_state)
                try:
                    page = await context.new_page()
                    submission = await self._open_assignment_submission(page, context, request)
                    if "ASSIGN_FILE" in submission.available_transports:
                        selected = "ASSIGN_FILE"
                    elif "ASSIGN_ONLINE_TEXT" in submission.available_transports:
                        selected = "ASSIGN_ONLINE_TEXT"
                    else:  # The parser already fails closed; retain an explicit boundary.
                        raise MoodleProtocolError(
                            "Moodle Assignment has no supported response transport"
                        )
                    state = await self._state(context)
                finally:
                    await context.close()
            return AssignmentSubmissionPrepareResponse.model_validate(
                {
                    "status": "READY",
                    "preparation": {
                        "course_id": request.course_id,
                        "cmid": request.cmid,
                        "answer_transport": selected,
                        "available_answer_transports": list(submission.available_transports),
                    },
                    "storage_state": state,
                }
            )

    async def sync_assignment_submission(
        self,
        request: AssignmentSubmissionSyncRequest,
    ) -> AssignmentSubmissionSyncResponse:
        """Save one artifact through a student's proven Assignment form."""

        self._require_origin(request.base_url)
        try:
            input_state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(input_state):
            raise MoodleSessionExpired("Moodle browser session is missing")
        artifact = self._decode_artifact(request)
        fingerprint = canonical_hash(
            {
                "course_id": request.course_id,
                "cmid": request.cmid,
                "answer_transport": request.answer_transport,
                "filename": request.artifact.filename,
                "sha256": request.artifact.sha256,
                "size_bytes": len(artifact),
                "finalize": request.finalize,
                "requires_submission_statement": request.requires_submission_statement,
                "submission_drafts": request.submission_drafts,
                "max_submission_bytes_inherited": request.max_submission_bytes_inherited,
                "previous_managed_filename": request.previous_managed_filename,
                "previous_managed_sha256": request.previous_managed_sha256,
            }
        )

        async with self._student_session_operation(
            input_state,
            terminal=request.finalize,
        ):
            cached = self._assignment_sync_cache.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, response = cached
                if cached_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "Moodle assignment idempotency key was reused for another artifact"
                    )
                self._assignment_sync_cache.move_to_end(request.idempotency_key)
                return response.model_copy(update={"storage_state": input_state}, deep=True)

            # Saving/finalizing a student's answer is a foreground operation.
            async with self._operation(foreground=True) as browser:
                context = await self._new_context(browser, storage_state=input_state)
                try:
                    page = await context.new_page()
                    result = await self._execute_assignment_submission_sync(
                        context,
                        page,
                        request,
                        artifact,
                    )
                finally:
                    await context.close()
            self._assignment_sync_cache[request.idempotency_key] = (fingerprint, result)
            self._assignment_sync_cache.move_to_end(request.idempotency_key)
            while len(self._assignment_sync_cache) > self.settings.idempotency_cache_entries:
                self._assignment_sync_cache.popitem(last=False)
            return result.model_copy(deep=True)

    async def _execute_assignment_submission_sync(
        self,
        context: BrowserContext,
        page: Page,
        request: AssignmentSubmissionSyncRequest,
        artifact: bytes,
    ) -> AssignmentSubmissionSyncResponse:
        submission = await self._open_assignment_submission(page, context, request)
        if request.answer_transport not in submission.available_transports:
            raise MoodleProtocolError(
                "Moodle assignment response format no longer matches the imported activity"
            )
        statement_accepted = await self._accept_assignment_submission_statement(
            page,
            request,
            submission,
        )
        if request.answer_transport == "ASSIGN_FILE":
            if request.max_submission_bytes_inherited and submission.effective_max_bytes is None:
                raise MoodleProtocolError(
                    "Moodle Assignment inherited file limit is not exposed by the "
                    "student file manager"
                )
            if (
                submission.effective_max_bytes is not None
                and len(artifact) > submission.effective_max_bytes
            ):
                raise MoodleContractError(
                    "Moodle Assignment artifact exceeds the effective student file limit"
                )
            if (
                request.previous_managed_filename is not None
                and request.previous_managed_filename != request.artifact.filename
                and request.previous_managed_filename in submission.existing_filenames
            ):
                # We cannot prove an exact root path from Moodle's rendered
                # basename inventory.  Saving the new filename would leave two
                # managed artifacts; deleting first could destroy user data.
                raise MoodleProtocolError(
                    "Moodle managed artifact filename changed and safe replacement "
                    "requires manual removal of the previous attachment"
                )
            await self._replace_assignment_file(
                page,
                request.artifact.filename,
                artifact,
                replace_existing=request.artifact.filename in submission.existing_filenames,
                existing_filenames=submission.existing_filenames,
                attachment_urls=submission.attachment_urls,
                previous_managed_filename=request.previous_managed_filename,
                previous_managed_sha256=request.previous_managed_sha256,
            )
        else:
            await self._replace_assignment_online_text(page, submission, artifact)
        view = await self._save_assignment_submission(page, context, request)
        if request.finalize:
            if view.status != "FINALIZED":
                await self._finalize_assignment_submission(
                    page,
                    context,
                    request,
                    view,
                    statement_already_accepted=statement_accepted,
                )
            status = "FINALIZED"
        else:
            if view.status == "UNKNOWN":
                raise MoodleProtocolError("Moodle did not expose the saved submission status")
            if view.status == "FINALIZED":
                # The write itself succeeded, and Moodle made it durable/final
                # (notably when draft submissions are disabled). Return that
                # receipt so the core can record the exact delivered artifact
                # before closing its local attempt.
                status = "FINALIZED"
            else:
                await self._return_to_assignment_submission(
                    page,
                    context,
                    request,
                    submission,
                    artifact,
                )
                status = view.status
        state = await self._state(context)
        return AssignmentSubmissionSyncResponse.model_validate(
            {
                "status": status,
                "receipt": {
                    "course_id": request.course_id,
                    "cmid": request.cmid,
                    "filename": request.artifact.filename,
                    "sha256": request.artifact.sha256,
                    "size_bytes": len(artifact),
                    "idempotency_key": request.idempotency_key,
                },
                "storage_state": state,
            }
        )

    async def sync_quiz_essay(
        self,
        request: QuizEssaySyncRequest,
    ) -> QuizEssaySyncResponse:
        self._require_origin(request.base_url)
        try:
            input_state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(input_state):
            raise MoodleSessionExpired("Moodle browser session is missing")
        artifact = self._decode_artifact(request)
        fingerprint_payload: dict[str, object] = {
            "course_id": request.course_id,
            "cmid": request.cmid,
            "answer_transport": request.answer_transport,
            "filename": request.artifact.filename,
            "sha256": request.artifact.sha256,
            "size_bytes": len(artifact),
            "finalize": request.finalize,
        }
        # Preserve the original Quiz idempotency contract for callers that do
        # not participate in managed-file replacement.
        if request.previous_managed_filename is not None:
            fingerprint_payload["previous_managed_filename"] = request.previous_managed_filename
            fingerprint_payload["previous_managed_sha256"] = request.previous_managed_sha256
        if request.expected_attempt_id is not None:
            fingerprint_payload["expected_attempt_id"] = request.expected_attempt_id
            fingerprint_payload["expected_question_slot"] = request.expected_question_slot
        fingerprint = canonical_hash(fingerprint_payload)

        async with self._student_session_operation(
            input_state,
            terminal=request.finalize,
        ):
            cached = self._quiz_sync_cache.get(request.idempotency_key)
            if cached is not None:
                cached_fingerprint, response = cached
                if cached_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "Moodle quiz idempotency key was reused for another artifact"
                    )
                self._quiz_sync_cache.move_to_end(request.idempotency_key)
                # No browser operation happened on a replay. Preserve the
                # caller's newer leased session instead of rolling it back to
                # the state captured by an older cached response.
                return response.model_copy(
                    update={"storage_state": input_state},
                    deep=True,
                )

            progress = self._quiz_sync_progress.get(request.idempotency_key)
            if progress is not None:
                progress_fingerprint, _, _ = progress
                if progress_fingerprint != fingerprint:
                    raise IdempotencyConflict(
                        "Moodle quiz idempotency key was reused for another artifact"
                    )
                self._quiz_sync_progress.move_to_end(request.idempotency_key)

            # Student answer delivery must remain available while background
            # course discovery occupies its dedicated lane.
            async with self._operation(foreground=True) as browser:
                context = await self._new_context(browser, storage_state=input_state)
                try:
                    page = await context.new_page()
                    if progress is None:
                        result = await self._execute_quiz_essay_sync(
                            context,
                            page,
                            request,
                            artifact,
                            fingerprint=fingerprint,
                        )
                    else:
                        _, attempt_id, question_slot = progress
                        result = await self._resume_quiz_essay_finalization(
                            context,
                            page,
                            request,
                            artifact,
                            attempt_id=attempt_id,
                            question_slot=question_slot,
                        )
                finally:
                    await context.close()
            self._quiz_sync_progress.pop(request.idempotency_key, None)
            self._quiz_sync_cache[request.idempotency_key] = (fingerprint, result)
            self._quiz_sync_cache.move_to_end(request.idempotency_key)
            while len(self._quiz_sync_cache) > self.settings.idempotency_cache_entries:
                self._quiz_sync_cache.popitem(last=False)
            return result.model_copy(deep=True)

    async def prepare_quiz_essay(
        self,
        request: QuizEssayPrepareRequest,
    ) -> QuizEssayPrepareResponse:
        """Open or resume the real Essay attempt and bind its random question.

        Discovery can prove that a single random slot draws only from Essay
        questions, but Moodle chooses the concrete question only when the
        student's attempt starts.  This read-only preparation operation is the
        boundary at which the core receives the immutable attempt id, slot,
        rendered statement and actual response controls.
        """

        self._require_origin(request.base_url)
        try:
            input_state = sanitize_storage_state(
                request.storage_state,
                base_url=self.settings.base_url,
                maximum_bytes=self.settings.storage_state_max_bytes,
                reject_foreign=True,
            )
        except InvalidStorageState as exc:
            raise MoodleContractError(str(exc)) from exc
        if not has_moodle_session(input_state):
            raise MoodleSessionExpired("Moodle browser session is missing")

        async with self._student_session_operation(input_state):
            # Starting/resuming the live attempt is user-facing foreground I/O.
            async with self._operation(foreground=True) as browser:
                context = await self._new_context(browser, storage_state=input_state)
                try:
                    page = await context.new_page()
                    attempt = await self._open_real_quiz_attempt(
                        page,
                        context,
                        request,
                    )
                    if not attempt.question_text:
                        raise MoodleProtocolError(
                            "Moodle Essay attempt did not expose the selected question text"
                        )
                    available = list(attempt.available_transports)
                    if "ESSAY_ATTACHMENT" in attempt.available_transports:
                        selected = "ESSAY_ATTACHMENT"
                    elif "ESSAY_ONLINE_TEXT" in attempt.available_transports:
                        selected = "ESSAY_ONLINE_TEXT"
                    else:  # parse_attempt_page already fails closed; keep the contract explicit.
                        raise MoodleProtocolError(
                            "Moodle Essay attempt has no supported response transport"
                        )
                    state = await self._state(context)
                finally:
                    await context.close()
            return QuizEssayPrepareResponse.model_validate(
                {
                    "status": "READY",
                    "preparation": {
                        "course_id": request.course_id,
                        "cmid": request.cmid,
                        "attempt_id": attempt.attempt_id,
                        "question_slot": attempt.question_slot,
                        "question_text": attempt.question_text,
                        "answer_transport": selected,
                        "available_answer_transports": available,
                        "remaining_seconds": attempt.remaining_seconds,
                    },
                    "storage_state": state,
                }
            )

    def _decode_artifact(
        self,
        request: QuizEssaySyncRequest | AssignmentSubmissionSyncRequest,
    ) -> bytes:
        try:
            artifact = base64.b64decode(request.artifact.content_base64, validate=True)
        except ValueError as exc:  # pragma: no cover - Pydantic validates first
            raise MoodleContractError("Moodle submission artifact is not valid base64") from exc
        if len(artifact) > self.settings.artifact_max_bytes:
            raise MoodleContractError("Moodle submission artifact size is outside the limit")
        digest = hashlib.sha256(artifact).hexdigest()
        if digest != request.artifact.sha256:
            raise MoodleContractError("Moodle submission artifact digest does not match")
        return artifact

    async def _open_assignment_submission(
        self,
        page: Page,
        context: BrowserContext,
        request: AssignmentSubmissionPrepareRequest | AssignmentSubmissionSyncRequest,
    ) -> AssignmentSubmissionForm:
        view_url = f"{self.settings.base_url}/mod/assign/view.php?" + urlencode(
            {"id": request.cmid}
        )
        view_html = await self._goto(page, view_url)
        await self._require_authenticated_page(context, view_html)
        try:
            view = parse_assignment_view_page(
                view_html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
            )
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc
        if view.status == "FINALIZED":
            raise MoodleAttemptFinalized(
                "Moodle Assignment is already finalized and cannot be changed"
            )
        url = f"{self.settings.base_url}/mod/assign/view.php?" + urlencode(
            {"id": request.cmid, "action": "editsubmission"}
        )
        html = await self._goto(page, url)
        await self._require_authenticated_page(context, html)
        try:
            await page.locator(ASSIGNMENT_SAVE_SELECTOR).first.wait_for(
                state="attached",
                timeout=self.settings.navigation_timeout_ms,
            )
            html = await page.content()
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise MoodleActivityUnavailable(
                "Moodle Assignment submission form is not available to this student"
            ) from exc
        try:
            return parse_assignment_edit_page(
                html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
            )
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc

    async def _replace_assignment_online_text(
        self,
        page: Page,
        submission: AssignmentSubmissionForm,
        artifact: bytes,
    ) -> None:
        try:
            source = artifact.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MoodleContractError(
                "Moodle Assignment online text accepts UTF-8 source code only"
            ) from exc
        if "\0" in source:
            raise MoodleContractError("Moodle Assignment online text contains NUL")
        if submission.online_text_control_name != "onlinetext_editor[text]":
            raise MoodleProtocolError("Moodle assignment online-text control is missing")
        try:
            textarea = page.locator(f"form.mform {ASSIGNMENT_ONLINE_TEXT_SELECTOR}")
            if await textarea.count() != 1:
                raise MoodleProtocolError("Moodle assignment online-text control changed")
            editors = page.locator(
                "#fitem_id_onlinetext_editor div.editor_atto_content[contenteditable='true']"
            )
            if await editors.count() > 1:
                raise MoodleProtocolError("Moodle assignment online-text editor is ambiguous")
            if await textarea.is_visible() and await editors.count() == 0:
                await textarea.fill(source)
            else:
                html_source = f"<pre>{html.escape(source)}</pre>"
                if await editors.count() == 1:
                    await editors.first.evaluate(
                        """(element, value) => {
                            element.innerHTML = value;
                            for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                                element.dispatchEvent(new Event(eventName, {bubbles: true}));
                            }
                        }""",
                        html_source,
                    )
                await textarea.evaluate(
                    """(element, payload) => {
                        const tiny = window.tinyMCE && element.id
                            ? window.tinyMCE.get(element.id)
                            : null;
                        if (tiny) tiny.setContent(payload.html);
                        element.value = payload.html;
                        for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                            element.dispatchEvent(new Event(eventName, {bubbles: true}));
                        }
                    }""",
                    {"html": html_source},
                )
            if not await self._assignment_online_text_matches(page, source):
                raise MoodleProtocolError("Moodle did not accept the Assignment source text")
        except (MoodleContractError, MoodleProtocolError):
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle Assignment text update failed") from exc

    async def _accept_assignment_submission_statement(
        self,
        page: Page,
        request: AssignmentSubmissionSyncRequest,
        submission: AssignmentSubmissionForm,
    ) -> bool:
        if not request.requires_submission_statement:
            return False
        if submission.submission_statement_control_name == "submissionstatement":
            control = page.locator("form.mform input[name='submissionstatement'][type='checkbox']")
            if await control.count() != 1:
                raise MoodleProtocolError("Moodle assignment submission statement control changed")
            try:
                await control.check()
            except (PlaywrightTimeoutError, PlaywrightError) as exc:
                raise BrowserUnavailable(
                    "Moodle assignment submission statement could not be accepted"
                ) from exc
            return True
        return False

    async def _assignment_online_text_matches(self, page: Page, expected: str) -> bool:
        textarea = page.locator(f"form.mform {ASSIGNMENT_ONLINE_TEXT_SELECTOR}")
        if await textarea.count() != 1:
            return False
        stored = await textarea.input_value()
        if stored.replace("\r\n", "\n") == expected.replace("\r\n", "\n"):
            return True
        if "<" in stored and ">" in stored:
            rendered = BeautifulSoup(stored, "html.parser").get_text()
            return rendered.replace("\r\n", "\n") == expected.replace("\r\n", "\n")
        return False

    async def _replace_assignment_file(
        self,
        page: Page,
        filename: str,
        artifact: bytes,
        *,
        replace_existing: bool,
        existing_filenames: tuple[str, ...] = (),
        attachment_urls: tuple[tuple[str, str], ...] = (),
        previous_managed_filename: str | None = None,
        previous_managed_sha256: str | None = None,
    ) -> None:
        try:
            managers = page.locator(ASSIGNMENT_FILEMANAGER_SELECTOR)
            if await managers.count() != 1:
                raise MoodleProtocolError("Moodle assignment file manager changed")
            manager = managers.first
            add_buttons = manager.locator(".fp-btn-add")
            if await add_buttons.count() != 1:
                raise MoodleProtocolError("Moodle assignment file add control changed")
            if (
                previous_managed_filename is not None
                and previous_managed_filename != filename
                and (
                    await manager.get_by_text(
                        previous_managed_filename,
                        exact=True,
                    ).count()
                    > 0
                    or await manager.locator(
                        f'[data-filename="{previous_managed_filename}"], '
                        f'[title="{previous_managed_filename}"]'
                    ).count()
                    > 0
                )
            ):
                raise MoodleProtocolError(
                    "Moodle managed artifact filename changed and safe replacement "
                    "requires manual removal of the previous attachment"
                )
            parsed_replace_existing = _managed_target_replace_existing(
                existing_filenames,
                filename,
                previous_managed_filename,
            )
            live_target = (
                await manager.get_by_text(filename, exact=True).count() > 0
                or await manager.locator(
                    f'[data-filename="{filename}"], [title="{filename}"]'
                ).count()
                > 0
            )
            if live_target and previous_managed_filename != filename:
                raise MoodleProtocolError(
                    "Moodle artifact target already exists but connector ownership is unproven"
                )
            replace_existing = parsed_replace_existing or live_target
            if replace_existing:
                if previous_managed_filename != filename or previous_managed_sha256 is None:
                    raise MoodleProtocolError(
                        "Moodle artifact target already exists but connector ownership is unproven"
                    )
                await self._verify_assignment_managed_file(
                    page,
                    filename=filename,
                    expected_sha256=previous_managed_sha256,
                    attachment_urls=attachment_urls,
                )
            await add_buttons.click()
            repository = page.get_by_text(re.compile(r"^(?:Upload a file|Загрузить файл)$", re.I))
            await (
                await self._wait_for_unique_locator(
                    repository,
                    detail="Moodle upload repository is ambiguous",
                    visible=True,
                )
            ).click()
            inputs = page.locator(
                ".file-picker:visible input[type='file'], "
                ".filepicker:visible input[type='file'], "
                ".moodle-dialogue:visible input[type='file']"
            )
            try:
                file_input = await self._wait_for_unique_locator(
                    inputs,
                    detail="Moodle upload input is ambiguous",
                    visible=False,
                    timeout_ms=min(self.settings.navigation_timeout_ms, 5_000),
                )
            except MoodleProtocolError:
                if await inputs.count() > 0:
                    raise
                file_input = await self._wait_for_unique_locator(
                    page.locator(f".fp-content {FILE_INPUT_SELECTOR}"),
                    detail="Moodle upload input is ambiguous",
                    visible=False,
                )
            await file_input.set_input_files(
                {
                    "name": filename,
                    "mimeType": self._artifact_mime_type(filename),
                    "buffer": artifact,
                }
            )
            upload = await self._wait_for_unique_locator(
                page.locator(f"{FILE_UPLOAD_SELECTOR}:visible"),
                detail="Moodle upload button is ambiguous",
                visible=True,
            )
            await upload.click()
            overwrite = page.locator(f"{FILE_OVERWRITE_SELECTOR}:visible")
            overwrite_button: Locator | None = None
            if replace_existing:
                overwrite_button = await self._wait_for_unique_locator(
                    overwrite,
                    detail="Moodle overwrite confirmation is ambiguous",
                    visible=True,
                )
            else:
                with suppress(MoodleProtocolError):
                    overwrite_button = await self._wait_for_unique_locator(
                        overwrite,
                        detail="Moodle overwrite confirmation is ambiguous",
                        visible=True,
                        timeout_ms=1_500,
                    )
            if overwrite_button is not None:
                if not replace_existing:
                    raise MoodleProtocolError(
                        "Moodle requested an overwrite without connector ownership evidence"
                    )
                await overwrite_button.click()
                await overwrite_button.wait_for(
                    state="hidden",
                    timeout=self.settings.navigation_timeout_ms,
                )
            label = manager.get_by_text(filename, exact=True)
            try:
                await label.first.wait_for(
                    state="attached",
                    timeout=self.settings.navigation_timeout_ms,
                )
            except PlaywrightTimeoutError:
                if filename not in ((await manager.text_content()) or ""):
                    raise MoodleProtocolError(
                        "Moodle did not expose the uploaded Assignment artifact"
                    ) from None
        except MoodleProtocolError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle Assignment artifact upload failed") from exc

    async def _verify_assignment_managed_file(
        self,
        page: Page,
        *,
        filename: str,
        expected_sha256: str,
        attachment_urls: tuple[tuple[str, str], ...],
    ) -> None:
        """Hash the exact remote attachment before allowing an overwrite.

        A durable receipt proves which connector slot was previously written,
        but it does not prove that a user has not replaced that file in Moodle
        afterwards.  Only a unique same-origin URL captured from the current
        file manager and matching bytes provide that proof.
        """

        candidates = sorted(
            {url for rendered_name, url in attachment_urls if rendered_name == filename}
        )
        if len(candidates) != 1:
            raise MoodleProtocolError("Moodle managed artifact has no unambiguous download URL")
        target = candidates[0]
        parsed = urlsplit(target)
        try:
            origin = exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
        except ValueError as exc:
            raise MoodleProtocolError("Moodle managed artifact download URL is invalid") from exc
        path = unquote(parsed.path)
        if (
            origin != self.settings.base_url
            or parsed.username
            or parsed.password
            or not (path.startswith("/draftfile.php/") or path.startswith("/pluginfile.php/"))
            or PurePosixPath(path).name != filename
        ):
            raise MoodleProtocolError("Moodle managed artifact download URL changed origin")

        response = None
        try:
            response = await page.context.request.get(
                target,
                fail_on_status_code=False,
                max_redirects=0,
                timeout=self.settings.navigation_timeout_ms,
            )
            if response.status != 200 or response.url != target:
                raise MoodleProtocolError("Moodle managed artifact could not be downloaded safely")
            raw_length = response.headers.get("content-length", "").strip()
            if raw_length and (
                not raw_length.isdigit() or int(raw_length) > self.settings.artifact_max_bytes
            ):
                raise MoodleProtocolError(
                    "Moodle managed artifact download exceeds the connector limit"
                )
            content = await response.body()
            if len(content) > self.settings.artifact_max_bytes:
                raise MoodleProtocolError(
                    "Moodle managed artifact download exceeds the connector limit"
                )
            if not hmac.compare_digest(
                hashlib.sha256(content).hexdigest(),
                expected_sha256,
            ):
                raise MoodleProtocolError(
                    "Moodle managed artifact changed after the previous synchronization"
                )
        except MoodleProtocolError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle managed artifact verification failed") from exc
        finally:
            if response is not None:
                with suppress(PlaywrightError):
                    await response.dispose()

    async def _save_assignment_submission(
        self,
        page: Page,
        context: BrowserContext,
        request: AssignmentSubmissionSyncRequest,
    ) -> AssignmentSubmissionView:
        try:
            save = page.locator(ASSIGNMENT_SAVE_SELECTOR)
            if await save.count() != 1:
                raise MoodleProtocolError("Moodle assignment save trigger changed")
            await save.click()
            await page.wait_for_load_state("domcontentloaded")
            html = await page.content()
        except MoodleProtocolError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle Assignment could not be saved") from exc
        await self._require_authenticated_page(context, html)
        try:
            return parse_assignment_view_page(
                html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
            )
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc

    async def _assignment_action_form(self, page: Page, action: str, cmid: int) -> Locator:
        forms = page.locator("form")
        matches: list[Locator] = []
        for index in range(min(await forms.count(), 64)):
            candidate = forms.nth(index)
            action_control = candidate.locator(f"input[name='action'][value='{action}']")
            id_control = candidate.locator(f"input[name='id'][value='{cmid}']")
            if await action_control.count() == 1 and await id_control.count() == 1:
                matches.append(candidate)
        if len(matches) != 1:
            raise MoodleProtocolError(f"Moodle assignment {action} form is ambiguous")
        return matches[0]

    async def _assignment_action_trigger(self, form: Locator) -> Locator:
        candidates = form.locator("button[type='submit'], input[type='submit']")
        matches: list[Locator] = []
        for index in range(await candidates.count()):
            candidate = candidates.nth(index)
            if (await candidate.get_attribute("name") or "") != "cancel":
                matches.append(candidate)
        if len(matches) != 1:
            raise MoodleProtocolError("Moodle assignment submit trigger is ambiguous")
        return matches[0]

    async def _finalize_assignment_submission(
        self,
        page: Page,
        context: BrowserContext,
        request: AssignmentSubmissionSyncRequest,
        view: AssignmentSubmissionView,
        *,
        statement_already_accepted: bool,
    ) -> None:
        if not view.can_submit:
            raise MoodleProtocolError("Moodle assignment draft cannot be finalized")
        try:
            form = await self._assignment_action_form(page, "submit", request.cmid)
            trigger = await self._assignment_action_trigger(form)
            await trigger.click()
            await page.wait_for_load_state("domcontentloaded")
            html = await page.content()
            await self._require_authenticated_page(context, html)
            try:
                confirmation = parse_assignment_confirmation_page(
                    html,
                    page.url,
                    base_url=self.settings.base_url,
                    course_id=request.course_id,
                    cmid=request.cmid,
                )
            except MoodleMarkupError as exc:
                raise MoodleProtocolError(str(exc)) from exc
            if confirmation:
                form = await self._assignment_action_form(page, "confirmsubmit", request.cmid)
                statements = form.locator("input[name='submissionstatement'][type='checkbox']")
                if await statements.count() > 1:
                    raise MoodleProtocolError(
                        "Moodle assignment submission statement is ambiguous"
                    )
                if (
                    request.requires_submission_statement
                    and not statement_already_accepted
                    and await statements.count() != 1
                ):
                    raise MoodleProtocolError(
                        "Moodle assignment submission statement control is missing"
                    )
                if await statements.count() == 1:
                    if not request.requires_submission_statement:
                        raise MoodleProtocolError(
                            "Moodle assignment submission statement setting changed"
                        )
                    await statements.check()
                trigger = await self._assignment_action_trigger(form)
                await trigger.click()
                await page.wait_for_load_state("domcontentloaded")
                html = await page.content()
                await self._require_authenticated_page(context, html)
            final = parse_assignment_view_page(
                html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
            )
            if final.status != "FINALIZED":
                raise MoodleProtocolError("Moodle did not finalize the Assignment submission")
        except (MoodleProtocolError, MoodleSessionExpired):
            raise
        except MoodleMarkupError as exc:
            raise MoodleProtocolError(str(exc)) from exc
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle Assignment final submission failed") from exc

    async def _return_to_assignment_submission(
        self,
        page: Page,
        context: BrowserContext,
        request: AssignmentSubmissionSyncRequest,
        previous: AssignmentSubmissionForm,
        artifact: bytes,
    ) -> None:
        current_html = await page.content()
        current_view = parse_assignment_view_page(
            current_html,
            page.url,
            base_url=self.settings.base_url,
            course_id=request.course_id,
            cmid=request.cmid,
        )
        if current_view.status == "FINALIZED":
            raise MoodleAttemptFinalized(
                "Moodle Assignment was finalized while the checkpoint was being verified"
            )
        returned = await self._open_assignment_submission(page, context, request)
        if request.answer_transport not in returned.available_transports:
            raise MoodleProtocolError("Moodle Assignment submission control changed after save")
        if request.answer_transport == "ASSIGN_FILE":
            if request.artifact.filename not in returned.existing_filenames:
                raise MoodleProtocolError("Moodle did not preserve the Assignment artifact")
        else:
            try:
                expected = artifact.decode("utf-8")
            except UnicodeDecodeError as exc:  # validated before mutation
                raise MoodleContractError(
                    "Moodle Assignment online text accepts UTF-8 source code only"
                ) from exc
            if not await self._assignment_online_text_matches(page, expected):
                raise MoodleProtocolError("Moodle did not preserve the Assignment source text")

    async def _execute_quiz_essay_sync(
        self,
        context: BrowserContext,
        page: Page,
        request: QuizEssaySyncRequest,
        artifact: bytes,
        *,
        fingerprint: str | None = None,
    ) -> QuizEssaySyncResponse:
        attempt = await self._open_real_quiz_attempt(page, context, request)
        if request.answer_transport not in attempt.available_transports:
            raise MoodleProtocolError(
                "Moodle essay response format no longer matches the imported activity"
            )
        if request.answer_transport == "ESSAY_ATTACHMENT":
            if (
                request.previous_managed_filename is not None
                and request.previous_managed_filename != request.artifact.filename
                and request.previous_managed_filename in attempt.existing_filenames
            ):
                raise MoodleProtocolError(
                    "Moodle managed artifact filename changed and safe replacement "
                    "requires manual removal of the previous attachment"
                )
            await self._replace_quiz_attachment(
                page,
                request.artifact.filename,
                artifact,
                replace_existing=request.artifact.filename in attempt.existing_filenames,
                existing_filenames=attempt.existing_filenames,
                attachment_urls=attempt.attachment_urls,
                previous_managed_filename=request.previous_managed_filename,
                previous_managed_sha256=request.previous_managed_sha256,
            )
        else:
            await self._replace_quiz_online_text(page, attempt, artifact)
        summary = await self._save_quiz_answer(
            page,
            context,
            request,
            attempt,
        )
        if request.finalize:
            if fingerprint is not None:
                self._remember_quiz_sync_progress(
                    request,
                    fingerprint=fingerprint,
                    attempt=attempt,
                )
            await self._finalize_quiz_attempt(page, context, request, summary)
            sync_status = "FINALIZED"
        else:
            await self._return_to_quiz_attempt(page, context, request, attempt, artifact)
            sync_status = "DRAFT_SAVED"
        state = await self._state(context)
        return QuizEssaySyncResponse.model_validate(
            {
                "status": sync_status,
                "receipt": {
                    "course_id": request.course_id,
                    "cmid": request.cmid,
                    "attempt_id": attempt.attempt_id,
                    "question_slot": attempt.question_slot,
                    "filename": request.artifact.filename,
                    "sha256": request.artifact.sha256,
                    "size_bytes": len(artifact),
                    "idempotency_key": request.idempotency_key,
                },
                "storage_state": state,
            }
        )

    def _remember_quiz_sync_progress(
        self,
        request: QuizEssaySyncRequest,
        *,
        fingerprint: str,
        attempt: QuizAttempt,
    ) -> None:
        self._quiz_sync_progress[request.idempotency_key] = (
            fingerprint,
            attempt.attempt_id,
            attempt.question_slot,
        )
        self._quiz_sync_progress.move_to_end(request.idempotency_key)
        while len(self._quiz_sync_progress) > self.settings.idempotency_cache_entries:
            self._quiz_sync_progress.popitem(last=False)

    async def _resume_quiz_essay_finalization(
        self,
        context: BrowserContext,
        page: Page,
        request: QuizEssaySyncRequest,
        artifact: bytes,
        *,
        attempt_id: str,
        question_slot: str,
    ) -> QuizEssaySyncResponse:
        """Finish an answer that this process already saved for the same request."""

        if not request.finalize:
            raise IdempotencyConflict(
                "Moodle quiz finalization progress was reused for a draft request"
            )
        if request.expected_attempt_id is not None and (
            request.expected_attempt_id != attempt_id
            or request.expected_question_slot != question_slot
        ):
            raise IdempotencyConflict(
                "Moodle quiz finalization progress identifies another attempt"
            )

        summary_url = f"{self.settings.base_url}/mod/quiz/summary.php?" + urlencode(
            {"attempt": attempt_id, "cmid": request.cmid}
        )
        html = await self._goto(page, summary_url)
        if not await self._quiz_final_page_is_confirmed(
            page,
            context,
            request,
            attempt_id,
            allow_causal_completed_view=True,
        ):
            await self._require_authenticated_page(context, html)
            try:
                summary = parse_summary_page(
                    html,
                    page.url,
                    base_url=self.settings.base_url,
                    course_id=request.course_id,
                    cmid=request.cmid,
                    attempt_id=attempt_id,
                    require_finalize=True,
                )
            except MoodleMarkupError as exc:
                # Moodle may complete the POST between the first inspection
                # and parsing the redirected page.  Accept only the same exact
                # terminal attempt, never an unrelated completed attempt.
                if not await self._quiz_final_page_is_confirmed(
                    page,
                    context,
                    request,
                    attempt_id,
                    allow_causal_completed_view=True,
                ):
                    self._raise_quiz_markup(exc)
                    raise AssertionError("unreachable") from exc
            else:
                await self._finalize_quiz_attempt(page, context, request, summary)

        state = await self._state(context)
        return QuizEssaySyncResponse.model_validate(
            {
                "status": "FINALIZED",
                "receipt": {
                    "course_id": request.course_id,
                    "cmid": request.cmid,
                    "attempt_id": attempt_id,
                    "question_slot": question_slot,
                    "filename": request.artifact.filename,
                    "sha256": request.artifact.sha256,
                    "size_bytes": len(artifact),
                    "idempotency_key": request.idempotency_key,
                },
                "storage_state": state,
            }
        )

    def _raise_quiz_markup(self, exc: MoodleMarkupError) -> None:
        detail = str(exc).lower()
        if "preview" in detail or "просмотр" in detail:
            raise QuizPreviewRejected("Moodle teacher preview is not writable") from exc
        raise MoodleProtocolError(str(exc)) from exc

    def _quiz_attempt_is_finalized_page(
        self,
        html: str,
        current_url: str,
        request: QuizEssayPrepareRequest | QuizEssaySyncRequest,
        attempt_id: str,
    ) -> bool:
        """Recognize a terminal redirect without treating a live view as terminal."""

        parsed = urlsplit(current_url)
        path = parsed.path.rstrip("/")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if path == "/mod/quiz/review.php":
            review_attempt = query.get("attempt")
            return (
                review_attempt is not None
                and len(review_attempt) == 1
                and bool(re.fullmatch(r"[1-9][0-9]{0,19}", review_attempt[0]))
                and query.get("cmid") in (None, [str(request.cmid)])
            )
        if path != "/mod/quiz/view.php" or query.get("id") != [str(request.cmid)]:
            return False
        try:
            parse_quiz_view(
                html,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
                expected_attempt_id=attempt_id,
            )
        except QuizAttemptNotActive:
            return True
        except MoodleMarkupError:
            return False
        return False

    async def _require_authenticated_page(
        self,
        context: BrowserContext,
        html: str,
    ) -> None:
        authenticated, _ = await self._authenticated(context, html)
        if not authenticated:
            raise MoodleSessionExpired("Moodle browser session expired")

    async def _require_session_without_global_navigation(
        self,
        context: BrowserContext,
        html: str,
        current_url: str,
    ) -> None:
        """Authenticate standalone Moodle documents that omit the user menu.

        Quiz manual-grading pages in Moodle 5.2 do not render the global
        logout/user-menu anchors used by ``has_authenticated_markup``.  A live
        MoodleSession cookie is necessary here, while the caller must prove
        the endpoint's exact URL, course and form identifiers separately.
        """

        state = await self._state(context)
        if not has_moodle_session(state):
            raise MoodleSessionExpired("Moodle browser session expired")
        parsed = urlsplit(current_url)
        soup = BeautifulSoup(html, "html.parser")
        if (
            parsed.path.rstrip("/") == "/login/index.php"
            or soup.select_one(
                "#loginbtn, form[action*='/login/index.php'], "
                "input[name='username'], input[name='password']"
            )
            is not None
        ):
            raise MoodleSessionExpired("Moodle browser session expired")

    async def _open_real_quiz_attempt(
        self,
        page: Page,
        context: BrowserContext,
        request: QuizEssayPrepareRequest | QuizEssaySyncRequest,
    ) -> QuizAttempt:
        view_url = f"{self.settings.base_url}/mod/quiz/view.php?" + urlencode({"id": request.cmid})
        html = await self._goto(page, view_url)
        await self._require_authenticated_page(context, html)
        current = urlsplit(page.url)
        if current.path.rstrip("/") != "/mod/quiz/view.php" or parse_qs(
            current.query, keep_blank_values=True
        ).get("id") != [str(request.cmid)]:
            raise MoodleProtocolError("Moodle quiz navigation changed target")
        try:
            launch = parse_quiz_view(
                html,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
                expected_attempt_id=request.expected_attempt_id,
            )
        except QuizAttemptNotActive as exc:
            raise MoodleAttemptFinalized(
                "The bound Moodle Quiz attempt is already finalized"
            ) from exc
        except QuizAttemptUnavailable as exc:
            raise MoodleActivityUnavailable(
                "Moodle Quiz is not currently available to this student"
            ) from exc
        except MoodleMarkupError as exc:
            self._raise_quiz_markup(exc)
            raise AssertionError("unreachable") from exc
        try:
            await self._activate_quiz_launch(page, launch)
        except MoodleProtocolError as exc:
            if request.expected_attempt_id is not None:
                raise MoodleAttemptFinalized(
                    "The bound Moodle Quiz attempt stopped being available before opening"
                ) from exc
            raise
        try:
            await page.wait_for_load_state("domcontentloaded")
            await page.locator(ESSAY_SELECTOR).first.wait_for(
                state="attached",
                timeout=self.settings.navigation_timeout_ms,
            )
            attempt_html = await page.content()
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            terminal_html = ""
            with suppress(PlaywrightError):
                terminal_html = await page.content()
            if request.expected_attempt_id is not None and self._quiz_attempt_is_finalized_page(
                terminal_html,
                page.url,
                request,
                request.expected_attempt_id,
            ):
                raise MoodleAttemptFinalized(
                    "The bound Moodle Quiz attempt was finalized while it was opening"
                ) from exc
            raise BrowserUnavailable("Moodle quiz attempt did not open") from exc
        await self._require_authenticated_page(context, attempt_html)
        try:
            attempt = parse_attempt_page(
                attempt_html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
            )
        except MoodleMarkupError as exc:
            self._raise_quiz_markup(exc)
            raise AssertionError("unreachable") from exc
        if request.expected_attempt_id is not None and (
            attempt.attempt_id != request.expected_attempt_id
            or attempt.question_slot != request.expected_question_slot
        ):
            raise MoodleAttemptFinalized(
                "The bound Moodle Quiz attempt can no longer be edited safely"
            )
        return attempt

    async def _activate_quiz_launch(self, page: Page, launch: QuizLaunch) -> None:
        try:
            if launch.kind == "FORM":
                forms = page.locator(QUIZ_DIRECT_START_FORM_SELECTOR)
                if await forms.count() != 1:
                    raise MoodleProtocolError("Moodle quiz start form changed")
                triggers = forms.locator(
                    "button[type='submit']:not([name='cancel']), "
                    "input[type='submit']:not([name='cancel'])"
                )
                if await triggers.count() != 1:
                    raise MoodleProtocolError("Moodle quiz start trigger changed")
                await triggers.click()
                if launch.requires_preflight:
                    preflight = page.locator(QUIZ_PREFLIGHT_FORM_SELECTOR)
                    await preflight.wait_for(
                        state="visible",
                        timeout=self.settings.navigation_timeout_ms,
                    )
                    preflight_triggers = page.locator(QUIZ_PREFLIGHT_START_SELECTOR)
                    if await preflight_triggers.count() != 1:
                        raise MoodleProtocolError("Moodle quiz preflight start trigger changed")
                    await preflight_triggers.click()
                return

            links = page.locator(QUIZ_CONTINUE_LINK_SELECTOR)
            matches = []
            for index in range(await links.count()):
                candidate = links.nth(index)
                href = await candidate.get_attribute("href")
                if href and urljoin(f"{self.settings.base_url}/", href) == launch.target_url:
                    matches.append(candidate)
            if len(matches) != 1:
                raise MoodleProtocolError("Moodle quiz continue trigger changed")
            await matches[0].click()
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle quiz attempt trigger failed") from exc

    async def _replace_quiz_online_text(
        self,
        page: Page,
        attempt: QuizAttempt,
        artifact: bytes,
    ) -> None:
        """Put source code into the one Essay response editor without reformatting it."""

        try:
            source = artifact.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MoodleContractError(
                "Moodle online-text Essay accepts UTF-8 source code only"
            ) from exc
        if "\0" in source:
            raise MoodleContractError("Moodle online-text Essay source contains NUL")
        control_name = attempt.online_text_control_name
        if not control_name:
            raise MoodleProtocolError("Moodle essay online-text control is missing")
        try:
            essays = page.locator(ESSAY_SELECTOR)
            textarea = page.locator(f"{ESSAY_SELECTOR} textarea[name='{control_name}']")
            if await essays.count() != 1 or await textarea.count() != 1:
                raise MoodleProtocolError("Moodle essay online-text control changed")

            essay = essays.first
            atto = essay.locator("div.editor_atto_content[contenteditable='true']")
            editor_count = await atto.count()
            if editor_count > 1:
                raise MoodleProtocolError("Moodle essay online-text editor is ambiguous")

            if await textarea.is_visible() and editor_count == 0:
                await textarea.fill(source)
            else:
                # Rich-text Moodle editors keep an HTML value in the hidden
                # textarea. A single <pre> preserves every tab and leading
                # space, while the visible editor receives the same text.
                html_source = f"<pre>{html.escape(source)}</pre>"
                if editor_count == 1:
                    await atto.first.evaluate(
                        """(element, value) => {
                            element.innerHTML = value;
                            for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                                element.dispatchEvent(new Event(eventName, {bubbles: true}));
                            }
                        }""",
                        html_source,
                    )
                await textarea.evaluate(
                    """(element, payload) => {
                        const tiny = window.tinyMCE && element.id
                            ? window.tinyMCE.get(element.id)
                            : null;
                        if (tiny) tiny.setContent(payload.html);
                        element.value = payload.html;
                        for (const eventName of ['input', 'keyup', 'change', 'blur']) {
                            element.dispatchEvent(new Event(eventName, {bubbles: true}));
                        }
                    }""",
                    {"html": html_source},
                )

            if not await self._quiz_online_text_matches(page, attempt, source):
                raise MoodleProtocolError("Moodle did not accept the Essay source text")
        except (MoodleContractError, MoodleProtocolError):
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle Essay text update failed") from exc

    async def _quiz_online_text_matches(
        self,
        page: Page,
        attempt: QuizAttempt,
        expected: str,
    ) -> bool:
        control_name = attempt.online_text_control_name
        if not control_name:
            return False
        textarea = page.locator(f"{ESSAY_SELECTOR} textarea[name='{control_name}']")
        if await textarea.count() != 1:
            return False
        stored = await textarea.input_value()
        if stored.replace("\r\n", "\n") == expected.replace("\r\n", "\n"):
            return True
        if "<" in stored and ">" in stored:
            rendered = BeautifulSoup(stored, "html.parser").get_text()
            return rendered.replace("\r\n", "\n") == expected.replace("\r\n", "\n")
        return False

    async def _replace_quiz_attachment(
        self,
        page: Page,
        filename: str,
        artifact: bytes,
        *,
        replace_existing: bool,
        existing_filenames: tuple[str, ...] = (),
        attachment_urls: tuple[tuple[str, str], ...] = (),
        previous_managed_filename: str | None = None,
        previous_managed_sha256: str | None = None,
    ) -> None:
        try:
            essays = page.locator(ESSAY_SELECTOR)
            managers = page.locator(FILEMANAGER_SELECTOR)
            add_buttons = page.locator(FILE_ADD_SELECTOR)
            if (
                await essays.count() != 1
                or await managers.count() != 1
                or await add_buttons.count() != 1
            ):
                raise MoodleProtocolError("Moodle essay file manager changed")
            manager = managers.first
            if (
                previous_managed_filename is not None
                and previous_managed_filename != filename
                and (
                    await manager.get_by_text(
                        previous_managed_filename,
                        exact=True,
                    ).count()
                    > 0
                    or await manager.locator(
                        f'[data-filename="{previous_managed_filename}"], '
                        f'[title="{previous_managed_filename}"]'
                    ).count()
                    > 0
                )
            ):
                raise MoodleProtocolError(
                    "Moodle managed artifact filename changed and safe replacement "
                    "requires manual removal of the previous attachment"
                )
            parsed_replace_existing = _managed_target_replace_existing(
                existing_filenames,
                filename,
                previous_managed_filename,
            )
            # Parsing the server-rendered file list and checking the live DOM
            # protect against a lazy file-manager rendering an existing stable
            # filename after the attempt page itself was parsed.
            existing_text = manager.get_by_text(filename, exact=True)
            existing_metadata = manager.locator(
                f'[data-filename="{filename}"], [title="{filename}"]'
            )
            live_target = await existing_text.count() > 0 or await existing_metadata.count() > 0
            if live_target and previous_managed_filename != filename:
                raise MoodleProtocolError(
                    "Moodle artifact target already exists but connector ownership is unproven"
                )
            replace_existing = parsed_replace_existing or live_target
            receipt_owns_target = (
                previous_managed_filename == filename and previous_managed_sha256 is not None
            )
            remote_verified = False
            if replace_existing and not receipt_owns_target:
                raise MoodleProtocolError(
                    "Moodle artifact target already exists but connector ownership is unproven"
                )
            if receipt_owns_target:
                remote_verified = await self._verify_quiz_managed_file_if_exposed(
                    page,
                    filename=filename,
                    expected_sha256=previous_managed_sha256,
                    attachment_urls=attachment_urls,
                )
            await add_buttons.click()

            repository = page.get_by_text(re.compile(r"^(?:Upload a file|Загрузить файл)$", re.I))
            upload_repository = await self._wait_for_unique_locator(
                repository,
                detail="Moodle upload repository is ambiguous",
                visible=True,
            )
            await upload_repository.click()

            file_inputs = page.locator(
                ".file-picker:visible input[type='file'], "
                ".filepicker:visible input[type='file'], "
                ".moodle-dialogue:visible input[type='file']"
            )
            try:
                file_input = await self._wait_for_unique_locator(
                    file_inputs,
                    detail="Moodle upload input is ambiguous",
                    visible=False,
                    timeout_ms=min(self.settings.navigation_timeout_ms, 5_000),
                )
            except MoodleProtocolError:
                if await file_inputs.count() > 0:
                    # A scoped input existed but was ambiguous. Do not weaken
                    # that signal by silently selecting a broader fallback.
                    raise
                fallback_inputs = page.locator(f".fp-content {FILE_INPUT_SELECTOR}")
                file_input = await self._wait_for_unique_locator(
                    fallback_inputs,
                    detail="Moodle upload input is ambiguous",
                    visible=False,
                )
            await file_input.set_input_files(
                {
                    "name": filename,
                    "mimeType": self._artifact_mime_type(filename),
                    "buffer": artifact,
                }
            )
            upload_buttons = page.locator(f"{FILE_UPLOAD_SELECTOR}:visible")
            upload_button = await self._wait_for_unique_locator(
                upload_buttons,
                detail="Moodle upload button is ambiguous",
                visible=True,
            )
            await upload_button.click()

            # Moodle asks for an explicit overwrite when the stable filename is
            # already present. Check unconditionally: DOM text is not a reliable
            # indicator when the file-manager renders icons lazily.
            overwrite = page.locator(f"{FILE_OVERWRITE_SELECTOR}:visible")
            overwrite_button: Locator | None = None
            if replace_existing:
                overwrite_button = await self._wait_for_unique_locator(
                    overwrite,
                    detail="Moodle overwrite confirmation is ambiguous",
                    visible=True,
                )
            else:
                with suppress(MoodleProtocolError):
                    overwrite_button = await self._wait_for_unique_locator(
                        overwrite,
                        detail="Moodle overwrite confirmation is ambiguous",
                        visible=True,
                        timeout_ms=(
                            min(self.settings.navigation_timeout_ms, 5_000)
                            if receipt_owns_target
                            else 1_500
                        ),
                    )
            if overwrite_button is not None:
                # Newer Moodle versions may render an empty file manager until
                # upload time and reveal the existing stable filename only via
                # this confirmation.  The durable receipt is ownership
                # evidence even when that lazy DOM omitted the file.  Hash the
                # remote bytes too whenever Moodle exposed a usable URL.
                if not receipt_owns_target:
                    raise MoodleProtocolError(
                        "Moodle requested an overwrite without connector ownership evidence"
                    )
                if not remote_verified:
                    await self._verify_quiz_managed_file_if_exposed(
                        page,
                        filename=filename,
                        expected_sha256=previous_managed_sha256,
                        attachment_urls=attachment_urls,
                    )
                await overwrite_button.click()
                await overwrite_button.wait_for(
                    state="hidden",
                    timeout=self.settings.navigation_timeout_ms,
                )

            file_label = manager.get_by_text(filename, exact=True)
            try:
                await file_label.first.wait_for(
                    state="attached", timeout=self.settings.navigation_timeout_ms
                )
            except PlaywrightTimeoutError:
                refreshed = (await manager.text_content()) or ""
                if filename not in refreshed:
                    raise MoodleProtocolError(
                        "Moodle did not expose the uploaded artifact"
                    ) from None
        except MoodleProtocolError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle artifact upload failed") from exc

    async def _verify_quiz_managed_file_if_exposed(
        self,
        page: Page,
        *,
        filename: str,
        expected_sha256: str,
        attachment_urls: tuple[tuple[str, str], ...],
    ) -> bool:
        """Verify the previous Quiz attachment when Moodle exposes its URL.

        Quiz file managers are lazy in some Moodle releases: the attempt HTML
        and live manager can omit an existing file even though the upload API
        subsequently asks to replace it.  In that case the durable receipt is
        the only available ownership proof.  If an exact URL is exposed, use
        the stronger byte-for-byte verification before any replacement.
        """

        candidates = tuple(
            (rendered_name, url)
            for rendered_name, url in attachment_urls
            if rendered_name == filename
        )
        if not candidates:
            return False
        await self._verify_assignment_managed_file(
            page,
            filename=filename,
            expected_sha256=expected_sha256,
            attachment_urls=candidates,
        )
        return True

    async def _wait_for_unique_locator(
        self,
        locator: Locator,
        *,
        detail: str,
        visible: bool,
        timeout_ms: int | None = None,
    ) -> Locator:
        """Wait for one bounded DOM target without executing page JavaScript."""

        timeout = timeout_ms or self.settings.navigation_timeout_ms
        deadline = asyncio.get_running_loop().time() + timeout / 1_000
        while True:
            count = await locator.count()
            if count > 32:
                raise MoodleProtocolError(detail)
            matches: list[Locator] = []
            for index in range(count):
                candidate = locator.nth(index)
                if not visible or await candidate.is_visible():
                    matches.append(candidate)
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1 or asyncio.get_running_loop().time() >= deadline:
                raise MoodleProtocolError(detail)
            await asyncio.sleep(0.05)

    @staticmethod
    def _artifact_mime_type(filename: str) -> str:
        lowered = filename.lower()
        if lowered.endswith((".c", ".cc", ".cpp", ".cxx", ".h", ".hpp")):
            return "text/plain"
        if lowered.endswith(".zip"):
            return "application/zip"
        return "application/octet-stream"

    async def _save_quiz_answer(
        self,
        page: Page,
        context: BrowserContext,
        request: QuizEssaySyncRequest,
        attempt: QuizAttempt,
    ) -> QuizSummary:
        try:
            next_nav = page.locator(NEXT_NAV_SELECTOR)
            if await next_nav.count() != 1:
                raise MoodleProtocolError("Moodle quiz save navigation is ambiguous")
            await next_nav.click()
            await page.wait_for_load_state("domcontentloaded")
            html = await page.content()
        except MoodleProtocolError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle quiz answer could not be saved") from exc
        await self._require_authenticated_page(context, html)
        try:
            return parse_summary_page(
                html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
                attempt_id=attempt.attempt_id,
                require_finalize=request.finalize,
            )
        except MoodleMarkupError as exc:
            if self._quiz_attempt_is_finalized_page(
                html,
                page.url,
                request,
                attempt.attempt_id,
            ):
                raise MoodleAttemptFinalized(
                    "The bound Moodle Quiz attempt was finalized while saving"
                ) from exc
            self._raise_quiz_markup(exc)
            raise AssertionError("unreachable") from exc

    async def _return_to_quiz_attempt(
        self,
        page: Page,
        context: BrowserContext,
        request: QuizEssaySyncRequest,
        attempt: QuizAttempt,
        artifact: bytes,
    ) -> None:
        url = f"{self.settings.base_url}/mod/quiz/attempt.php?" + urlencode(
            {"attempt": attempt.attempt_id, "cmid": request.cmid}
        )
        html = await self._goto(page, url)
        try:
            response_selector = (
                FILE_ADD_SELECTOR
                if request.answer_transport == "ESSAY_ATTACHMENT"
                else ONLINE_TEXT_SELECTOR
            )
            await page.locator(response_selector).first.wait_for(
                state="attached",
                timeout=self.settings.navigation_timeout_ms,
            )
            if request.answer_transport == "ESSAY_ATTACHMENT":
                await (
                    page.locator(FILEMANAGER_SELECTOR)
                    .get_by_text(
                        request.artifact.filename,
                        exact=True,
                    )
                    .first.wait_for(
                        state="attached",
                        timeout=self.settings.navigation_timeout_ms,
                    )
                )
            html = await page.content()
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            if self._quiz_attempt_is_finalized_page(
                html,
                page.url,
                request,
                attempt.attempt_id,
            ):
                raise MoodleAttemptFinalized(
                    "The bound Moodle Quiz attempt was finalized while verifying the checkpoint"
                ) from exc
            raise MoodleProtocolError("Moodle did not preserve the draft artifact") from exc
        await self._require_authenticated_page(context, html)
        try:
            returned = parse_attempt_page(
                html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
            )
        except MoodleMarkupError as exc:
            if self._quiz_attempt_is_finalized_page(
                html,
                page.url,
                request,
                attempt.attempt_id,
            ):
                raise MoodleAttemptFinalized(
                    "The bound Moodle Quiz attempt was finalized while verifying the checkpoint"
                ) from exc
            self._raise_quiz_markup(exc)
            raise AssertionError("unreachable") from exc
        identity_changed = (
            returned.attempt_id != attempt.attempt_id
            or returned.question_slot != attempt.question_slot
            or request.answer_transport not in returned.available_transports
        )
        attachment_missing = (
            request.answer_transport == "ESSAY_ATTACHMENT"
            and request.artifact.filename not in returned.existing_filenames
        )
        text_missing = False
        if request.answer_transport == "ESSAY_ONLINE_TEXT":
            try:
                expected = artifact.decode("utf-8")
            except UnicodeDecodeError as exc:  # validated before navigation
                raise MoodleContractError(
                    "Moodle online-text Essay accepts UTF-8 source code only"
                ) from exc
            text_missing = not await self._quiz_online_text_matches(page, returned, expected)
        if identity_changed or attachment_missing or text_missing:
            raise MoodleProtocolError("Moodle did not preserve the draft artifact")

    async def _finalize_quiz_attempt(
        self,
        page: Page,
        context: BrowserContext,
        request: QuizEssaySyncRequest,
        summary: QuizSummary,
    ) -> None:
        try:
            current = urlsplit(page.url)
            current_query = parse_qs(current.query, keep_blank_values=True)
            expected_origin = urlsplit(self.settings.base_url)
            if (
                (current.scheme, current.netloc)
                != (expected_origin.scheme, expected_origin.netloc)
                or current.path.rstrip("/") != "/mod/quiz/summary.php"
                or current_query.get("attempt") != [summary.attempt_id]
                or current_query.get("cmid") != [str(request.cmid)]
            ):
                raise MoodleProtocolError("Moodle final submission summary changed")
            forms = page.locator(FINALIZE_FORM_SELECTOR)
            attempt_fields = page.locator(FINALIZE_ATTEMPT_SELECTOR)
            cmid_fields = page.locator(FINALIZE_CMID_SELECTOR)
            finish_fields = page.locator(FINALIZE_FINISH_SELECTOR)
            sesskey_fields = page.locator(FINALIZE_SESSKEY_SELECTOR)
            timeup_fields = page.locator(FINALIZE_TIMEUP_SELECTOR)
            triggers = page.locator(FINALIZE_TRIGGER_SELECTOR)
            if (
                await forms.count() != 1
                or await attempt_fields.count() != 1
                or await cmid_fields.count() != 1
                or await finish_fields.count() != 1
                or await sesskey_fields.count() != 1
                or await timeup_fields.count() != 1
                or await triggers.count() != 1
            ):
                raise MoodleProtocolError("Moodle final submission trigger is ambiguous")
            if await attempt_fields.get_attribute("value") != summary.attempt_id:
                raise MoodleProtocolError("Moodle final submission attempt id changed")
            if await cmid_fields.get_attribute("value") != str(request.cmid):
                raise MoodleProtocolError("Moodle final submission activity id changed")
            if await finish_fields.get_attribute("value") != "1":
                raise MoodleProtocolError("Moodle final submission flag changed")
            if await timeup_fields.get_attribute("value") != "0":
                raise MoodleProtocolError("Moodle final submission time flag changed")
            sesskey = await sesskey_fields.get_attribute("value")
            if not sesskey or len(sesskey) > 128:
                raise MoodleProtocolError("Moodle final submission session key changed")
            trigger_text = (await triggers.text_content()) or ""
            if not trigger_text:
                trigger_text = await triggers.get_attribute("value") or ""
            if not finalize_text_matches(trigger_text):
                raise MoodleProtocolError("Moodle final submission trigger changed")
            method = (await forms.get_attribute("method") or "get").lower()
            if method != "post":
                raise MoodleProtocolError("Moodle final submission method changed")

            # Moodle 5.2's own ``mod_quiz/submission_confirmation`` module
            # invokes form.submit() after the learner accepts its modal. The
            # application has already collected that explicit confirmation, so
            # submit the exact, strictly validated Moodle form directly. Trying
            # to drive the transient Moodle modal was racy: the file could be
            # saved while the connector timed out and uploaded it again later.
            await forms.evaluate("form => HTMLFormElement.prototype.submit.call(form)")
        except MoodleProtocolError:
            raise
        except (PlaywrightTimeoutError, PlaywrightError) as exc:
            raise BrowserUnavailable("Moodle final submission trigger failed") from exc

        # page.url can expose the final target before its authenticated DOM has
        # replaced the transitional processattempt.php document. Poll until
        # Moodle proves that this exact bound attempt is terminal.
        await self._wait_for_quiz_final_page(
            page,
            context,
            request,
            summary.attempt_id,
        )

    async def _wait_for_quiz_final_page(
        self,
        page: Page,
        context: BrowserContext,
        request: QuizEssaySyncRequest,
        attempt_id: str,
    ) -> None:
        """Wait until Moodle proves that the bound attempt is terminal."""

        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self.settings.navigation_timeout_ms / 1_000)
        while True:
            if await self._quiz_final_page_is_confirmed(
                page,
                context,
                request,
                attempt_id,
                allow_causal_completed_view=True,
                transient_document=True,
            ):
                return

            remaining = deadline - loop.time()
            if remaining <= 0:
                state = await self._state(context)
                if not has_moodle_session(state):
                    raise MoodleSessionExpired("Moodle browser session expired")
                raise BrowserUnavailable("Moodle final submission timed out")
            await asyncio.sleep(min(0.05, remaining))

    async def _quiz_final_page_is_confirmed(
        self,
        page: Page,
        context: BrowserContext,
        request: QuizEssaySyncRequest,
        attempt_id: str,
        *,
        allow_causal_completed_view: bool = False,
        transient_document: bool = False,
    ) -> bool:
        """Return true only for an authenticated, exact-attempt final page."""

        current = urlsplit(page.url)
        current_query = parse_qs(current.query, keep_blank_values=True)
        stable_candidate = (
            current.path.rstrip("/") == "/mod/quiz/review.php"
            and current_query.get("attempt") == [attempt_id]
            and current_query.get("cmid") in (None, [str(request.cmid)])
        ) or (
            current.path.rstrip("/") == "/mod/quiz/view.php"
            and current_query.get("id") == [str(request.cmid)]
        )
        if not stable_candidate:
            # During a direct POST Moodle briefly exposes processattempt.php or
            # an empty transitional document.  It is neither a final-page proof
            # nor evidence that the authenticated session expired.
            return False
        try:
            html = await page.content()
        except PlaywrightError as exc:
            if transient_document:
                return False
            raise BrowserUnavailable("Moodle final page could not be inspected") from exc
        authenticated, state = await self._authenticated(context, html)
        if not has_moodle_session(state):
            raise MoodleSessionExpired("Moodle browser session expired")
        if not authenticated:
            # During navigation Playwright can expose view.php before the new
            # document has its logout/user-menu evidence.  A still-present
            # MoodleSession cookie makes that a transient DOM state, not an
            # expired login.  The bounded caller keeps polling.
            if transient_document:
                return False
            raise MoodleProtocolError("Moodle final page is not authenticated")
        try:
            validate_final_page(
                html,
                page.url,
                base_url=self.settings.base_url,
                course_id=request.course_id,
                cmid=request.cmid,
                attempt_id=attempt_id,
                allow_causal_completed_view=allow_causal_completed_view,
            )
        except MoodleMarkupError:
            return False
        return True
