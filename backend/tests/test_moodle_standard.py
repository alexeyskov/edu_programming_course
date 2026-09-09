from __future__ import annotations

from urllib.parse import parse_qs

import httpx
import pytest

from app.core.config import Settings
from app.integrations.errors import IntegrationConfigurationError, IntegrationProtocolError
from app.integrations.moodle_standard import (
    MoodleAuthenticationError,
    MoodleStandardClient,
    MoodleWebServicesDisabled,
)

TOKEN = "mobile-token-1234567890"


def configured_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "debug": False,
        "secret_key": "test-secret-" + "x" * 40,
        "moodle_base_url": "https://moodle.example.edu",
        "moodle_service_token": "",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def form(request: httpx.Request) -> dict[str, list[str]]:
    return parse_qs(request.content.decode(), keep_blank_values=True)


def site_info(*functions: str) -> dict[str, object]:
    return {
        "userid": 7,
        "fullname": "Ada Teacher",
        "email": "ada@example.edu",
        "lang": "ru",
        "siteurl": "https://moodle.example.edu/",
        "uploadfiles": 1,
        "functions": [{"name": name} for name in functions],
    }


@pytest.mark.asyncio
async def test_authenticate_builds_identity_and_uses_least_privilege_roles() -> None:
    calls: list[tuple[str, dict[str, list[str]]]] = []
    functions = (
        "core_enrol_get_users_courses",
        "core_course_get_user_administration_options",
        "core_enrol_get_enrolled_users",
        "core_course_get_contents",
        "mod_assign_get_assignments",
        "mod_assign_save_grade",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        posted = form(request)
        calls.append((request.url.path, posted))
        if request.url.path == "/login/token.php":
            return httpx.Response(200, json={"token": TOKEN})
        function = posted["wsfunction"][0]
        if function == "core_webservice_get_site_info":
            return httpx.Response(200, json=site_info(*functions))
        if function == "core_enrol_get_users_courses":
            assert posted["userid"] == ["7"]
            assert posted["returnusercount"] == ["0"]
            return httpx.Response(
                200,
                json=[
                    {"id": 549, "fullname": "C++", "shortname": "CPP"},
                    {"id": 550, "fullname": "Algorithms", "shortname": "ALG"},
                ],
            )
        if function == "core_course_get_user_administration_options":
            return httpx.Response(
                200,
                json=[
                    {"id": 549, "options": [{"name": "editsettings", "available": True}]},
                    # Unknown options never elevate a user to teacher.
                    {"id": 550, "options": [{"name": "custom_unknown", "available": True}]},
                ],
            )
        raise AssertionError(function)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        identity = await MoodleStandardClient(configured_settings(), client).authenticate(
            "ada", "correct horse battery staple"
        )

    assert identity.external_subject == "7"
    assert identity.display_name == "Ada Teacher"
    assert identity.email == "ada@example.edu"
    assert identity.locale == "ru"
    assert identity.upload_files is True
    assert identity.token == TOKEN
    assert [(course.external_id, course.role) for course in identity.courses] == [
        ("549", "TEACHER"),
        ("550", "STUDENT"),
    ]
    assert "mod_assign_save_grade" in identity.functions
    assert calls[0][0] == "/login/token.php"
    assert calls[0][1] == {
        "username": ["ada"],
        "password": ["correct horse battery staple"],
        "service": ["moodle_mobile_app"],
    }
    assert all("password" not in payload for _, payload in calls[1:])
    assert all(payload.get("wstoken") == [TOKEN] for _, payload in calls[1:])
    assert [payload["wsfunction"][0] for _, payload in calls[1:]] == [
        "core_webservice_get_site_info",
        "core_enrol_get_users_courses",
        "core_course_get_user_administration_options",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_code", "exception_type", "expected_message"),
    [
        ("invalidlogin", MoodleAuthenticationError, "credentials were not accepted"),
        (
            "webservicesdisabled",
            MoodleWebServicesDisabled,
            "web services are not enabled",
        ),
    ],
)
async def test_authenticate_has_safe_distinct_failure_semantics(
    error_code: str,
    exception_type: type[Exception],
    expected_message: str,
) -> None:
    username = "secret-user"
    password = "secret-password"

    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "error": f"server echoed {username} and {password}",
                "errorcode": error_code,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(exception_type) as captured:
            await MoodleStandardClient(configured_settings(), client).authenticate(
                username, password
            )

    public_error = str(captured.value)
    assert expected_message in public_error
    assert username not in public_error
    assert password not in public_error


@pytest.mark.asyncio
async def test_base_url_and_returned_site_url_are_strict() -> None:
    async with httpx.AsyncClient() as client:
        with pytest.raises(IntegrationConfigurationError):
            MoodleStandardClient(
                configured_settings(moodle_base_url="http://moodle.example.edu"), client
            )

    async def handler(request: httpx.Request) -> httpx.Response:
        posted = form(request)
        if request.url.path == "/login/token.php":
            return httpx.Response(200, json={"token": TOKEN})
        assert posted["wsfunction"] == ["core_webservice_get_site_info"]
        payload = site_info("core_enrol_get_users_courses")
        payload["siteurl"] = "https://moodle.example.edu.evil.test"
        return httpx.Response(200, json=payload)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationProtocolError, match="does not match"):
            await MoodleStandardClient(configured_settings(), client).authenticate("ada", "pw")

    async with httpx.AsyncClient(follow_redirects=True) as client:
        with pytest.raises(IntegrationConfigurationError, match="must not follow redirects"):
            MoodleStandardClient(configured_settings(), client)


