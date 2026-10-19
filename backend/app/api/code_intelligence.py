from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.attempts import _active_files, _aware, _ensure_quiz_session_active, _owned_attempt
from app.auth.context import CurrentAuth
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.code_intelligence import CodeIntelligenceAdapter
from app.integrations.errors import IntegrationError
from app.models.attempts import Attempt, Snapshot
from app.models.enums import AttemptState
from app.models.review import TeacherExperiment, TeacherExperimentFile
from app.models.tasks import TaskVersion
from app.schemas.code_intelligence import (
    MAX_SOURCE_BYTES,
    CodeIntelligenceFile,
    CodeIntelligenceQuery,
    CodeIntelligenceRead,
    source_path,
)
from app.services.build_profile import effective_attempt_build_profile
from app.services.common import DomainError
from app.services.policy import require_submission_review_access
from app.services.review import require_teacher_experiment_access
from app.services.storage_state import lock_submission_payload, require_submission_payload
from app.services.workspace import ensure_attempt_not_finalized_in_moodle

router = APIRouter(tags=["code-intelligence"])
DB = Annotated[AsyncSession, Depends(get_db)]


async def _bounded_query(request: Request) -> CodeIntelligenceQuery:
    limit = min(
        request.app.state.settings.code_intelligence_request_body_max_bytes, 3 * 1024 * 1024
    )
    raw = bytearray()
    try:
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                if len(raw) + len(chunk) > limit:
                    raise DomainError(
                        413, "CODE_INTELLIGENCE_TOO_LARGE", "Source request is too large"
                    )
                raw.extend(chunk)
    except TimeoutError as exc:
        raise DomainError(
            408, "CODE_INTELLIGENCE_REQUEST_TIMEOUT", "Source upload timed out"
        ) from exc
    try:
        return CodeIntelligenceQuery.model_validate_json(bytes(raw))
    except ValidationError as exc:
        # Do not echo source code in validation errors or request logs.
        raise DomainError(422, "CODE_INTELLIGENCE_INVALID_REQUEST", "Invalid source query") from exc


class _Admission:
    """Process-local front-line guard; the sidecar also limits total sessions.

    Operations on these dictionaries do not await and are atomic on the ASGI
    event loop. No user source is retained, and inactive rate entries are bounded.
    """

    def __init__(self) -> None:
        self.active: dict[uuid.UUID, int] = {}
        self.rates: OrderedDict[uuid.UUID, tuple[float, float]] = OrderedDict()

    @contextmanager
    def acquire(self, principal_id: uuid.UUID) -> Iterator[None]:
        now = time.monotonic()
        tokens, previous = self.rates.pop(principal_id, (24.0, now))
        tokens = min(24.0, tokens + (now - previous) * 8)
        self.rates[principal_id] = (max(0, tokens - 1), now)
        while len(self.rates) > 2048:
            self.rates.popitem(last=False)
        if tokens < 1 or self.active.get(principal_id, 0) >= 4 or sum(self.active.values()) >= 32:
            raise DomainError(
                429, "CODE_INTELLIGENCE_BUSY", "Code intelligence is busy; retry later"
            )
        self.active[principal_id] = self.active.get(principal_id, 0) + 1
        try:
            yield
        finally:
            remaining = self.active[principal_id] - 1
            if remaining:
                self.active[principal_id] = remaining
            else:
                del self.active[principal_id]


