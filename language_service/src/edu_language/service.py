from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from .config import Settings
from .models import PROFILES, Query, safe_path


class Unavailable(RuntimeError):
    pass


class Busy(Unavailable):
    pass


class QueryTimeout(Unavailable):
    pass


class ProtocolError(Unavailable):
    pass


_DROP = object()


def safe_result(
    value: Any, root: Path, allowed: set[str], *, field: str | None = None
) -> Any:
    """No server filesystem URI, commands, remote edits or external-file reads."""
    if isinstance(value, list):
        return [
            item
            for child in value
            if (item := safe_result(child, root, allowed)) is not _DROP
        ]
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key in {"command", "data", "codeDescription"}:
                continue
            if key in {"uri", "targetUri"}:
                parsed = urlparse(str(child))
                if parsed.scheme != "file" or parsed.netloc:
                    return _DROP
                try:
                    relative = str(Path(unquote(parsed.path)).relative_to(root))
                    safe_path(relative)
                except ValueError:
                    return _DROP
                if relative not in allowed:
                    return _DROP
                result[key] = relative
            else:
                sanitized = safe_result(child, root, allowed, field=key)
                if sanitized is not _DROP:
                    result[key] = sanitized
        return result
    if isinstance(value, str):
        # Never truncate/rewrite edit text: applying a shortened formatting edit
        # would silently corrupt source. Overall response-size bounds reject it.
        if field in {"message", "documentation", "detail"}:
            return value.replace(str(root) + "/", "")
        return value
    return value