@pytest.mark.asyncio
async def test_discover_course_projects_assignments_and_roster() -> None:
    functions = (
        "core_enrol_get_users_courses",
        "core_course_get_user_administration_options",
        "core_enrol_get_enrolled_users",
        "core_course_get_contents",
        "mod_assign_get_assignments",
        "mod_assign_save_grade",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        posted = form(request)
        if request.url.path == "/login/token.php":
            return httpx.Response(200, json={"token": TOKEN})
        function = posted["wsfunction"][0]
        if function == "core_webservice_get_site_info":
            return httpx.Response(200, json=site_info(*functions))
        if function == "core_enrol_get_users_courses":
            assert posted["userid"] == ["7"]
            assert posted["returnusercount"] == ["0"]
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 549,
                        "fullname": "Programming in C++",
                        "shortname": "CPP",
                        "startdate": 100,
                        "enddate": 200,
                    }
                ],
            )
        if function == "core_course_get_user_administration_options":
            return httpx.Response(
                200,
                json=[{"id": 549, "options": [{"name": "editsettings", "available": 1}]}],
            )
        if function == "core_course_get_contents":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 10,
                        "section": 1,
                        "name": "Laboratory work",
                        "visible": 1,
                        "modules": [
                            {
                                "id": 44,
                                "instance": 91,
                                "modname": "assign",
                                "name": "Lab 1",
                                "visible": 1,
                                "uservisible": 1,
                                "url": "https://moodle.example.edu/mod/assign/view.php?id=44",
                            }
                        ],
                    }
                ],
            )
        if function == "mod_assign_get_assignments":
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {
                            "id": 549,
                            "assignments": [
                                {
                                    "id": 91,
                                    "cmid": 44,
                                    "allowsubmissionsfromdate": 110,
                                    "duedate": 180,
                                    "cutoffdate": 190,
                                    "grade": 10,
                                }
                            ],
                        }
                    ]
                },
            )
        if function == "core_enrol_get_enrolled_users":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "fullname": "Ada Teacher",
                        "email": "ada@example.edu",
                        "roles": [{"shortname": "editingteacher"}],
                        "groups": [{"id": 3, "name": "Group A"}],
                    },
                    {
                        "id": 8,
                        "fullname": "Sam Student",
                        "roles": [{"shortname": "student"}],
                        "groups": [{"id": 3, "name": "Group A"}],
                    },
                ],
            )
        raise AssertionError(function)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = MoodleStandardClient(configured_settings(), client)
        identity = await bridge.authenticate("ada", "pw")
        result = await bridge.discover_course("549", identity.external_subject)

    assert result.external_id == "549"
    assert result.preview["title"] == "Programming in C++"
    assert len(result.preview["external_revision"]) == 64
    assert len(result.preview["membership_revision"]) == 64
    assert result.preview["groups"] == [{"external_id": "3", "name": "Group A"}]
    assert result.preview["membership_snapshot"]["complete"] is True
    assert [member["role"] for member in result.preview["membership_snapshot"]["members"]] == [
        "TEACHER",
        "STUDENT",
    ]
    activity = result.preview["sections"][0]["activities"][0]
    assert activity == {
        "cmid": 44,
        "instance_id": 91,
        "module": "assign",
        "name": "Lab 1",
        "visible": True,
        "uservisible": True,
        "url": "https://moodle.example.edu/mod/assign/view.php?id=44",
        "opens_at": 110,
        "due_at": 180,
        "cutoff_at": 190,
        "grade_max": 10,
    }
    assert result.capabilities == {
        "roster": True,
        "groups": True,
        "grades": True,
        "comments": True,
        "checkpoints": False,
        "task_bank_mirror": False,
        "native_question_bank_write": False,
    }


