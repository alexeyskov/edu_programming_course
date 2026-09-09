from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from datetime import timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Header, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.attempts import (
    _effective_flags,
    _store_run_result,
    _unavailable_run_result,
)
from app.auth.context import CurrentAuth
from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.errors import IntegrationError, IntegrationTimeout
from app.integrations.runner import RunnerAdapter
from app.models.attempts import Attempt, RunRequest, RunResult, Snapshot, Submission
from app.models.enums import RunOrigin, RunStatus, TaskVersionStatus
from app.models.evidence import EvidenceReport
from app.models.identity import ExternalPrincipal
from app.models.tasks import Assessment, TaskVersion
from app.schemas.common import validate_source_path
from app.schemas.evidence import EvidenceReportRead, EvidenceRunCreateRequest
from app.schemas.tasks import HiddenTestCase, parse_hidden_test_manifest
from app.services.build_profile import effective_attempt_build_profile
from app.services.common import DomainError, canonical_hash, sha256_text
from app.services.policy import (
    require_decision_support,
    require_review_required,
    require_submission_review_access,
)
from app.services.review import require_owned_active_claim

router = APIRouter(tags=["decision-support", "evidence"])
DB = Annotated[AsyncSession, Depends(get_db)]

_PREVIEW_BYTES = 4_096
_MAX_SNAPSHOT_FILES = 128
_MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024


async def _submission_context(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    allow_system_settings_read: bool = False,
) -> tuple[Submission, Attempt, Assessment, Snapshot, TaskVersion]:
    access = await require_submission_review_access(
        db,
        principal_id=teacher_id,
        submission_id=submission_id,
        allow_system_settings_read=allow_system_settings_read,
    )
    submission = access.submission
    attempt = access.attempt
    assessment = access.assessment
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt is not None and attempt.assigned_task_version_id is not None
        else None
    )
    if attempt is None or assessment is None or snapshot is None or version is None:
        raise DomainError(
            500,
            "SUBMISSION_CONTEXT_MISSING",
            "Submission evidence context is incomplete",
        )
    return submission, attempt, assessment, snapshot, version