class Session:
    def __init__(self, settings: Settings, profile: str):
        self.settings = settings
        self.profile = profile
        self.root = Path(
            tempfile.mkdtemp(prefix="scope-", dir=settings.work_root)
        ).resolve()
        self.lock = asyncio.Lock()
        self.users = 0
        self.last_used = time.monotonic()
        self.process: asyncio.subprocess.Process | None = None
        self.reader_task: asyncio.Task | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.sequence = 0
        self.files: dict[str, str] = {}
        self.versions: dict[str, int] = {}
        self.diagnostics: dict[str, list[dict]] = {}
        self.diagnostic_versions: dict[str, int] = {}
        self.diagnostics_changed = asyncio.Event()
        self.closed = False

    async def start(self) -> None:
        command = [
            str(
                Path(
                    shutil.which(self.settings.clangd) or self.settings.clangd
                ).resolve()
            ),
            "--enable-config=false",
            "--background-index=false",
            "--clang-tidy=false",
            "--header-insertion=never",
            "--completion-style=detailed",
            "--limit-results=200",
            "--pch-storage=memory",
            "-j=1",
            "--log=error",
            f"--compile-commands-dir={self.root}",
        ]
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.root),
            "TMPDIR": str(self.root),
            "LANG": "C.UTF-8",
            "XDG_CACHE_HOME": str(self.root / ".cache"),
        }
        if not self.settings.test_without_sandbox:
            command = [
                self.settings.sandbox_launcher,
                "--isolation",
                self.settings.isolation,
                str(self.root),
                str(self.settings.process_memory_mb),
                *command,
            ]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            cwd=self.root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
            limit=self.settings.output_limit + 4096,
        )
        self.reader_task = asyncio.create_task(self._read(), name="clangd-rpc-reader")
        await self.rpc(
            "initialize",
            {
                "processId": None,
                "rootUri": self.root.as_uri(),
                "capabilities": {
                    "general": {"positionEncodings": ["utf-16"]},
                    "textDocument": {
                        "completion": {
                            "completionItem": {
                                "snippetSupport": True,
                                "documentationFormat": ["markdown", "plaintext"],
                            }
                        },
                        "hover": {"contentFormat": ["markdown", "plaintext"]},
                        "publishDiagnostics": {"versionSupport": True},
                    },
                },
                "initializationOptions": {"clangdFileStatus": False},
            },
        )
        await self.notify("initialized", {})

    async def _send(self, message: dict) -> None:
        if (
            not self.process
            or self.process.returncode is not None
            or not self.process.stdin
        ):
            raise Unavailable("clangd is not running")
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode()
        self.process.stdin.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
        await self.process.stdin.drain()

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def rpc(self, method: str, params: dict) -> Any:
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
            return await future
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.notify("$/cancelRequest", {"id": request_id})
            raise
        finally:
            self.pending.pop(request_id, None)

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        try:
            while True:
                header = await self.process.stdout.readuntil(b"\r\n\r\n")
                if len(header) > 4096:
                    raise ProtocolError("oversized clangd header")
                lengths = [
                    line.split(b":", 1)[1].strip()
                    for line in header.split(b"\r\n")
                    if line.lower().startswith(b"content-length:")
                ]
                if len(lengths) != 1:
                    raise ProtocolError("invalid clangd framing")
                length = int(lengths[0])
                if not 0 < length <= self.settings.output_limit:
                    raise ProtocolError("oversized clangd message")
                message = json.loads(await self.process.stdout.readexactly(length))
                if not isinstance(message, dict):
                    raise ProtocolError("invalid clangd message")
                if "id" in message and "method" not in message:
                    pending = self.pending.get(message["id"])
                    if pending and not pending.done():
                        if "error" in message:
                            pending.set_exception(
                                ProtocolError("clangd request failed")
                            )
                        else:
                            pending.set_result(message.get("result"))
                elif message.get("method") == "textDocument/publishDiagnostics":
                    params = message.get("params", {})
                    parsed = urlparse(params.get("uri", ""))
                    try:
                        path = str(Path(unquote(parsed.path)).relative_to(self.root))
                    except ValueError:
                        continue
                    version = params.get("version", self.versions.get(path, -1))
                    if path in self.files and version == self.versions.get(path):
                        self.diagnostics[path] = params.get("diagnostics", [])[:100]
                        self.diagnostic_versions[path] = version
                        self.diagnostics_changed.set()
                elif "id" in message:
                    # No client-side command execution or configuration requests.
                    await self._send(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -32601, "message": "Unsupported method"},
                        }
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ProtocolError("clangd connection closed"))
            self.diagnostics_changed.set()

    async def update(self, query: Query) -> None:
        manifest = {file.path: file.content for file in query.files}
        for path in self.files.keys() - manifest.keys():
            await self.notify(
                "textDocument/didClose",
                {"textDocument": {"uri": (self.root / path).as_uri()}},
            )
            (self.root / path).unlink(missing_ok=True)
            self.versions.pop(path, None)
            self.diagnostics.pop(path, None)
            self.diagnostic_versions.pop(path, None)
        language, family, standard = PROFILES[self.profile]
        compiler = (
            ("g++" if language == "cpp" else "gcc")
            if family == "gcc"
            else ("clang++" if language == "cpp" else "clang")
        )
        commands = [
            {
                "directory": str(self.root),
                "file": str(self.root / path),
                "arguments": [
                    f"/usr/bin/{compiler}",
                    f"-std={standard}",
                    "-Wall",
                    "-Wextra",
                    "-pedantic",
                    "-x",
                    "c++" if language == "cpp" else "c",
                    "-I",
                    str(self.root),
                    "-fsyntax-only",
                    str(self.root / path),
                ],
            }
            for path in manifest
        ]
        (self.root / "compile_commands.json").write_text(
            json.dumps(commands), encoding="utf-8"
        )
        # Materialize every header before notifying clangd about dependent sources.
        for path, content in manifest.items():
            if self.files.get(path) == content:
                continue
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.write_text(content, encoding="utf-8")
        previous = self.files
        self.files = manifest
        changed = (
            any(previous.get(path) != content for path, content in manifest.items())
            or previous.keys() != manifest.keys()
        )
        for path, content in manifest.items():
            uri = (self.root / path).as_uri()
            if path not in previous:
                self.versions[path] = 1
                await self.notify(
                    "textDocument/didOpen",
                    {
                        "textDocument": {
                            "uri": uri,
                            "languageId": "cpp" if language == "cpp" else "c",
                            "version": 1,
                            "text": content,
                        }
                    },
                )
            elif changed:
                # Reparse unchanged sources too when their included headers changed.
                self.versions[path] += 1
                self.diagnostics.pop(path, None)
                self.diagnostic_versions.pop(path, None)
                await self.notify(
                    "textDocument/didChange",
                    {
                        "textDocument": {"uri": uri, "version": self.versions[path]},
                        "contentChanges": [{"text": content}],
                        "wantDiagnostics": True,
                    },
                )

    async def query(self, query: Query) -> dict:
        await self.update(query)
        # clangd may return lexical fallback before its first AST is ready.
        # Wait only for this document; later requests reuse its ready AST.
        while self.diagnostic_versions.get(query.path) != self.versions[query.path]:
            if self.reader_task and self.reader_task.done():
                raise Unavailable("clangd is not running")
            self.diagnostics_changed.clear()
            await self.diagnostics_changed.wait()
        params: dict[str, Any] = {
            "textDocument": {"uri": (self.root / query.path).as_uri()}
        }
        if query.position:
            params["position"] = query.position.model_dump()
        context = query.context.model_dump(exclude_none=True) if query.context else {}
        if query.method == "completion":
            params["context"] = {
                "triggerKind": 1,
                **{
                    key: value
                    for key, value in context.items()
                    if key in {"triggerKind", "triggerCharacter"}
                },
            }
        elif query.method == "signatureHelp":
            params["context"] = {
                "triggerKind": 1,
                "isRetrigger": False,
                **{
                    key: value
                    for key, value in context.items()
                    if key in {"triggerKind", "triggerCharacter", "isRetrigger"}
                },
            }
        elif query.method == "references":
            params["context"] = {
                "includeDeclaration": context.get("includeDeclaration", True)
            }
        elif query.method == "formatting":
            params["options"] = {
                "tabSize": context.get("tabSize", 4),
                "insertSpaces": context.get("insertSpaces", True),
            }
        if query.method == "diagnostics":
            result = None
        else:
            result = await self.rpc("textDocument/" + query.method, params)
        if query.method == "completion" and isinstance(result, dict):
            result["items"] = result.get("items", [])[:200]
        allowed = set(self.files)
        result = safe_result(result, self.root, allowed)
        diagnostics = safe_result(
            [
                {"path": path, "diagnostics": values}
                for path, values in self.diagnostics.items()
                if self.diagnostic_versions.get(path) == self.versions.get(path)
            ],
            self.root,
            allowed,
        )
        return {
            "result": None if result is _DROP else result,
            "diagnostics": diagnostics,
        }

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.reader_task:
            self.reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader_task
        if self.process:
            if self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
            # Drain a killed child's bounded pipe so subprocess.wait cannot get
            # stuck behind an already-full StreamReader after a framing error.
            with contextlib.suppress(OSError, TimeoutError):
                await asyncio.wait_for(self.process.communicate(), 2)
        for pending in self.pending.values():
            if not pending.done():
                pending.cancel()
        shutil.rmtree(self.root)