@pytest.mark.asyncio
async def test_discovery_uses_one_roster_as_teacher_fallback_and_snapshot() -> None:
    roster_calls = 0
    functions = (
        "core_enrol_get_users_courses",
        "core_course_get_user_administration_options",
        "core_enrol_get_enrolled_users",
        "core_course_get_contents",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal roster_calls
        posted = form(request)
        if request.url.path == "/login/token.php":
            return httpx.Response(200, json={"token": TOKEN})
        function = posted["wsfunction"][0]
        if function == "core_webservice_get_site_info":
            return httpx.Response(200, json=site_info(*functions))
        if function == "core_enrol_get_users_courses":
            assert posted["returnusercount"] == ["0"]
            return httpx.Response(
                200,
                json=[{"id": 549, "fullname": "C++", "shortname": "CPP"}],
            )
        if function == "core_course_get_user_administration_options":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 549,
                        "options": [{"name": "unknown_site_option", "available": 1}],
                    }
                ],
            )
        if function == "core_enrol_get_enrolled_users":
            roster_calls += 1
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 7,
                        "fullname": "Ada Teacher",
                        "roles": [{"shortname": "editingteacher"}],
                        "groups": [],
                    }
                ],
            )
        if function == "core_course_get_contents":
            return httpx.Response(200, json=[])
        raise AssertionError(function)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = MoodleStandardClient(configured_settings(), client)
        identity = await bridge.authenticate("ada", "pw")
        assert identity.courses[0].role == "STUDENT"
        result = await bridge.discover_course("549", "7")

    assert roster_calls == 1
    assert result.capabilities["roster"] is True
    assert result.preview["membership_snapshot"]["members"][0]["role"] == "TEACHER"


@pytest.mark.asyncio
@pytest.mark.parametrize(("attempt_number", "expected_attempt_number"), [(None, "-1"), (2, "2")])
async def test_push_grade_resolves_assignment_instance_and_sends_absolute_grade(
    attempt_number: int | None,
    expected_attempt_number: str,
) -> None:
    captured_grade: dict[str, list[str]] = {}
    functions = (
        "core_enrol_get_users_courses",
        "core_course_get_contents",
        "mod_assign_save_grade",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_grade
        posted = form(request)
        if request.url.path == "/login/token.php":
            return httpx.Response(200, json={"token": TOKEN})
        function = posted["wsfunction"][0]
        if function == "core_webservice_get_site_info":
            return httpx.Response(200, json=site_info(*functions))
        if function == "core_enrol_get_users_courses":
            assert posted["userid"] == ["7"]
            assert posted["returnusercount"] == ["0"]
            return httpx.Response(200, json=[])
        if function == "core_course_get_contents":
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 1,
                        "modules": [{"id": 44, "instance": 91, "modname": "assign", "name": "Lab"}],
                    }
                ],
            )
        if function == "mod_assign_save_grade":
            captured_grade = posted
            return httpx.Response(
                200,
                content=b"null",
                headers={"content-type": "application/json"},
            )
        raise AssertionError(function)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = MoodleStandardClient(configured_settings(), client)
        await bridge.authenticate("ada", "pw")
        payload = {
            "course_id": "549",
            "cmid": 44,
            "user_id": "8",
            "grade": "9.50",
            "comment": "Checked",
        }
        if attempt_number is not None:
            payload["attempt_number"] = attempt_number
        result = await bridge.push_grade(payload, "grade-549-8-v1")

    assert result["receipt"]["assignment_id"] == "91"
    assert captured_grade["assignmentid"] == ["91"]
    assert captured_grade["userid"] == ["8"]
    assert captured_grade["grade"] == ["9.50"]
    assert captured_grade["attemptnumber"] == [expected_attempt_number]
    assert captured_grade["plugindata[assignfeedbackcomments_editor][text]"] == ["Checked"]
    assert "idempotencykey" not in captured_grade


@pytest.mark.asyncio
async def test_unsupported_writes_fail_closed_without_network_calls() -> None:
    requests = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        raise AssertionError("network must not be used")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bridge = MoodleStandardClient(configured_settings(), client)
        with pytest.raises(IntegrationConfigurationError, match="checkpoint storage"):
            await bridge.store_checkpoint({}, "checkpoint-key")
        assert await bridge.get_latest_checkpoint() is None
        with pytest.raises(IntegrationConfigurationError, match="task definitions"):
            await bridge.upsert_task_definition({}, "task-key")

    assert requests == 0


@pytest.mark.asyncio
async def test_authentication_rejects_unbounded_course_list() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        posted = form(request)
        if request.url.path == "/login/token.php":
            return httpx.Response(200, json={"token": TOKEN})
        function = posted["wsfunction"][0]
        if function == "core_webservice_get_site_info":
            return httpx.Response(200, json=site_info("core_enrol_get_users_courses"))
        assert function == "core_enrol_get_users_courses"
        assert posted["userid"] == ["7"]
        assert posted["returnusercount"] == ["0"]
        return httpx.Response(200, json=[{"id": index + 1} for index in range(257)])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationProtocolError, match="course list"):
            await MoodleStandardClient(configured_settings(), client).authenticate("ada", "pw")
