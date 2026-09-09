from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

import httpx

from app.core.config import Settings

from ._http import canonical_json, request_json_limited, secret_value, sha256_hex
from .errors import IntegrationConfigurationError, IntegrationProtocolError
from .moodle import CourseDiscovery

_MAX_COURSES = 256
_MAX_SECTIONS = 512
_MAX_ACTIVITIES = 4096
_MAX_ASSIGNMENTS = 4096
_MAX_ROSTER_MEMBERS = 5000
_MAX_GROUPS_PER_MEMBER = 128
_MAX_FUNCTIONS = 4096
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{16,1024}$")
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_-]{1,64}$")

# These entries expose course administration, not merely course participation.
# Unknown or site-specific option names deliberately do not grant teacher rights.
_TEACHER_ADMIN_OPTIONS = frozenset(
    {
        "backup",
        "backuprestore",
        "checkpermissions",
        "contentbank",
        "coursecompletion",
        "customfields",
        "editsettings",
        "enrolusers",
        "filters",
        "groups",
        "import",
        "manageactivities",
        "permissions",
        "questionbank",
        "reset",
        "restore",
        "reusecourse",
        "users",
    }
)
_TEACHER_ROLE_NAMES = frozenset(
    {
        "editingteacher",
        "instructor",
        "manager",
        "teacher",
        "teachingassistant",
        "teaching_assistant",
    }
)
_DISABLED_SERVICE_CODES = frozenset(
    {
        "enablewsdescription",
        "servicenotavailable",
        "service_not_available",
        "webservicedisabled",
        "webservicesdisabled",
    }
)
_AUTHENTICATION_CODES = frozenset(
    {
        "invalidlogin",
        "invalidtoken",
        "requirecorrectaccess",
        "usernotconfirmed",
        "usernotexist",
    }
)


@dataclass(frozen=True, slots=True)
class MoodleCourseMembership:
    external_id: str
    title: str
    short_name: str
    role: str


@dataclass(frozen=True, slots=True)
class TokenIdentity:
    token: str
    external_subject: str
    display_name: str
    email: str
    locale: str
    courses: tuple[MoodleCourseMembership, ...]
    functions: frozenset[str]
    upload_files: bool


class MoodleAuthenticationError(IntegrationProtocolError):
    """The Moodle account credentials or issued token were not accepted."""

    code = "MOODLE_AUTHENTICATION_FAILED"


class MoodleWebServicesDisabled(IntegrationConfigurationError):
    """The site's standard mobile web service cannot be used."""

    code = "MOODLE_WEB_SERVICES_DISABLED"


class _MoodleRejected(IntegrationProtocolError):
    def __init__(self, error_code: str) -> None:
        super().__init__("Moodle rejected the web-service operation")
        self.error_code = error_code