async def _authorized_sources(
    db: AsyncSession,
    auth: CurrentAuth,
    payload: CodeIntelligenceQuery,
) -> tuple[Attempt, list[dict[str, str]]]:
    scope = payload.scope
    if scope.kind == "attempt":
        attempt, _assessment, workspace = await _owned_attempt(
            db,
            attempt_id=scope.id,
            principal_id=auth.principal_id,
        )
        ensure_attempt_not_finalized_in_moodle(attempt)
        await _ensure_quiz_session_active(db, attempt)
        deadline = _aware(attempt.deadline_at)
        if attempt.state != AttemptState.ACTIVE.value or (deadline and utcnow() >= deadline):
            raise DomainError(409, "ATTEMPT_READ_ONLY", "Attempt is read-only")
        files = [
            {"path": row.path, "content": row.content}
            for row in await _active_files(db, workspace.id)
        ]
    else:
        experiment = (
            await db.get(TeacherExperiment, scope.id) if scope.kind == "experiment" else None
        )
        if scope.kind == "experiment" and (
            experiment is None
            or experiment.owner_id != auth.principal_id
            or experiment.deleted_at is not None
            or _aware(experiment.expires_at) <= utcnow()
        ):
            raise DomainError(404, "EXPERIMENT_NOT_FOUND", "Teacher experiment was not found")
        access = await require_submission_review_access(
            db,
            principal_id=auth.principal_id,
            submission_id=experiment.submission_id if experiment else scope.id,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
        )
        attempt = access.attempt
        submission = await lock_submission_payload(db, access.submission)
        require_submission_payload(submission)
        if attempt.submission_source == "MOODLE_DELETED":
            raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
        if experiment is not None:
            await require_teacher_experiment_access(
                db,
                submission_id=access.submission.id,
                teacher_id=auth.principal_id,
            )
            rows = await db.scalars(
                select(TeacherExperimentFile).where(
                    TeacherExperimentFile.experiment_id == experiment.id,
                )
            )
            files = [{"path": row.path, "content": row.content} for row in rows]
        else:
            snapshot = await db.get(Snapshot, submission.snapshot_id, populate_existing=True)
            if snapshot is None:
                raise DomainError(404, "SNAPSHOT_MISSING", "Submission snapshot is missing")
            files = [{"path": row["path"], "content": row["content"]} for row in snapshot.files]
    # Data files are not clangd configuration. Never forward .clangd, compiler
    # databases, or browser-supplied paths outside the authorized workspace.
    sources: dict[str, str] = {}
    for row in files:
        try:
            source_path(row["path"])
        except ValueError:
            continue
        sources[row["path"]] = row["content"]
    for draft in payload.files or []:
        if draft.path not in sources:
            raise DomainError(403, "SOURCE_SCOPE_REQUIRED", "File is outside the current workspace")
        sources[draft.path] = draft.content
    if payload.path not in sources:
        raise DomainError(404, "SOURCE_NOT_FOUND", "Source file was not found")
    if (
        len(sources) > 64
        or sum(len(content.encode("utf-8")) for content in sources.values()) > MAX_SOURCE_BYTES
    ):
        raise DomainError(413, "CODE_INTELLIGENCE_TOO_LARGE", "Source workspace is too large")
    try:
        manifest = [
            CodeIntelligenceFile(path=path, content=content).model_dump()
            for path, content in sources.items()
        ]
    except ValidationError as exc:
        raise DomainError(
            422, "CODE_INTELLIGENCE_INVALID_SOURCE", "Unsupported source text"
        ) from exc
    return attempt, manifest


@router.post("/code-intelligence/query", response_model=CodeIntelligenceRead)
async def query_code_intelligence(
    request: Request,
    auth: CurrentAuth,
    db: DB,
    payload: Annotated[CodeIntelligenceQuery, Depends(_bounded_query)],
) -> CodeIntelligenceRead:
    settings = request.app.state.settings
    if not settings.code_intelligence_enabled or not settings.code_intelligence_url:
        raise DomainError(503, "CODE_INTELLIGENCE_UNAVAILABLE", "Code intelligence is unavailable")
    admission = getattr(request.app.state, "code_intelligence_admission", None)
    if admission is None:
        admission = request.app.state.code_intelligence_admission = _Admission()
    with admission.acquire(auth.principal_id):
        attempt, manifest = await _authorized_sources(db, auth, payload)
        version = await db.get(TaskVersion, attempt.assigned_task_version_id)
        if version is None:
            raise DomainError(
                503, "CODE_INTELLIGENCE_UNAVAILABLE", "Build configuration is unavailable"
            )
        profile = await effective_attempt_build_profile(
            db, attempt=attempt, configured_profile=version.build_profile
        )
        upstream: dict[str, Any] = {
            "workspace_id": hashlib.sha256(
                f"{auth.principal_id}:{payload.scope.kind}:{payload.scope.id}".encode("ascii")
            ).hexdigest(),
            "build_profile": profile,
            "files": manifest,
            "method": payload.method,
            "path": payload.path,
        }
        if payload.position is not None:
            upstream["position"] = payload.position.model_dump()
        if payload.context is not None:
            upstream["context"] = payload.context.model_dump(exclude_none=True)
        # This endpoint never persists drafts, compiles/runs programs, or holds a
        # database connection while clangd indexes. Save/submit have their own lanes.
        await db.rollback()
        try:
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                result = await CodeIntelligenceAdapter(settings, client).query(upstream)
        except IntegrationError as exc:
            raise DomainError(
                503, "CODE_INTELLIGENCE_UNAVAILABLE", "Code intelligence is temporarily unavailable"
            ) from exc
        return CodeIntelligenceRead.model_validate(result)
