from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings

from ._http import canonical_json, request_json_limited, secret_value
from .errors import IntegrationConfigurationError, IntegrationProtocolError


@dataclass(frozen=True, slots=True)
class RunnerResult:
    external_job_id: str
    status: str
    exit_code: int | None
    exit_reason: str
    stdout: str
    stderr: str
    diagnostics: list[dict[str, Any]]
    metrics: dict[str, Any]
    executor_version: str
    filesystem_policy_version: str
    filesystem_isolated: bool
    network_enabled: bool


class RunnerAdapter:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        clock: Callable[[], float] = time.time,
        nonce_factory: Callable[[], str] | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        self.clock = clock
        self.nonce_factory = nonce_factory or (lambda: secrets.token_urlsafe(24))
        self.timeout_seconds = float(getattr(settings, "runner_http_timeout_seconds", 60))
        self.request_limit = int(
            getattr(
                settings,
                "runner_request_body_max_bytes",
                getattr(settings, "runner_max_request_bytes", 16 * 1024 * 1024),
            )
        )
        self.response_limit = int(
            getattr(
                settings,
                "runner_response_body_max_bytes",
                getattr(settings, "runner_max_response_bytes", 4 * 1024 * 1024),
            )
        )
        self.output_limit = min(
            int(getattr(settings, "runner_max_output_bytes", 1_000_000)),
            4_194_304,
        )

    async def dispatch(
        self,
        *,
        request_id: str,
        profile_id: str,
        files: Sequence[dict[str, Any]],
        stdin: str = "",
        mode: str = "RUN",
        limits: Mapping[str, int] | None = None,
    ) -> RunnerResult:
        normalized_files = self._files(files)
        payload = {
            "schema_version": "1.0",
            "request_id": str(request_id),
            "profile_id": str(profile_id),
            "action": "compile" if mode.upper() == "COMPILE" else "compile_and_run",
            "files": normalized_files,
            "stdin": str(stdin),
        }
        if limits is not None:
            payload["limits"] = self._limits(limits)
        result = await self._signed_json("/v1/jobs", payload)
        return self.normalize(result)

    async def start_interactive(
        self,
        *,
        request_id: str,
        owner_key: str,
        profile_id: str,
        files: Sequence[dict[str, Any]],
        limits: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": "1.0",
            "request_id": str(request_id),
            "owner_key": str(owner_key),
            "profile_id": str(profile_id),
            "files": self._files(files),
        }
        if limits is not None:
            payload["limits"] = self._limits(limits)
        return self._interactive(await self._signed_json("/v1/interactive-sessions", payload))

    async def interactive_state(self, *, session_id: str, owner_key: str) -> dict[str, Any]:
        session_id = self._session_id(session_id)
        return self._interactive(
            await self._signed_json(
                f"/v1/interactive-sessions/{session_id}/state",
                {"owner_key": str(owner_key)},
            )
        )

    async def interactive_input(
        self, *, session_id: str, owner_key: str, text: str
    ) -> dict[str, Any]:
        session_id = self._session_id(session_id)
        if len(text.encode("utf-8")) > 64 * 1024:
            raise IntegrationProtocolError("Interactive input exceeds the configured limit")
        return self._interactive(
            await self._signed_json(
                f"/v1/interactive-sessions/{session_id}/input",
                {"owner_key": str(owner_key), "text": text},
            )
        )

    async def interactive_stop(self, *, session_id: str, owner_key: str) -> dict[str, Any]:
        session_id = self._session_id(session_id)
        return self._interactive(
            await self._signed_json(
                f"/v1/interactive-sessions/{session_id}/stop",
                {"owner_key": str(owner_key)},
            )
        )

    async def interactive_eof(self, *, session_id: str, owner_key: str) -> dict[str, Any]:
        session_id = self._session_id(session_id)
        return self._interactive(
            await self._signed_json(
                f"/v1/interactive-sessions/{session_id}/eof",
                {"owner_key": str(owner_key)},
            )
        )

    async def _signed_json(self, path: str, payload: Mapping[str, Any]) -> Any:
        secret = secret_value(self.settings.runner_shared_secret)
        if not secret:
            raise IntegrationConfigurationError("Runner authentication is not configured")
        body = canonical_json(payload)
        if len(body) > self.request_limit:
            raise IntegrationProtocolError("Runner request exceeds the configured size limit")
        timestamp = str(int(self.clock()))
        nonce = self.nonce_factory()
        canonical = f"{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode("ascii")
        signature = hmac.new(secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()
        return await request_json_limited(
            self.client,
            "POST",
            f"{self._base_endpoint()}{path}",
            timeout_seconds=self.timeout_seconds,
            response_limit=self.response_limit,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "X-Runner-Timestamp": timestamp,
                "X-Runner-Nonce": nonce,
                "X-Runner-Signature": f"v1={signature}",
            },
            content=body,
        )

    def _interactive(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Runner returned an invalid interactive response")
        status = str(value.get("status", ""))
        if status not in {
            "RUNNING",
            "SUCCESS",
            "COMPILE_ERROR",
            "RUNTIME_ERROR",
            "TIME_LIMIT",
            "MEMORY_LIMIT",
            "OUTPUT_LIMIT",
            "WORKSPACE_LIMIT",
            "STOPPED",
            "INFRA_ERROR",
        }:
            raise IntegrationProtocolError("Runner returned an invalid interactive status")
        session_id = self._session_id(str(value.get("session_id", "")))
        stdout = self._bounded_string(value.get("stdout"), limit=self.output_limit)
        stderr = self._bounded_string(value.get("stderr"), limit=self.output_limit)
        diagnostics_raw = value.get("diagnostics", [])
        if not isinstance(diagnostics_raw, list) or len(diagnostics_raw) > 10_000:
            raise IntegrationProtocolError("Runner diagnostics have an invalid shape")
        diagnostics = [self._diagnostic(item) for item in diagnostics_raw]
        duration = self._optional_nonnegative_int(value.get("duration_ms"))
        exit_code = self._optional_exit_code(value.get("exit_code"))
        terminal = value.get("terminal")
        truncated = value.get("output_truncated")
        input_closed = value.get("input_closed", False)
        if (
            not isinstance(terminal, bool)
            or not isinstance(truncated, bool)
            or not isinstance(input_closed, bool)
        ):
            raise IntegrationProtocolError("Runner interactive flags have an invalid shape")
        return {
            "session_id": session_id,
            "status": status,
            "terminal": terminal,
            "exit_code": exit_code,
            "duration_ms": duration or 0,
            "stdout": stdout,
            "stderr": stderr,
            "output_truncated": truncated,
            "input_closed": input_closed,
            "diagnostics": diagnostics,
        }

    @staticmethod
    def _limits(limits: Mapping[str, int]) -> dict[str, int]:
        expected = {"cpu_seconds", "memory_mb"}
        if set(limits) != expected:
            raise IntegrationProtocolError(
                "Runner limits must contain exactly cpu_seconds and memory_mb"
            )
        cpu_seconds = limits["cpu_seconds"]
        memory_mb = limits["memory_mb"]
        if (
            isinstance(cpu_seconds, bool)
            or not isinstance(cpu_seconds, int)
            or not 1 <= cpu_seconds <= 300
            or isinstance(memory_mb, bool)
            or not isinstance(memory_mb, int)
            or not 1 <= memory_mb <= 65_536
        ):
            raise IntegrationProtocolError("Runner limits are outside the supported range")
        return {"cpu_seconds": cpu_seconds, "memory_mb": memory_mb}

    def normalize(self, result: Any) -> RunnerResult:
        if not isinstance(result, dict):
            raise IntegrationProtocolError("Runner returned an invalid response")
        raw_status = str(result.get("status", "INFRA_ERROR")).upper()
        if raw_status in {"QUEUED", "RUNNING"}:
            raise IntegrationProtocolError(
                "Runner returned a non-terminal result from its synchronous endpoint"
            )
        status = (
            "COMPLETED"
            if raw_status in {"SUCCESS", "COMPILED", "COMPLETED", "SUCCEEDED"}
            else "CANCELLED"
            if raw_status == "CANCELLED"
            else "FAILED"
        )
        compilation = self._phase(result.get("compilation"))
        execution = self._phase(result.get("execution"))
        terminal = execution if execution else compilation
        stdout = self._capture_text(terminal.get("stdout") if terminal else "")
        stderr_parts = [
            self._capture_text(compilation.get("stderr") if compilation else ""),
            self._capture_text(execution.get("stderr") if execution else ""),
        ]
        diagnostics_raw = result.get("diagnostics", [])
        if not isinstance(diagnostics_raw, list) or len(diagnostics_raw) > 10_000:
            raise IntegrationProtocolError("Runner diagnostics have an invalid shape")
        diagnostics = [self._diagnostic(item) for item in diagnostics_raw]
        isolation = result.get("isolation") or {}
        profile = result.get("profile") or {}
        if not isinstance(isolation, dict) or not isinstance(profile, dict):
            raise IntegrationProtocolError("Runner metadata has an invalid shape")
        filesystem_isolated = isolation.get("filesystem_isolated")
        if not isinstance(filesystem_isolated, bool):
            raise IntegrationProtocolError("Runner filesystem policy has an invalid shape")
        network_enabled = self._network_enabled(isolation.get("network", "denied"))
        metrics = {
            "compilation_duration_ms": self._optional_nonnegative_int(
                compilation.get("duration_ms") if compilation else None
            ),
            "execution_duration_ms": self._optional_nonnegative_int(
                execution.get("duration_ms") if execution else None
            ),
            "manifest_sha256": self._sha256_or_none(result.get("manifest_sha256")),
            "executable_sha256": self._sha256_or_none(result.get("executable_sha256")),
        }
        return RunnerResult(
            external_job_id=self._bounded_string(result.get("job_id"), limit=255),
            status=status,
            exit_code=self._optional_exit_code(terminal.get("exit_code") if terminal else None),
            exit_reason=self._bounded_string(raw_status, limit=64, fallback="INVALID_RESPONSE"),
            stdout=stdout,
            stderr="\n".join(part for part in stderr_parts if part)[: self.output_limit],
            diagnostics=diagnostics,
            metrics=metrics,
            executor_version=self._bounded_string(
                ":".join(
                    filter(
                        None,
                        [
                            self._bounded_string(isolation.get("executor"), limit=40),
                            self._bounded_string(profile.get("compiler_family"), limit=20),
                            self._bounded_string(profile.get("compiler_version"), limit=40),
                        ],
                    )
                ),
                limit=100,
            ),
            filesystem_policy_version=(
                self._bounded_string(isolation.get("policy_version"), limit=100).strip()
                or "unknown"
            ),
            filesystem_isolated=filesystem_isolated,
            network_enabled=network_enabled,
        )

    def _base_endpoint(self) -> str:
        base = self.settings.runner_url.rstrip("/")
        try:
            parsed = urlsplit(base)
        except ValueError as exc:
            raise IntegrationConfigurationError("Runner URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise IntegrationConfigurationError("Runner URL is invalid")
        return base

    def _endpoint(self) -> str:
        return f"{self._base_endpoint()}/v1/jobs"

    @staticmethod
    def _session_id(value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise IntegrationProtocolError("Runner interactive session id is invalid")
        return value

    @staticmethod
    def _files(files: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
        if not files or len(files) > 128:
            raise IntegrationProtocolError("Runner file count is outside the accepted range")
        normalized = []
        for item in files:
            if not isinstance(item, dict):
                raise IntegrationProtocolError("Runner file has an invalid shape")
            path = item.get("path")
            content = item.get("content")
            if not isinstance(path, str) or not path or not isinstance(content, str):
                raise IntegrationProtocolError("Runner file path/content is invalid")
            normalized.append({"path": path, "content": content})
        return normalized

    @staticmethod
    def _phase(value: Any) -> dict[str, Any]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Runner phase has an invalid shape")
        return value

    def _capture_text(self, value: Any) -> str:
        if isinstance(value, dict):
            value = value.get("text", "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise IntegrationProtocolError("Runner output has an invalid shape")
        return value[: self.output_limit]

    @staticmethod
    def _diagnostic(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise IntegrationProtocolError("Runner diagnostic has an invalid shape")
        location = value.get("range") or {}
        if not isinstance(location, dict):
            raise IntegrationProtocolError("Runner diagnostic range has an invalid shape")
        related = value.get("related", [])
        fix_its = value.get("fix_its", [])
        notes = value.get("notes", [])
        if (
            not isinstance(related, list)
            or not isinstance(fix_its, list)
            or not isinstance(notes, list)
        ):
            raise IntegrationProtocolError("Runner diagnostic details have an invalid shape")
        start_line = RunnerAdapter._optional_positive_int(location.get("start_line"))
        start_column = RunnerAdapter._optional_positive_int(location.get("start_column"))
        end_line = RunnerAdapter._optional_positive_int(location.get("end_line"))
        end_column = RunnerAdapter._optional_positive_int(location.get("end_column"))
        if start_line is not None and end_line is not None and end_line < start_line:
            end_line = None
            end_column = None
        if (
            start_line is not None
            and end_line == start_line
            and start_column is not None
            and end_column is not None
            and end_column < start_column
        ):
            end_column = None
        severity = value.get("severity", "error")
        severity = severity.lower() if isinstance(severity, str) else "error"
        severity = {
            "fatal": "error",
            "fatal error": "error",
            "note": "info",
            "remark": "info",
        }.get(severity, severity)
        if severity not in {"error", "warning", "info"}:
            severity = "error"
        raw_message = value.get("message")
        message = raw_message.strip() if isinstance(raw_message, str) else ""
        if not message:
            message = "Compiler diagnostic did not include a message."
        raw_file = value.get("file")
        file = (
            RunnerAdapter._bounded_string(raw_file, limit=512).strip()
            if isinstance(raw_file, str)
            else ""
        )
        raw_code = value.get("code")
        code = (
            RunnerAdapter._bounded_string(raw_code, limit=100).strip()
            if isinstance(raw_code, str)
            else ""
        )
        return {
            "file": file or None,
            "range": {
                "start_line": start_line,
                "start_column": start_column,
                "end_line": end_line,
                "end_column": end_column,
            },
            "severity": severity,
            "code": code or None,
            "message": message[:20_000],
            "notes": [item[:4_000] for item in notes[:100] if isinstance(item, str)],
            "related": [item for item in related[:100] if isinstance(item, dict)],
            "fix_its": [item for item in fix_its[:100] if isinstance(item, dict)],
        }

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise IntegrationProtocolError("Runner numeric field is invalid")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and re.fullmatch(r"-?[0-9]+", value):
            return int(value)
        raise IntegrationProtocolError("Runner numeric field is invalid")

    @staticmethod
    def _optional_exit_code(value: Any) -> int | None:
        normalized = RunnerAdapter._optional_int(value)
        if normalized is None:
            return None
        if not -(2**31) <= normalized < 2**31:
            raise IntegrationProtocolError("Runner exit code is outside the accepted range")
        return normalized

    @staticmethod
    def _optional_positive_int(value: Any) -> int | None:
        try:
            normalized = RunnerAdapter._optional_int(value)
        except IntegrationProtocolError:
            return None
        if normalized is None or not 1 <= normalized < 2**31:
            return None
        return normalized

    @staticmethod
    def _optional_nonnegative_int(value: Any) -> int | None:
        try:
            normalized = RunnerAdapter._optional_int(value)
        except IntegrationProtocolError:
            return None
        if normalized is None or not 0 <= normalized < 2**63:
            return None
        return normalized

    @staticmethod
    def _bounded_string(value: Any, *, limit: int, fallback: str = "") -> str:
        if not isinstance(value, str):
            return fallback
        return value[:limit]

    @staticmethod
    def _sha256_or_none(value: Any) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", value):
            return None
        return value.lower()

    @staticmethod
    def _network_enabled(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if not isinstance(value, str):
            raise IntegrationProtocolError("Runner network policy has an invalid shape")
        normalized = value.strip().lower()
        if normalized in {"denied", "disabled", "false", "none", "off", "0"}:
            return False
        if normalized in {"host", "enabled", "allowed", "true", "on", "1"}:
            return True
        raise IntegrationProtocolError("Runner network policy is unknown")