class MoodleStandardClient:
    """Bounded client for Moodle's standard ``moodle_mobile_app`` REST service.

    It intentionally does not emulate an interactive browser session.  Every
    operation is limited to functions Moodle reports for the token in
    ``core_webservice_get_site_info``.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        base_url: str | None = None,
        service_token: str | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.base_url = self._normalise_base_url(base_url or settings.moodle_base_url)
        self.service_token = (
            service_token
            if service_token is not None
            else secret_value(settings.moodle_service_token)
        ).strip()
        self.timeout_seconds = float(getattr(settings, "moodle_http_timeout_seconds", 15))
        self.response_limit = int(getattr(settings, "moodle_max_response_bytes", 4 * 1024 * 1024))
        self._identity: TokenIdentity | None = None

        # Redirect-following could send a password or token to a different host.
        if bool(getattr(client, "follow_redirects", False)):
            raise IntegrationConfigurationError("Moodle HTTP client must not follow redirects")

    def recognize_course_url(self, value: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise IntegrationProtocolError("Course URL is invalid") from exc
        if parsed.username or parsed.password or parsed.fragment:
            raise IntegrationProtocolError("Course URL contains unsupported components")
        expected = urlsplit(self.base_url)
        if self._origin(parsed) != self._origin(expected):
            raise IntegrationProtocolError("Course URL origin is not configured")
        expected_path = f"{expected.path.rstrip('/')}/course/view.php" or "/course/view.php"
        if parsed.path.rstrip("/") != expected_path:
            raise IntegrationProtocolError("Only a Moodle course URL is accepted")
        values = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if len(values) != 1:
            raise IntegrationProtocolError("Moodle course id is missing")
        return self._positive_id(values[0], "Moodle course id")

    async def authenticate(self, username: str, password: str) -> TokenIdentity:
        if not isinstance(username, str) or not username.strip() or len(username) > 320:
            raise MoodleAuthenticationError("Moodle credentials were not accepted")
        if not isinstance(password, str) or not password or len(password) > 4096:
            raise MoodleAuthenticationError("Moodle credentials were not accepted")

        result = await request_json_limited(
            self.client,
            "POST",
            self._endpoint("login/token.php"),
            timeout_seconds=self.timeout_seconds,
            response_limit=self.response_limit,
            data={
                "username": username,
                "password": password,
                "service": "moodle_mobile_app",
            },
        )
        if not isinstance(result, dict):
            raise IntegrationProtocolError("Moodle token endpoint returned an invalid response")
        error_code = self._error_code(result)
        if error_code:
            self._raise_authentication_error(error_code)
        token = result.get("token")
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise IntegrationProtocolError("Moodle token endpoint returned an invalid token")

        identity = await self._load_identity(token, fallback_name=username.strip())
        self.service_token = token
        self._identity = identity
        return identity

    async def discover_course(
        self,
        external_id: str,
        actor_external_subject: str,
    ) -> CourseDiscovery:
        course_id = self._positive_id(external_id, "Moodle course id")
        actor_id = self._positive_id(actor_external_subject, "Moodle user id")
        identity = await self._ensure_identity()
        if actor_id != identity.external_subject:
            raise IntegrationProtocolError("Moodle identity does not match the course actor")

        raw_courses = await self._load_courses(identity.token, identity.external_subject)
        raw_course = next(
            (course for course in raw_courses if self._object_id(course) == course_id),
            None,
        )
        if raw_course is None:
            raise IntegrationProtocolError("Moodle did not confirm course membership")

        role, role_roster = await self._resolve_one_role(identity.token, course_id, actor_id)
        if role != "TEACHER":
            raise IntegrationProtocolError("Moodle did not confirm teacher membership")

        contents = await self._required_call(
            "core_course_get_contents", {"courseid": course_id}, token=identity.token
        )
        assignments_result: Any = {"courses": []}
        if self._supports(identity, "mod_assign_get_assignments"):
            assignments_result = await self._required_call(
                "mod_assign_get_assignments",
                {"courseids[0]": course_id},
                token=identity.token,
            )

        members: list[dict[str, Any]] = []
        roster_complete = False
        if self._supports(identity, "core_enrol_get_enrolled_users"):
            try:
                roster = (
                    role_roster
                    if role_roster is not None
                    else await self._call(
                        "core_enrol_get_enrolled_users",
                        {"courseid": course_id},
                        token=identity.token,
                    )
                )
                members = self._parse_roster(roster, actor_id=actor_id, actor_role=role)
                roster_complete = True
            except _MoodleRejected:
                # Some Moodle roles may see a course without permission to list users.
                members = []

        if not members:
            members = [
                {
                    "user_id": actor_id,
                    "display_name": identity.display_name,
                    "email": identity.email,
                    "suspended": False,
                    "role": "TEACHER",
                    "roles": ["TEACHER"],
                    "groups": [],
                }
            ]
        sections = self._parse_sections(contents, assignments_result)
        groups = sorted(
            {
                (str(group["id"]), str(group.get("name", "")))
                for member in members
                for group in member.get("groups", [])
                if isinstance(group, dict) and str(group.get("id", ""))
            }
        )
        membership_snapshot = {"complete": roster_complete, "members": members}
        membership_revision = sha256_hex(canonical_json(membership_snapshot))
        course_projection = {
            "external_id": course_id,
            "title": self._text(raw_course.get("fullname"), 255),
            "short_name": self._text(raw_course.get("shortname"), 120),
            "starts_at_epoch": self._nonnegative_int(raw_course.get("startdate")),
            "ends_at_epoch": self._nonnegative_int(raw_course.get("enddate")),
            "sections": sections,
            "groups": [{"external_id": group_id, "name": name} for group_id, name in groups],
        }
        external_revision = sha256_hex(canonical_json(course_projection))
        preview = {
            **course_projection,
            "external_revision": external_revision,
            "membership_revision": membership_revision,
            "membership_snapshot": membership_snapshot,
        }
        grade_write = self._supports(identity, "mod_assign_save_grade")
        return CourseDiscovery(
            external_id=course_id,
            preview=preview,
            capabilities={
                "roster": roster_complete,
                "groups": roster_complete,
                "grades": grade_write,
                "comments": grade_write,
                "checkpoints": False,
                "task_bank_mirror": False,
                "native_question_bank_write": False,
            },
        )

    async def push_grade(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._idempotency_key(idempotency_key)
        identity = await self._ensure_identity()
        if not self._supports(identity, "mod_assign_save_grade"):
            raise IntegrationConfigurationError(
                "Moodle token does not expose assignment grade writing"
            )

        course_id = self._positive_id(
            self._required(payload, "courseid", "course_id"), "Moodle course id"
        )
        user_id = self._positive_id(self._required(payload, "userid", "user_id"), "Moodle user id")
        assignment_value = payload.get("assignmentid", payload.get("assignment_id"))
        if assignment_value is None:
            cmid = self._positive_id(self._required(payload, "cmid"), "Moodle cmid")
            assignment_id = await self._assignment_id_for_cmid(identity.token, course_id, cmid)
        else:
            assignment_id = self._positive_id(assignment_value, "Moodle assignment id")

        grade = self._grade(self._required(payload, "grade"))
        comment = self._text(payload.get("comment"), 16_000)
        attempt_number = -1
        if "attempt_number" in payload:
            raw_attempt_number = payload.get("attempt_number")
            if (
                isinstance(raw_attempt_number, bool)
                or not isinstance(raw_attempt_number, int)
                or not 0 <= raw_attempt_number <= 1_000_000
            ):
                raise IntegrationProtocolError("Moodle assignment attempt number is invalid")
            attempt_number = raw_attempt_number
        params: dict[str, Any] = {
            "assignmentid": assignment_id,
            "userid": user_id,
            "grade": grade,
            "attemptnumber": attempt_number,
            "addattempt": 0,
            "workflowstate": "",
            "applytoall": 0,
        }
        if comment:
            params.update(
                {
                    "plugindata[assignfeedbackcomments_editor][text]": comment,
                    "plugindata[assignfeedbackcomments_editor][format]": 0,
                }
            )
        await self._required_call(
            "mod_assign_save_grade", params, token=identity.token, allow_null=True
        )
        # Moodle's function sets an absolute grade; repeating this request is safe,
        # although Moodle has no native idempotency-key parameter for it.
        return {
            "status": "DELIVERED",
            "receipt": {
                "function": "mod_assign_save_grade",
                "assignment_id": assignment_id,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            },
        }

    async def store_checkpoint(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        del payload, idempotency_key
        raise IntegrationConfigurationError(
            "Standard Moodle REST checkpoint storage requires an explicit "
            "assignment-submission mapping"
        )

    async def get_latest_checkpoint(
        self,
        *,
        course_id: str = "",
        user_id: str = "",
        attempt_ref: str = "",
    ) -> None:
        del course_id, user_id, attempt_ref
        return None

    async def upsert_task_definition(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        del payload, idempotency_key
        raise IntegrationConfigurationError(
            "Standard Moodle REST cannot mirror task definitions without an explicit "
            "Moodle activity mapping"
        )

    async def _ensure_identity(self) -> TokenIdentity:
        if self._identity is not None and self._identity.token == self.service_token:
            return self._identity
        if not self.service_token:
            raise IntegrationConfigurationError("Moodle service token is not configured")
        if not _TOKEN_RE.fullmatch(self.service_token):
            raise IntegrationConfigurationError("Moodle service token is invalid")
        try:
            identity = await self._load_identity(self.service_token, fallback_name="")
        except _MoodleRejected as exc:
            self._raise_authentication_error(exc.error_code)
        self._identity = identity
        return identity

    async def _load_identity(self, token: str, *, fallback_name: str) -> TokenIdentity:
        try:
            site_info = await self._call("core_webservice_get_site_info", {}, token=token)
        except _MoodleRejected as exc:
            self._raise_authentication_error(exc.error_code)
        if not isinstance(site_info, dict):
            raise IntegrationProtocolError("Moodle site-info response has an invalid shape")

        site_url = site_info.get("siteurl")
        if site_url:
            try:
                returned_base = self._normalise_base_url(str(site_url))
            except IntegrationConfigurationError as exc:
                raise IntegrationProtocolError("Moodle site URL is invalid") from exc
            if returned_base != self.base_url:
                raise IntegrationProtocolError("Moodle site URL does not match the configured site")

        external_subject = self._positive_id(site_info.get("userid"), "Moodle user id")
        functions = self._parse_functions(site_info.get("functions", []))
        # This call itself may be omitted by old/custom service definitions even
        # though it just succeeded, so retain it in the effective function set.
        functions = functions | {"core_webservice_get_site_info"}
        if "core_enrol_get_users_courses" not in functions:
            raise MoodleWebServicesDisabled(
                "Moodle mobile service does not expose enrolled courses"
            )

        raw_courses = await self._load_courses(token, external_subject)
        roles = await self._resolve_roles(token, raw_courses, functions)
        memberships = tuple(
            MoodleCourseMembership(
                external_id=self._object_id(course),
                title=self._text(course.get("fullname"), 255),
                short_name=self._text(course.get("shortname"), 120),
                role=roles.get(self._object_id(course), "STUDENT"),
            )
            for course in raw_courses
        )
        display_name = self._text(site_info.get("fullname"), 255) or fallback_name[:255]
        return TokenIdentity(
            token=token,
            external_subject=external_subject,
            display_name=display_name,
            email=self._text(site_info.get("email"), 320),
            locale=self._text(site_info.get("lang"), 32) or "en",
            courses=memberships,
            functions=frozenset(functions),
            upload_files=self._truthy(site_info.get("uploadfiles", False)),
        )

    async def _load_courses(self, token: str, user_id: str) -> list[dict[str, Any]]:
        result = await self._required_call(
            "core_enrol_get_users_courses",
            {"userid": user_id, "returnusercount": 0},
            token=token,
        )
        if not isinstance(result, list) or len(result) > _MAX_COURSES:
            raise IntegrationProtocolError("Moodle course list has an invalid shape")
        courses: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result:
            if not isinstance(item, dict):
                raise IntegrationProtocolError("Moodle course list has an invalid entry")
            course_id = self._object_id(item)
            if course_id in seen:
                raise IntegrationProtocolError("Moodle course list contains duplicate ids")
            seen.add(course_id)
            courses.append(item)
        return courses

    async def _resolve_roles(
        self,
        token: str,
        courses: list[dict[str, Any]],
        functions: set[str],
    ) -> dict[str, str]:
        course_ids = [self._object_id(course) for course in courses]
        roles = {course_id: "STUDENT" for course_id in course_ids}
        if "core_course_get_user_administration_options" in functions and course_ids:
            params = {f"courseids[{index}]": value for index, value in enumerate(course_ids)}
            try:
                result = await self._call(
                    "core_course_get_user_administration_options", params, token=token
                )
                for course_id, options in self._administration_options(result).items():
                    if course_id in roles and self._options_confirm_teacher(options):
                        roles[course_id] = "TEACHER"
            except _MoodleRejected:
                pass
        return roles

    async def _resolve_one_role(
        self, token: str, course_id: str, actor_id: str
    ) -> tuple[str, Any | None]:
        identity = self._identity
        functions = identity.functions if identity and identity.token == token else frozenset()
        if "core_course_get_user_administration_options" in functions:
            try:
                result = await self._call(
                    "core_course_get_user_administration_options",
                    {"courseids[0]": course_id},
                    token=token,
                )
                options = self._administration_options(result).get(course_id, [])
                if self._options_confirm_teacher(options):
                    return "TEACHER", None
            except _MoodleRejected:
                pass

        # Course discovery is an explicit, infrequent action.  At this point one
        # roster fetch is acceptable and its response is reused as the snapshot;
        # login itself never performs per-course roster requests.
        if "core_enrol_get_enrolled_users" in functions:
            try:
                roster = await self._call(
                    "core_enrol_get_enrolled_users",
                    {"courseid": course_id},
                    token=token,
                )
            except _MoodleRejected:
                return "STUDENT", None
            actor = self._find_roster_actor(roster, actor_id)
            if actor is not None and self._raw_roles_confirm_teacher(actor.get("roles", [])):
                return "TEACHER", roster
            return "STUDENT", roster
        return "STUDENT", None

    async def _assignment_id_for_cmid(self, token: str, course_id: str, cmid: str) -> str:
        contents = await self._required_call(
            "core_course_get_contents", {"courseid": course_id}, token=token
        )
        if not isinstance(contents, list) or len(contents) > _MAX_SECTIONS:
            raise IntegrationProtocolError("Moodle course contents have an invalid shape")
        activity_count = 0
        for section in contents:
            if not isinstance(section, dict):
                continue
            modules = section.get("modules", [])
            if not isinstance(modules, list):
                raise IntegrationProtocolError("Moodle course section has invalid modules")
            activity_count += len(modules)
            if activity_count > _MAX_ACTIVITIES:
                raise IntegrationProtocolError("Moodle course contains too many activities")
            for module in modules:
                if (
                    isinstance(module, dict)
                    and str(module.get("id", "")) == cmid
                    and str(module.get("modname", "")).lower() == "assign"
                ):
                    return self._positive_id(module.get("instance"), "Moodle assignment id")
        raise IntegrationProtocolError("Moodle assignment mapping was not found")

    def _parse_sections(self, contents: Any, assignments_result: Any) -> list[dict[str, Any]]:
        if not isinstance(contents, list) or len(contents) > _MAX_SECTIONS:
            raise IntegrationProtocolError("Moodle course contents have an invalid shape")
        assignments = self._assignment_index(assignments_result)
        sections: list[dict[str, Any]] = []
        activity_count = 0
        for index, raw_section in enumerate(contents):
            if not isinstance(raw_section, dict):
                raise IntegrationProtocolError("Moodle course contents have an invalid entry")
            modules = raw_section.get("modules", [])
            if not isinstance(modules, list):
                raise IntegrationProtocolError("Moodle course section has invalid modules")
            activity_count += len(modules)
            if activity_count > _MAX_ACTIVITIES:
                raise IntegrationProtocolError("Moodle course contains too many activities")
            activities: list[dict[str, Any]] = []
            for module in modules:
                if not isinstance(module, dict):
                    continue
                cmid = self._optional_positive_id(module.get("id"))
                modname = self._text(module.get("modname"), 32).lower()
                if not cmid or not modname or not modname.replace("_", "").isalnum():
                    continue
                instance_id = self._optional_positive_id(module.get("instance")) or "0"
                assignment = assignments.get((cmid, instance_id), {})
                activities.append(
                    {
                        "cmid": int(cmid),
                        "instance_id": int(instance_id),
                        "module": modname,
                        "name": self._text(module.get("name"), 255),
                        "visible": self._truthy(module.get("visible", True)),
                        "uservisible": self._truthy(module.get("uservisible", True)),
                        "url": self._safe_activity_url(module.get("url")),
                        "opens_at": self._nonnegative_int(
                            assignment.get("allowsubmissionsfromdate")
                        ),
                        "due_at": self._nonnegative_int(assignment.get("duedate")),
                        "cutoff_at": self._nonnegative_int(assignment.get("cutoffdate")),
                        "grade_max": self._finite_number(assignment.get("grade")),
                    }
                )
            section_id = self._optional_positive_id(raw_section.get("id")) or str(index)
            sections.append(
                {
                    "external_id": section_id,
                    "title": self._text(raw_section.get("name"), 255),
                    "position": self._nonnegative_int(raw_section.get("section"), default=index),
                    "visible": self._truthy(raw_section.get("visible", True)),
                    "activities": activities,
                }
            )
        return sections

    def _assignment_index(self, result: Any) -> dict[tuple[str, str], dict[str, Any]]:
        if not isinstance(result, dict):
            raise IntegrationProtocolError("Moodle assignment response has an invalid shape")
        raw_courses = result.get("courses", [])
        if not isinstance(raw_courses, list) or len(raw_courses) > _MAX_COURSES:
            raise IntegrationProtocolError("Moodle assignment response has an invalid shape")
        index: dict[tuple[str, str], dict[str, Any]] = {}
        total = 0
        for course in raw_courses:
            if not isinstance(course, dict):
                continue
            raw_assignments = course.get("assignments", [])
            if not isinstance(raw_assignments, list):
                raise IntegrationProtocolError("Moodle assignment list has an invalid shape")
            total += len(raw_assignments)
            if total > _MAX_ASSIGNMENTS:
                raise IntegrationProtocolError("Moodle assignment list is too large")
            for assignment in raw_assignments:
                if not isinstance(assignment, dict):
                    continue
                cmid = self._optional_positive_id(assignment.get("cmid")) or ""
                instance_id = self._optional_positive_id(assignment.get("id")) or ""
                if cmid and instance_id:
                    index[(cmid, instance_id)] = assignment
        return index

    def _parse_roster(self, result: Any, *, actor_id: str, actor_role: str) -> list[dict[str, Any]]:
        if not isinstance(result, list) or len(result) > _MAX_ROSTER_MEMBERS:
            raise IntegrationProtocolError("Moodle roster has an invalid shape")
        members: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result:
            if not isinstance(item, dict):
                raise IntegrationProtocolError("Moodle roster has an invalid entry")
            user_id = self._positive_id(item.get("id"), "Moodle roster user id")
            if user_id in seen:
                raise IntegrationProtocolError("Moodle roster contains duplicate users")
            seen.add(user_id)
            role = (
                "TEACHER" if self._raw_roles_confirm_teacher(item.get("roles", [])) else "STUDENT"
            )
            if user_id == actor_id and actor_role == "TEACHER":
                role = "TEACHER"
            raw_groups = item.get("groups", [])
            if not isinstance(raw_groups, list) or len(raw_groups) > _MAX_GROUPS_PER_MEMBER:
                raise IntegrationProtocolError("Moodle roster groups have an invalid shape")
            groups: list[dict[str, str]] = []
            for group in raw_groups:
                if not isinstance(group, dict):
                    continue
                group_id = self._optional_positive_id(group.get("id"))
                if group_id:
                    groups.append({"id": group_id, "name": self._text(group.get("name"), 255)})
            members.append(
                {
                    "user_id": user_id,
                    "display_name": self._text(item.get("fullname"), 255),
                    "email": self._text(item.get("email"), 320),
                    "suspended": self._truthy(item.get("suspended", False)),
                    "role": role,
                    "roles": [role],
                    "groups": groups,
                }
            )
        return members

    async def _required_call(
        self,
        function: str,
        params: dict[str, Any],
        *,
        token: str,
        allow_null: bool = False,
    ) -> Any:
        try:
            result = await self._call(function, params, token=token)
        except _MoodleRejected as exc:
            if exc.error_code in _DISABLED_SERVICE_CODES:
                raise MoodleWebServicesDisabled(
                    "Moodle mobile web services are not enabled"
                ) from exc
            if exc.error_code in _AUTHENTICATION_CODES:
                raise MoodleAuthenticationError("Moodle token was not accepted") from exc
            raise
        if result is None and allow_null:
            return None
        return result

    async def _call(self, function: str, params: dict[str, Any], *, token: str) -> Any:
        if not _TOKEN_RE.fullmatch(token):
            raise IntegrationConfigurationError("Moodle service token is invalid")
        result = await request_json_limited(
            self.client,
            "POST",
            self._endpoint("webservice/rest/server.php"),
            timeout_seconds=self.timeout_seconds,
            response_limit=self.response_limit,
            data={
                "wstoken": token,
                "moodlewsrestformat": "json",
                "wsfunction": function,
                **params,
            },
        )
        if isinstance(result, dict):
            error_code = self._error_code(result)
            if error_code or result.get("exception"):
                raise _MoodleRejected(error_code or "unknown")
        return result

    def _normalise_base_url(self, value: object) -> str:
        raw = str(value or "").strip()
        try:
            parsed = urlsplit(raw)
            port = parsed.port
        except ValueError as exc:
            raise IntegrationConfigurationError("Moodle base URL is invalid") from exc
        allowed_schemes = {"https", "http"} if self.settings.debug else {"https"}
        if (
            parsed.scheme.lower() not in allowed_schemes
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise IntegrationConfigurationError("Moodle base URL is invalid")
        hostname = parsed.hostname.lower()
        default_port = 443 if parsed.scheme.lower() == "https" else 80
        netloc = hostname if port in {None, default_port} else f"{hostname}:{port}"
        path = "/" + "/".join(part for part in parsed.path.split("/") if part)
        if path == "/":
            path = ""
        return urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))

    def _endpoint(self, relative_path: str) -> str:
        endpoint = f"{self.base_url}/{relative_path.lstrip('/')}"
        expected = urlsplit(self.base_url)
        parsed = urlsplit(endpoint)
        if self._origin(parsed) != self._origin(expected):
            raise IntegrationConfigurationError("Moodle endpoint is invalid")
        return endpoint

    def _safe_activity_url(self, value: object) -> str:
        if not value:
            return ""
        raw = self._text(value, 2000)
        try:
            parsed = urlsplit(raw)
            expected = urlsplit(self.base_url)
        except ValueError:
            return ""
        if self._origin(parsed) != self._origin(expected):
            return ""
        return raw

    @staticmethod
    def _administration_options(result: Any) -> dict[str, list[dict[str, Any]]]:
        entries = result.get("courses", []) if isinstance(result, dict) else result
        if not isinstance(entries, list) or len(entries) > _MAX_COURSES:
            raise IntegrationProtocolError(
                "Moodle administration-options response has an invalid shape"
            )
        output: dict[str, list[dict[str, Any]]] = {}
        for item in entries:
            if not isinstance(item, dict):
                continue
            course_id = item.get("id", item.get("courseid"))
            options = item.get("options", [])
            if (
                isinstance(course_id, int | str)
                and str(course_id).isdigit()
                and isinstance(options, list)
                and len(options) <= 256
            ):
                output[str(course_id)] = [value for value in options if isinstance(value, dict)]
        return output

    @staticmethod
    def _options_confirm_teacher(options: list[dict[str, Any]]) -> bool:
        return any(
            MoodleStandardClient._truthy(option.get("available", False))
            and str(option.get("name", "")).strip().lower() in _TEACHER_ADMIN_OPTIONS
            for option in options
        )

    @staticmethod
    def _raw_roles_confirm_teacher(raw_roles: Any) -> bool:
        if not isinstance(raw_roles, list):
            return False
        for role in raw_roles[:128]:
            if isinstance(role, dict):
                candidates = (role.get("shortname"), role.get("name"))
            else:
                candidates = (role,)
            for candidate in candidates:
                normalised = re.sub(r"[^a-z]", "", str(candidate or "").lower())
                if normalised in {name.replace("_", "") for name in _TEACHER_ROLE_NAMES}:
                    return True
        return False

    @staticmethod
    def _find_roster_actor(result: Any, actor_id: str) -> dict[str, Any] | None:
        if not isinstance(result, list) or len(result) > _MAX_ROSTER_MEMBERS:
            raise IntegrationProtocolError("Moodle roster has an invalid shape")
        return next(
            (
                item
                for item in result
                if isinstance(item, dict) and str(item.get("id", "")) == actor_id
            ),
            None,
        )

    @staticmethod
    def _parse_functions(raw: Any) -> set[str]:
        if not isinstance(raw, list) or len(raw) > _MAX_FUNCTIONS:
            raise IntegrationProtocolError("Moodle function list has an invalid shape")
        functions: set[str] = set()
        for entry in raw:
            name = entry.get("name") if isinstance(entry, dict) else entry
            if (
                isinstance(name, str)
                and 1 <= len(name) <= 128
                and re.fullmatch(r"[a-z0-9_]+", name)
            ):
                functions.add(name)
        return functions

    @staticmethod
    def _error_code(result: dict[str, Any]) -> str:
        value = str(result.get("errorcode", "")).strip().lower()
        return value if _ERROR_CODE_RE.fullmatch(value) else ""

    @staticmethod
    def _raise_authentication_error(error_code: str) -> None:
        if error_code in _DISABLED_SERVICE_CODES:
            raise MoodleWebServicesDisabled("Moodle mobile web services are not enabled")
        raise MoodleAuthenticationError("Moodle credentials were not accepted")

    @staticmethod
    def _supports(identity: TokenIdentity, function: str) -> bool:
        return function in identity.functions

    @staticmethod
    def _object_id(value: dict[str, Any]) -> str:
        return MoodleStandardClient._positive_id(value.get("id"), "Moodle course id")

    @staticmethod
    def _positive_id(value: object, label: str) -> str:
        if isinstance(value, bool):
            raise IntegrationProtocolError(f"{label} is invalid")
        text = str(value or "").strip()
        if not text.isdigit() or int(text) <= 0 or len(text) > 20:
            raise IntegrationProtocolError(f"{label} is invalid")
        return str(int(text))

    @staticmethod
    def _optional_positive_id(value: object) -> str | None:
        try:
            return MoodleStandardClient._positive_id(value, "Moodle id")
        except IntegrationProtocolError:
            return None

    @staticmethod
    def _required(payload: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in payload and payload[name] is not None:
                return payload[name]
        raise IntegrationProtocolError(f"Required Moodle field is missing: {names[-1]}")

    @staticmethod
    def _idempotency_key(value: str) -> None:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 200
            or any(char.isspace() for char in value)
        ):
            raise IntegrationProtocolError("Invalid Moodle idempotency key")

    @staticmethod
    def _grade(value: object) -> str:
        if isinstance(value, bool):
            raise IntegrationProtocolError("Moodle grade is invalid")
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise IntegrationProtocolError("Moodle grade is invalid") from exc
        if not decimal.is_finite() or decimal < 0 or decimal > Decimal("1000000000"):
            raise IntegrationProtocolError("Moodle grade is invalid")
        return format(decimal, "f")

    @staticmethod
    def _finite_number(value: object) -> int | float | None:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        if not math.isfinite(float(value)):
            return None
        return value

    @staticmethod
    def _nonnegative_int(value: object, *, default: int = 0) -> int:
        if isinstance(value, bool):
            return default
        try:
            parsed = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            return default
        return parsed if parsed >= 0 else default

    @staticmethod
    def _truthy(value: object) -> bool:
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return value is True or (isinstance(value, int | float) and value == 1)

    @staticmethod
    def _text(value: object, maximum: int) -> str:
        if value is None:
            return ""
        text = str(value).replace("\x00", "").strip()
        return text[:maximum]

    @staticmethod
    def _origin(value: Any) -> tuple[str, str | None, int | None]:
        try:
            port = value.port
        except ValueError as exc:
            raise IntegrationProtocolError("URL port is invalid") from exc
        if port is None:
            port = 443 if value.scheme == "https" else 80 if value.scheme == "http" else None
        return value.scheme.lower(), value.hostname.lower() if value.hostname else None, port


__all__ = [
    "MoodleAuthenticationError",
    "MoodleCourseMembership",
    "MoodleStandardClient",
    "MoodleWebServicesDisabled",
    "TokenIdentity",
]