def _source_manifest(snapshot: Snapshot) -> list[dict[str, str]]:
    files = snapshot.files
    if not isinstance(files, list) or not 1 <= len(files) <= _MAX_SNAPSHOT_FILES:
        raise DomainError(
            409,
            "INVALID_SUBMISSION_SNAPSHOT",
            "Submission snapshot has an invalid file count",
        )
    try:
        computed_manifest_hash = canonical_hash(files)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DomainError(
            409,
            "INVALID_SUBMISSION_SNAPSHOT",
            "Submission snapshot manifest is not canonical JSON",
        ) from exc
    if not isinstance(snapshot.manifest_hash, str) or not hmac.compare_digest(
        computed_manifest_hash, snapshot.manifest_hash.lower()
    ):
        raise DomainError(
            409,
            "INVALID_SUBMISSION_SNAPSHOT",
            "Submission snapshot manifest hash does not match",
        )
    paths: set[str] = set()
    result: list[dict[str, str]] = []
    aggregate_bytes = 0
    for item in files:
        if not isinstance(item, dict):
            raise DomainError(
                409,
                "INVALID_SUBMISSION_SNAPSHOT",
                "Submission snapshot contains an invalid file",
            )
        path = item.get("path")
        content = item.get("content")
        content_hash = item.get("content_hash")
        try:
            normalized_path = validate_source_path(path) if isinstance(path, str) else ""
        except ValueError as exc:
            raise DomainError(
                409,
                "INVALID_SUBMISSION_SNAPSHOT",
                "Submission snapshot contains an unsafe source path",
            ) from exc
        if not normalized_path or normalized_path in paths or not isinstance(content, str):
            raise DomainError(
                409,
                "INVALID_SUBMISSION_SNAPSHOT",
                "Submission snapshot contains duplicate or invalid source files",
            )
        paths.add(normalized_path)
        try:
            aggregate_bytes += len(content.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise DomainError(
                409,
                "INVALID_SUBMISSION_SNAPSHOT",
                "Submission snapshot is not valid UTF-8",
            ) from exc
        if aggregate_bytes > _MAX_SNAPSHOT_BYTES:
            raise DomainError(
                413,
                "SUBMISSION_SNAPSHOT_TOO_LARGE",
                "Submission snapshot exceeds the evidence-run limit",
            )
        if not isinstance(content_hash, str) or not hmac.compare_digest(
            sha256_text(content), content_hash.lower()
        ):
            raise DomainError(
                409,
                "INVALID_SUBMISSION_SNAPSHOT",
                "Submission snapshot file hash does not match",
            )
        result.append({"path": normalized_path, "content": content})
    return result


def _preview(value: str) -> str:
    encoded = value.encode("utf-8")[:_PREVIEW_BYTES]
    return encoded.decode("utf-8", errors="ignore")


def _is_sha256(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


def _trim_trailing_whitespace(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _outputs_match(case: HiddenTestCase, actual: str) -> bool:
    if case.comparison == "EXACT":
        return hmac.compare_digest(actual.encode("utf-8"), case.expected_stdout.encode("utf-8"))
    return hmac.compare_digest(
        _trim_trailing_whitespace(actual).encode("utf-8"),
        _trim_trailing_whitespace(case.expected_stdout).encode("utf-8"),
    )


def _case_outcome(
    *,
    case_index: int,
    case: HiddenTestCase,
    run: RunRequest,
    result: RunResult,
    infrastructure_error: bool,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    passed = (
        not infrastructure_error
        and run.status == RunStatus.COMPLETED.value
        and result.exit_code == 0
        and _outputs_match(case, result.stdout)
    )
    if infrastructure_error:
        outcome_status = "INFRASTRUCTURE_ERROR"
        finding_code = result.exit_reason or "RUNNER_INTEGRATION_FAILED"
        finding_message = "The runner could not produce evidence for this case."
    elif passed:
        outcome_status = "PASSED"
        finding_code = ""
        finding_message = ""
    elif run.status != RunStatus.COMPLETED.value or result.exit_code != 0:
        outcome_status = "FAILED"
        finding_code = "PROGRAM_DID_NOT_COMPLETE"
        finding_message = "The program did not complete successfully for this hidden case."
    else:
        outcome_status = "FAILED"
        finding_code = "OUTPUT_MISMATCH"
        finding_message = "Program output did not match the hidden expected output."
    outcome = {
        "case_index": case_index,
        "name": case.name,
        "run_id": str(run.id),
        "status": outcome_status,
        "comparison": case.comparison,
        "exit_code": result.exit_code,
        "actual_stdout_sha256": sha256_text(result.stdout),
        "expected_stdout_sha256": sha256_text(case.expected_stdout),
        "actual_stdout_preview": _preview(result.stdout),
        "stderr_preview": _preview(result.stderr),
        "filesystem_isolated": result.filesystem_isolated,
        "network_enabled": result.network_enabled,
    }
    finding = None
    if finding_code:
        finding = {
            "code": finding_code[:100],
            "message": finding_message,
            "case_index": case_index,
            "run_id": str(run.id),
        }
    return outcome, finding


def _is_infrastructure_failure(result: RunResult, error: IntegrationError | None) -> bool:
    if error is not None:
        return True
    return result.exit_reason.upper() in {
        "INFRA_ERROR",
        "INTERNAL_ERROR",
        "INVALID_RESPONSE",
        "NOT_CONFIGURED",
        "RESPONSE_TOO_LARGE",
        "UNAVAILABLE",
    }


async def _fail_report(
    db: AsyncSession,
    *,
    report_id: uuid.UUID,
    outcomes: list[dict[str, Any]],
    findings: list[dict[str, Any]],
    code: str,
    message: str,
) -> EvidenceReport:
    await db.rollback()
    report = await db.scalar(
        select(EvidenceReport).where(EvidenceReport.id == report_id).with_for_update()
    )
    if report is None:
        raise DomainError(500, "EVIDENCE_REPORT_MISSING", "Evidence report disappeared")
    report.status = "FAILED"
    report.passed_cases = sum(row.get("status") == "PASSED" for row in outcomes)
    report.outcomes = outcomes[:20]
    report.findings = findings[:100]
    report.failure_code = code[:100]
    report.failure_message = message[:2_000]
    report.completed_at = utcnow()
    await db.commit()
    return report


async def _persist_report_progress(
    db: AsyncSession,
    *,
    report_id: uuid.UUID,
    outcomes: list[dict[str, Any]],
    findings: list[dict[str, Any]],
) -> EvidenceReport:
    report = await db.scalar(
        select(EvidenceReport).where(EvidenceReport.id == report_id).with_for_update()
    )
    if report is None:
        raise DomainError(500, "EVIDENCE_REPORT_MISSING", "Evidence report disappeared")
    if report.status != "RUNNING":
        raise DomainError(409, "EVIDENCE_REPORT_NOT_RUNNING", "Evidence report is not running")
    report.passed_cases = sum(row.get("status") == "PASSED" for row in outcomes)
    report.outcomes = outcomes[:20]
    report.findings = findings[:100]
    await db.commit()
    return report


async def _recover_stale_reports(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    snapshot_id: uuid.UUID,
    teacher_id: uuid.UUID,
    settings: Settings,
) -> None:
    stale_before = utcnow() - timedelta(seconds=settings.evidence_running_stale_seconds)
    reports = list(
        (
            await db.scalars(
                select(EvidenceReport)
                .where(
                    EvidenceReport.status == "RUNNING",
                    EvidenceReport.updated_at < stale_before,
                    or_(
                        EvidenceReport.requested_by_id == teacher_id,
                        (
                            (EvidenceReport.submission_id == submission_id)
                            & (EvidenceReport.snapshot_id == snapshot_id)
                        ),
                    ),
                )
                .with_for_update()
            )
        ).all()
    )
    if not reports:
        return
    report_ids = [report.id for report in reports]
    runs = list(
        (
            await db.scalars(
                select(RunRequest).where(
                    RunRequest.evidence_report_id.in_(report_ids),
                    RunRequest.status == RunStatus.RUNNING.value,
                )
            )
        ).all()
    )
    existing_result_ids = (
        set(
            (
                await db.scalars(
                    select(RunResult.run_id).where(RunResult.run_id.in_([run.id for run in runs]))
                )
            ).all()
        )
        if runs
        else set()
    )
    for run in runs:
        run.status = RunStatus.FAILED.value
        if run.id not in existing_result_ids:
            db.add(_unavailable_run_result(run.id, "STALE_EVIDENCE_RECOVERED"))
    for report in reports:
        findings = list(report.findings) if isinstance(report.findings, list) else []
        findings.append(
            {
                "code": "STALE_EVIDENCE_RECOVERED",
                "message": "An interrupted evidence run was recovered as a failed report.",
                "case_index": None,
                "run_id": None,
            }
        )
        report.status = "FAILED"
        report.findings = findings[:100]
        report.failure_code = "STALE_EVIDENCE_RECOVERED"
        report.failure_message = "The evidence request did not finish within its durable lease."
        report.completed_at = utcnow()
    await db.commit()


async def _enforce_evidence_budget(
    db: AsyncSession,
    *,
    teacher_id: uuid.UUID,
    settings: Settings,
) -> None:
    principal = await db.scalar(
        select(ExternalPrincipal).where(ExternalPrincipal.id == teacher_id).with_for_update()
    )
    if principal is None:
        raise DomainError(404, "PRINCIPAL_NOT_FOUND", "Teacher was not found")
    active = int(
        await db.scalar(
            select(func.count(EvidenceReport.id)).where(
                EvidenceReport.requested_by_id == teacher_id,
                EvidenceReport.status == "RUNNING",
            )
        )
        or 0
    )
    if active >= settings.evidence_max_concurrent_reports_per_teacher:
        raise DomainError(
            429,
            "EVIDENCE_CONCURRENCY_LIMIT",
            "Too many evidence reports are already running",
            {"limit": settings.evidence_max_concurrent_reports_per_teacher},
        )
    recent_after = utcnow() - timedelta(seconds=settings.evidence_rate_limit_window_seconds)
    recent = int(
        await db.scalar(
            select(func.count(EvidenceReport.id)).where(
                EvidenceReport.requested_by_id == teacher_id,
                EvidenceReport.created_at >= recent_after,
            )
        )
        or 0
    )
    if recent >= settings.evidence_rate_limit_reports:
        raise DomainError(
            429,
            "EVIDENCE_RATE_LIMIT",
            "Evidence report rate limit exceeded",
            {
                "limit": settings.evidence_rate_limit_reports,
                "window_seconds": settings.evidence_rate_limit_window_seconds,
            },
        )


def _idempotency_hash(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or len(normalized.encode("utf-8")) > 200:
        raise DomainError(
            422,
            "INVALID_IDEMPOTENCY_KEY",
            "Idempotency-Key must contain between 1 and 200 UTF-8 bytes",
        )
    return sha256_text(normalized)


@router.post(
    "/submissions/{submission_id}/evidence-runs",
    response_model=EvidenceReportRead,
    status_code=status.HTTP_201_CREATED,
    description=(
        "Run the immutable submission against bounded hidden tests as teacher-only "
        "decision-support evidence. Execution has an independent per-case CPU/wall limit, "
        "a total wall-clock limit of at most 60 seconds, report-level concurrency/rate limits, "
        "durable partial progress, stale recovery, and optional Idempotency-Key replay."
    ),
)
async def create_evidence_run(
    submission_id: uuid.UUID,
    _payload: EvidenceRunCreateRequest,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> EvidenceReportRead:
    system_access = auth.has_capability("SYSTEM_SETTINGS")
    submission, attempt, assessment, snapshot, version = await _submission_context(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=system_access,
    )
    settings: Settings = request.app.state.settings
    request_key_hash = _idempotency_hash(idempotency_key)
    if request_key_hash is not None:
        previous = await db.scalar(
            select(EvidenceReport)
            .where(
                EvidenceReport.submission_id == submission.id,
                EvidenceReport.requested_by_id == auth.principal_id,
                EvidenceReport.idempotency_key_hash == request_key_hash,
            )
            .order_by(EvidenceReport.created_at.desc())
        )
        if previous is not None:
            if previous.status == "RUNNING":
                await _recover_stale_reports(
                    db,
                    submission_id=submission.id,
                    snapshot_id=snapshot.id,
                    teacher_id=auth.principal_id,
                    settings=settings,
                )
                previous = await db.get(EvidenceReport, previous.id)
                if previous is None:
                    raise DomainError(
                        500,
                        "EVIDENCE_REPORT_MISSING",
                        "Evidence report disappeared",
                    )
            return EvidenceReportRead.model_validate(previous)
    if not system_access:
        require_review_required(assessment)
        require_decision_support(assessment)
    await require_owned_active_claim(
        db,
        submission_id=submission.id,
        teacher_id=auth.principal_id,
    )
    flags = await _effective_flags(db, settings)
    if not flags["runner_enabled"]:
        raise DomainError(503, "RUNNER_DISABLED", "Compilation and execution are disabled")
    if settings.runner_mock_enabled:
        raise DomainError(
            503,
            "EVIDENCE_REQUIRES_REAL_RUNNER",
            "Deterministic review evidence cannot be produced by the non-executing runner mock",
        )
    if version.status != TaskVersionStatus.PUBLISHED.value:
        raise DomainError(
            409,
            "TASK_VERSION_NOT_PUBLISHED",
            "Assigned task version is not published",
        )
    try:
        manifest = parse_hidden_test_manifest(version.hidden_test_manifest)
    except ValueError as exc:
        raise DomainError(
            409,
            "INVALID_HIDDEN_TEST_MANIFEST",
            "Published task has an invalid hidden-test manifest",
        ) from exc
    if manifest is None:
        raise DomainError(
            409,
            "HIDDEN_TESTS_NOT_CONFIGURED",
            "This task version has no hidden tests",
        )
    source_manifest = _source_manifest(snapshot)
    build_profile = await effective_attempt_build_profile(
        db,
        attempt=attempt,
        configured_profile=version.build_profile,
    )
    if not _is_sha256(version.content_hash):
        raise DomainError(409, "INVALID_TASK_CONTENT_HASH", "Task content hash is invalid")
    await _recover_stale_reports(
        db,
        submission_id=submission.id,
        snapshot_id=snapshot.id,
        teacher_id=auth.principal_id,
        settings=settings,
    )
    active = await db.scalar(
        select(EvidenceReport).where(
            EvidenceReport.submission_id == submission.id,
            EvidenceReport.snapshot_id == snapshot.id,
            EvidenceReport.status == "RUNNING",
        )
    )
    if active is not None:
        raise DomainError(
            409,
            "EVIDENCE_RUN_IN_PROGRESS",
            "An evidence report is already running for this immutable submission",
            {"report_id": str(active.id)},
        )
    await _enforce_evidence_budget(
        db,
        teacher_id=auth.principal_id,
        settings=settings,
    )
    report = EvidenceReport(
        submission_id=submission.id,
        snapshot_id=snapshot.id,
        task_version_id=version.id,
        requested_by_id=auth.principal_id,
        idempotency_key_hash=request_key_hash,
        hidden_test_manifest_hash=canonical_hash(manifest.model_dump(mode="json")),
        task_content_hash=version.content_hash.lower(),
        snapshot_manifest_hash=snapshot.manifest_hash.lower(),
        status="RUNNING",
        passed_cases=0,
        total_cases=len(manifest.cases),
        outcomes=[],
        findings=[],
    )
    db.add(report)
    # This commit releases the review-claim row lock before the first HTTP call.
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        concurrent = await db.scalar(
            select(EvidenceReport)
            .where(
                EvidenceReport.submission_id == submission.id,
                EvidenceReport.snapshot_id == snapshot.id,
                EvidenceReport.status == "RUNNING",
            )
            .order_by(EvidenceReport.created_at.desc())
        )
        if request_key_hash is not None:
            replay = await db.scalar(
                select(EvidenceReport).where(
                    EvidenceReport.submission_id == submission.id,
                    EvidenceReport.requested_by_id == auth.principal_id,
                    EvidenceReport.idempotency_key_hash == request_key_hash,
                )
            )
            if replay is not None:
                return EvidenceReportRead.model_validate(replay)
        raise DomainError(
            409,
            "EVIDENCE_RUN_IN_PROGRESS",
            "An evidence report is already running for this immutable submission",
            {"report_id": str(concurrent.id) if concurrent else None},
        ) from exc

    outcomes: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    started_at = time.monotonic()
    for case_index, case in enumerate(manifest.cases):
        run = RunRequest(
            origin=RunOrigin.IMMUTABLE_SUBMISSION.value,
            attempt_id=attempt.id,
            submission_id=submission.id,
            evidence_report_id=report.id,
            evidence_case_index=case_index,
            requested_by_id=auth.principal_id,
            revision=snapshot.revision,
            mode="TEST",
            build_profile=build_profile,
            filesystem_profile="UNRESTRICTED_CONTAINER",
            network_enabled=True,
            stdin=case.stdin,
            status=RunStatus.RUNNING.value,
        )
        db.add(run)
        # A durable RUNNING request is committed before dispatch, and no database
        # transaction or row lock is kept open while waiting for the runner.
        await db.commit()

        runner_result = None
        integration_error: IntegrationError | None = None
        try:
            remaining_seconds = settings.evidence_total_timeout_seconds - (
                time.monotonic() - started_at
            )
            if remaining_seconds <= 0:
                raise IntegrationTimeout("Evidence report exceeded its total wall-clock budget")
            wall_timeout = min(settings.evidence_case_timeout_seconds, remaining_seconds)
            async with asyncio.timeout(wall_timeout):
                async with httpx.AsyncClient() as client:
                    runner_result = await RunnerAdapter(settings, client).dispatch(
                        request_id=str(run.id),
                        profile_id=build_profile,
                        files=source_manifest,
                        stdin=case.stdin,
                        mode="TEST",
                        limits={
                            "cpu_seconds": min(
                                flags["runner_cpu_seconds"],
                                settings.evidence_cpu_seconds_per_case,
                            ),
                            "memory_mb": flags["runner_memory_mb"],
                        },
                    )
        except TimeoutError:
            integration_error = IntegrationTimeout("Evidence case exceeded its wall-clock budget")
        except IntegrationError as exc:
            integration_error = exc

        try:
            run = await _store_run_result(
                db,
                run_id=run.id,
                result=runner_result,
                error=integration_error,
            )
            stored = await db.scalar(select(RunResult).where(RunResult.run_id == run.id))
            if stored is None:
                raise DomainError(500, "RUN_RESULT_MISSING", "Evidence run result is missing")
            infrastructure_error = _is_infrastructure_failure(stored, integration_error)
            outcome, finding = _case_outcome(
                case_index=case_index,
                case=case,
                run=run,
                result=stored,
                infrastructure_error=infrastructure_error,
            )
            outcomes.append(outcome)
            if finding is not None:
                findings.append(finding)
            if infrastructure_error:
                code = integration_error.code if integration_error else "RUNNER_UNAVAILABLE"
                failed = await _fail_report(
                    db,
                    report_id=report.id,
                    outcomes=outcomes,
                    findings=findings,
                    code=code,
                    message=("The runner could not complete all hidden cases."),
                )
                return EvidenceReportRead.model_validate(failed)
            await _persist_report_progress(
                db,
                report_id=report.id,
                outcomes=outcomes,
                findings=findings,
            )
        except Exception as exc:
            await _fail_report(
                db,
                report_id=report.id,
                outcomes=outcomes,
                findings=findings,
                code="EVIDENCE_PROCESSING_FAILED",
                message="Evidence processing failed before all hidden cases completed.",
            )
            raise exc

    report = await db.scalar(
        select(EvidenceReport).where(EvidenceReport.id == report.id).with_for_update()
    )
    if report is None:
        raise DomainError(500, "EVIDENCE_REPORT_MISSING", "Evidence report disappeared")
    report.status = "COMPLETED"
    report.passed_cases = sum(row["status"] == "PASSED" for row in outcomes)
    report.outcomes = outcomes
    report.findings = findings
    report.failure_code = ""
    report.failure_message = ""
    report.completed_at = utcnow()
    await db.commit()
    return EvidenceReportRead.model_validate(report)


@router.get(
    "/submissions/{submission_id}/evidence-runs",
    response_model=list[EvidenceReportRead],
)
async def list_evidence_runs(
    submission_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> list[EvidenceReportRead]:
    await _submission_context(
        db,
        submission_id=submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    reports = list(
        (
            await db.scalars(
                select(EvidenceReport)
                .where(EvidenceReport.submission_id == submission_id)
                .order_by(EvidenceReport.created_at.desc(), EvidenceReport.id)
                .offset(offset)
                .limit(limit)
            )
        ).all()
    )
    return [EvidenceReportRead.model_validate(report) for report in reports]


@router.get("/evidence-runs/{report_id}", response_model=EvidenceReportRead)
async def get_evidence_run(
    report_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
) -> EvidenceReportRead:
    report = await db.get(EvidenceReport, report_id)
    if report is None:
        raise DomainError(404, "EVIDENCE_REPORT_NOT_FOUND", "Evidence report was not found")
    await _submission_context(
        db,
        submission_id=report.submission_id,
        teacher_id=auth.principal_id,
        allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
    )
    return EvidenceReportRead.model_validate(report)


__all__ = ["router"]