class LanguageService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sessions: dict[str, Session] = {}
        self.pool_lock = asyncio.Lock()
        self.reaper: asyncio.Task | None = None
        self.ready = False

    async def start(self) -> None:
        if self.settings.test_without_sandbox:
            self.ready = bool(shutil.which(self.settings.clangd))
        else:
            process = None
            try:
                process = await asyncio.create_subprocess_exec(
                    self.settings.sandbox_launcher,
                    "--isolation",
                    self.settings.isolation,
                    "--probe",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    env={"PATH": "/usr/bin:/bin"},
                )
                self.ready = await asyncio.wait_for(process.wait(), 3) == 0 and bool(
                    shutil.which(self.settings.clangd)
                )
            except (OSError, asyncio.TimeoutError):
                self.ready = False
                if process and process.returncode is None:
                    process.kill()
                    await process.wait()
        self.reaper = asyncio.create_task(self._reap(), name="clangd-idle-reaper")

    async def _reap(self) -> None:
        while True:
            await asyncio.sleep(min(self.settings.idle_seconds / 2, 30))
            async with self.pool_lock:
                expired = [
                    key
                    for key, session in self.sessions.items()
                    if session.users == 0
                    and not session.lock.locked()
                    and time.monotonic() - session.last_used
                    > self.settings.idle_seconds
                ]
                for key in expired:
                    await self.sessions.pop(key).close()

    async def query(self, query: Query) -> dict:
        if not self.ready:
            raise Unavailable("secure language service is unavailable")
        session: Session | None = None
        acquired = False
        reserved = False
        key = hashlib.sha256(
            (query.workspace_id + ":" + query.build_profile).encode()
        ).hexdigest()
        try:
            async with asyncio.timeout(self.settings.request_seconds):
                async with self.pool_lock:
                    session = self.sessions.get(key)
                    if session and session.users >= 4:
                        raise Busy("workspace is processing another request")
                    if session is None:
                        if len(self.sessions) >= self.settings.max_sessions:
                            idle = [
                                (candidate.last_used, identity)
                                for identity, candidate in self.sessions.items()
                                if candidate.users == 0 and not candidate.lock.locked()
                            ]
                            if not idle:
                                raise Busy("language service is busy")
                            _, identity = min(idle)
                            await self.sessions.pop(identity).close()
                        session = Session(self.settings, query.build_profile)
                        self.sessions[key] = session
                    session.users += 1
                    reserved = True
                await session.lock.acquire()
                acquired = True
                if session.closed:
                    raise Unavailable("language session was closed")
                if session.process is None:
                    await session.start()
                result = await session.query(query)
                if (
                    len(json.dumps(result, ensure_ascii=False).encode())
                    > self.settings.output_limit
                ):
                    raise ProtocolError("language response exceeds limit")
                return result
        except Busy:
            raise
        except BaseException as exc:
            if session is not None and acquired:
                async with self.pool_lock:
                    if self.sessions.get(key) is session:
                        self.sessions.pop(key)
                await asyncio.shield(session.close())
            if isinstance(exc, TimeoutError):
                raise QueryTimeout("language request timed out") from exc
            if isinstance(exc, (OSError, ValueError)):
                raise Unavailable("language service could not process request") from exc
            raise
        finally:
            if session and acquired:
                session.last_used = time.monotonic()
                session.lock.release()
            if session and reserved:
                session.users -= 1

    async def close(self) -> None:
        self.ready = False
        if self.reaper:
            self.reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reaper
        await asyncio.gather(*(session.close() for session in self.sessions.values()))
        self.sessions.clear()
