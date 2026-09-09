from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from app.core.config import Settings

from ._http import request_json_limited, secret_value
from .errors import IntegrationConfigurationError, IntegrationProtocolError


@dataclass(frozen=True, slots=True)
class CourseDiscovery:
    external_id: str
    preview: dict[str, Any]
    capabilities: dict[str, Any]


class MoodleBridge:
    """Narrow async client for ``local_programming_bridge`` functions only."""

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
        self.base_url = (base_url or settings.moodle_base_url).rstrip("/")
        self.service_token = (
            service_token
            if service_token is not None
            else secret_value(settings.moodle_service_token)
        )
        self.timeout_seconds = float(getattr(settings, "moodle_http_timeout_seconds", 10))
        self.response_limit = int(getattr(settings, "moodle_max_response_bytes", 4 * 1024 * 1024))

    def recognize_course_url(self, value: str) -> str:
        try:
            expected = urlsplit(self.base_url)
            parsed = urlsplit(value)
        except ValueError as exc:
            raise IntegrationProtocolError("Course URL is invalid") from exc
        if not expected.scheme or not expected.hostname:
            raise IntegrationConfigurationError("Moodle base URL is not configured")
        if parsed.username or parsed.password or parsed.fragment:
            raise IntegrationProtocolError("Course URL contains unsupported components")
        if parsed.scheme != "https" and not self.settings.debug:
            raise IntegrationProtocolError("Course URL must use HTTPS")
        if self._origin(parsed) != self._origin(expected):
            raise IntegrationProtocolError("Course URL origin is not configured")
        if parsed.path.rstrip("/") != "/course/view.php":
            raise IntegrationProtocolError("Only a Moodle course URL is accepted")
        values = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if len(values) != 1 or not values[0].isdigit() or int(values[0]) <= 0:
            raise IntegrationProtocolError("Moodle course id is missing")
        return values[0]

    async def discover_course(
        self,
        external_id: str,
        actor_external_subject: str,
    ) -> CourseDiscovery:
        course_result = await self._call(
            "local_programming_bridge_get_course_snapshot", {"courseid": external_id}
        )
        membership_result = await self._call(
            "local_programming_bridge_get_membership_snapshot", {"courseid": external_id}
        )
        course_payload = self._payload(course_result)
        membership_payload = self._payload(membership_result)
        members = membership_payload.get("members", [])
        if not isinstance(members, list):
            raise IntegrationProtocolError("Moodle membership payload has an invalid shape")
        actor = next(
            (
                member
                for member in members
                if isinstance(member, dict)
                and (
                    str(member.get("user_id", "")) == str(actor_external_subject)
                    or str(member.get("username", "")) == str(actor_external_subject)
                )
            ),
            None,
        )
        if (
            not actor
            or str(actor.get("role", "")).upper() != "TEACHER"
            or bool(actor.get("suspended"))
        ):
            raise IntegrationProtocolError("Moodle did not confirm teacher membership")

        course = course_payload.get("course")
        sections = course_payload.get("sections", [])
        if not isinstance(course, dict) or not isinstance(sections, list):
            raise IntegrationProtocolError("Moodle course payload has an invalid shape")
        groups = sorted(
            {
                (str(group.get("id", "")), str(group.get("name", "")))
                for member in members
                if isinstance(member, dict)
                for group in member.get("groups", [])
                if isinstance(group, dict) and group.get("id") is not None
            }
        )
        preview = {
            "external_id": str(course.get("id", external_id)),
            "title": str(course.get("fullname", "")),
            "short_name": str(course.get("shortname", "")),
            "external_revision": str(course_result.get("revision", "")),
            "membership_revision": str(membership_result.get("revision", "")),
            "starts_at_epoch": int(course.get("startdate") or 0),
            "ends_at_epoch": int(course.get("enddate") or 0),
            "sections": [
                self._section(section) for section in sections if isinstance(section, dict)
            ],
            "groups": [
                {"external_id": external_group_id, "name": name}
                for external_group_id, name in groups
            ],
            "membership_snapshot": membership_payload,
        }
        return CourseDiscovery(
            external_id=str(external_id),
            preview=preview,
            capabilities={
                "roster": True,
                "groups": True,
                "grades": True,
                "comments": True,
                "checkpoints": True,
                "task_bank_mirror": True,
                "native_question_bank_write": False,
            },
        )

    async def discover_roster(self, external_id: str) -> dict[str, Any]:
        result = await self._call(
            "local_programming_bridge_get_membership_snapshot", {"courseid": external_id}
        )
        payload = self._payload(result)
        if not isinstance(payload.get("members", []), list):
            raise IntegrationProtocolError("Moodle roster payload has an invalid shape")
        return {
            "external_id": str(external_id),
            "revision": str(result.get("revision", "")),
            "members": payload.get("members", []),
        }

    async def push_grade(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        self._idempotency_key(idempotency_key)
        result = await self._call(
            "local_programming_bridge_push_grade",
            {
                "courseid": self._required(payload, "courseid", "course_id"),
                "cmid": self._required(payload, "cmid"),
                "userid": self._required(payload, "userid", "user_id"),
                "grade": self._required(payload, "grade"),
                "comment": payload.get("comment", ""),
                "idempotencykey": idempotency_key,
            },
        )
        return {"status": "DELIVERED", "receipt": result}

    async def upsert_task_definition(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Mirror one immutable task version into the connector-owned Moodle table."""

        self._idempotency_key(idempotency_key)
        result = await self._call(
            "local_programming_bridge_upsert_task_definition",
            {
                "courseid": self._required(payload, "courseid", "course_id"),
                "taskref": self._required(payload, "taskref", "task_ref"),
                "versionnum": self._required(payload, "versionnum", "version"),
                "contenthash": self._required(payload, "contenthash", "content_hash"),
                "definitionhash": self._required(payload, "definitionhash", "definition_sha256"),
                "definitionjson": self._required(payload, "definitionjson", "definition_json"),
                "status": self._required(payload, "status"),
                "idempotencykey": idempotency_key,
            },
        )
        return {"status": "DELIVERED", "receipt": result}

    async def get_task_bank_snapshot(self, *, course_id: str) -> dict[str, Any]:
        result = await self._call(
            "local_programming_bridge_get_task_bank_snapshot", {"courseid": course_id}
        )
        payload = self._payload(result)
        tasks = payload.get("tasks")
        if str(payload.get("course_id", "")) != str(course_id) or not isinstance(tasks, list):
            raise IntegrationProtocolError("Moodle task-bank payload has an invalid shape")
        return {
            "course_id": str(course_id),
            "revision": str(result.get("revision", "")),
            "tasks": tasks,
        }

    async def store_checkpoint(
        self,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._idempotency_key(idempotency_key)
        result = await self._call(
            "local_programming_bridge_store_checkpoint",
            {
                "courseid": self._required(payload, "courseid", "course_id"),
                "userid": self._required(payload, "userid", "user_id"),
                "attemptref": self._required(payload, "attemptref", "attempt_ref"),
                "snapshotref": self._required(payload, "snapshotref", "snapshot_ref"),
                "snapshotsha256": self._required(payload, "snapshotsha256", "snapshot_sha256"),
                "eventchainhead": self._required(payload, "eventchainhead", "event_chain_head"),
                "epoch": self._required(payload, "epoch"),
                "workspacerevision": self._required(
                    payload, "workspacerevision", "workspace_revision"
                ),
                "reason": self._required(payload, "reason"),
                "manifestjson": self._required(payload, "manifestjson", "manifest_json"),
                "idempotencykey": idempotency_key,
            },
        )
        return {"status": "DELIVERED", "receipt": result}

    async def get_latest_checkpoint(
        self,
        *,
        course_id: str,
        user_id: str,
        attempt_ref: str,
    ) -> dict[str, Any] | None:
        result = await self._call(
            "local_programming_bridge_get_latest_checkpoint",
            {"courseid": course_id, "userid": user_id, "attemptref": attempt_ref},
        )
        checkpoint_status = str(result.get("status", "")).upper()
        if result.get("found") is False or checkpoint_status == "NOT_FOUND":
            return None
        checkpoint = result.get("checkpoint")
        if checkpoint is None and (result.get("found") is True or checkpoint_status == "FOUND"):
            checkpoint = result
        if checkpoint is None:
            raise IntegrationProtocolError("Moodle checkpoint response has an invalid status")
        if not isinstance(checkpoint, dict):
            raise IntegrationProtocolError("Moodle checkpoint payload has an invalid shape")
        manifest_json = checkpoint.get("manifestjson", checkpoint.get("manifest_json"))
        snapshot_hash = checkpoint.get("snapshotsha256", checkpoint.get("snapshot_sha256"))
        if not isinstance(manifest_json, str) or not isinstance(snapshot_hash, str):
            raise IntegrationProtocolError("Moodle checkpoint manifest/hash is missing")
        actual_hash = hashlib.sha256(manifest_json.encode("utf-8")).hexdigest()
        if not hmac.compare_digest(actual_hash, snapshot_hash.lower()):
            raise IntegrationProtocolError("Moodle checkpoint manifest hash does not match")
        chain_head = checkpoint.get("eventchainhead", checkpoint.get("event_chain_head"))
        if not isinstance(chain_head, str) or (
            chain_head
            and (
                len(chain_head) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in chain_head)
            )
        ):
            raise IntegrationProtocolError("Moodle checkpoint event chain head is invalid")
        for field, alias in (("epoch", "epoch"), ("workspacerevision", "workspace_revision")):
            value = checkpoint.get(field, checkpoint.get(alias))
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntegrationProtocolError(f"Moodle checkpoint {alias} is invalid")
        return checkpoint

    async def _call(self, function: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.base_url or not self.service_token:
            raise IntegrationConfigurationError("Moodle bridge is not configured")
        try:
            parsed = urlsplit(self.base_url)
        except ValueError as exc:
            raise IntegrationConfigurationError("Moodle base URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not self.settings.debug)
        ):
            raise IntegrationConfigurationError("Moodle base URL is invalid")
        endpoint = f"{self.base_url}/webservice/rest/server.php"
        result = await request_json_limited(
            self.client,
            "POST",
            endpoint,
            timeout_seconds=self.timeout_seconds,
            response_limit=self.response_limit,
            data={
                "wstoken": self.service_token,
                "moodlewsrestformat": "json",
                "wsfunction": function,
                **params,
            },
        )
        if not isinstance(result, dict):
            raise IntegrationProtocolError("Moodle bridge returned an invalid response")
        if result.get("exception") or result.get("errorcode"):
            raise IntegrationProtocolError("Moodle bridge rejected the operation")
        return result

    @staticmethod
    def _payload(result: dict[str, Any]) -> dict[str, Any]:
        payload = result.get("payload", result)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise IntegrationProtocolError("Moodle bridge payload is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise IntegrationProtocolError("Moodle bridge payload has an invalid shape")
        return payload

    @staticmethod
    def _section(section: dict[str, Any]) -> dict[str, Any]:
        return {
            "external_id": str(section.get("id", "")),
            "title": str(section.get("name", "")),
            "position": int(section.get("number") or 0),
            "visible": bool(section.get("visible", True)),
            "activities": section.get("activities", []),
        }

    @staticmethod
    def _required(payload: dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in payload and payload[name] is not None:
                return payload[name]
        raise IntegrationProtocolError(f"Required Moodle field is missing: {names[-1]}")

    @staticmethod
    def _idempotency_key(value: str) -> None:
        if not value or len(value) > 200 or any(char.isspace() for char in value):
            raise IntegrationProtocolError("Invalid Moodle idempotency key")

    @staticmethod
    def _origin(value: Any) -> tuple[str, str | None, int | None]:
        try:
            port = value.port
        except ValueError as exc:
            raise IntegrationProtocolError("URL port is invalid") from exc
        if port is None:
            port = 443 if value.scheme == "https" else 80 if value.scheme == "http" else None
        return value.scheme.lower(), value.hostname.lower() if value.hostname else None, port
