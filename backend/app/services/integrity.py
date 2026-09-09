from __future__ import annotations

import bisect
import hashlib
import hmac
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from difflib import SequenceMatcher
from itertools import combinations
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.integrations.authorship import AuthorshipResult
from app.models.analysis import (
    AuthorshipAnalysisJob,
    AuthorshipAnalysisResult,
    PlagiarismCase,
    SimilarityAnalysis,
    SimilarityMatch,
)
from app.models.attempts import Attempt, EditEvent, Snapshot, Submission, Workspace
from app.models.courses import Course
from app.models.enums import (
    AnalysisState,
    AuthorshipAnalysisState,
    CourseRole,
    PlagiarismCaseState,
)
from app.models.identity import ExternalPrincipal
from app.models.tasks import Assessment, TaskVersion
from app.services.common import DomainError, audit_row, canonical_hash
from app.services.policy import require_decision_support, require_membership

_KEYWORDS = {
    "alignas",
    "alignof",
    "and",
    "asm",
    "auto",
    "bool",
    "break",
    "case",
    "catch",
    "char",
    "class",
    "const",
    "constexpr",
    "continue",
    "default",
    "delete",
    "do",
    "double",
    "else",
    "enum",
    "explicit",
    "extern",
    "false",
    "float",
    "for",
    "friend",
    "if",
    "inline",
    "int",
    "long",
    "namespace",
    "new",
    "nullptr",
    "operator",
    "private",
    "protected",
    "public",
    "register",
    "return",
    "short",
    "signed",
    "sizeof",
    "static",
    "struct",
    "switch",
    "template",
    "this",
    "throw",
    "true",
    "try",
    "typedef",
    "typename",
    "union",
    "unsigned",
    "using",
    "virtual",
    "void",
    "volatile",
    "while",
}
_TOKEN_RE = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|"
    r'R"[^()\\\s]{0,16}\([\s\S]*?\)[^()\\\s]{0,16}"|'
    r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|'
    r"[A-Za-z_]\w*|(?:0[xX][0-9A-Fa-f]+|\d+(?:\.\d+)?)|"
    r"::|->\*|->|<<=|>>=|==|!=|<=|>=|&&|\|\||\+\+|--|"
    r"\+=|-=|\*=|/=|%=|<<|>>|[{}()\[\];,.?:~+\-*/%&|^!=<>#]"
)


@dataclass(frozen=True, slots=True)
class LexToken:
    normalized: str
    raw: str
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class Fingerprint:
    value: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class PreparedSubmission:
    submission: Submission
    snapshot: Snapshot
    tokens: list[LexToken]
    fingerprints: dict[int, list[Fingerprint]]


def _lex(path: str, source: str) -> list[LexToken]:
    newline_offsets = [match.start() for match in re.finditer("\n", source)]
    result: list[LexToken] = []
    for match in _TOKEN_RE.finditer(source):
        raw = match.group(0)
        if raw.startswith(("//", "/*")):
            continue
        if raw.startswith('"') or raw.startswith('R"'):
            normalized = "STR"
        elif raw.startswith("'"):
            normalized = "CHAR"
        elif raw[0].isalpha() or raw[0] == "_":
            normalized = raw if raw in _KEYWORDS else "ID"
        elif raw[0].isdigit():
            normalized = "NUM"
        else:
            normalized = raw
        line = bisect.bisect_left(newline_offsets, match.start()) + 1
        result.append(LexToken(normalized, raw, path, line))
    return result


def _strip_starter(tokens: list[LexToken], starter: list[LexToken], minimum: int) -> list[LexToken]:
    if not starter or not tokens:
        return tokens
    matcher = SequenceMatcher(
        None,
        [item.normalized for item in tokens],
        [item.normalized for item in starter],
        autojunk=False,
    )
    removed: set[int] = set()
    for block in matcher.get_matching_blocks():
        if block.size >= minimum:
            removed.update(range(block.a, block.a + block.size))
    return [token for index, token in enumerate(tokens) if index not in removed]


def _fingerprints(
    tokens: list[LexToken], shingle_size: int, window_size: int
) -> dict[int, list[Fingerprint]]:
    if len(tokens) < shingle_size:
        return {}
    shingles: list[Fingerprint] = []
    for start in range(len(tokens) - shingle_size + 1):
        raw = "\x1f".join(token.normalized for token in tokens[start : start + shingle_size])
        value = int.from_bytes(hashlib.blake2b(raw.encode(), digest_size=8).digest(), "big")
        shingles.append(Fingerprint(value, start, start + shingle_size - 1))
    if len(shingles) <= window_size:
        selected = [min(enumerate(shingles), key=lambda item: (item[1].value, -item[0]))[1]]
    else:
        selected = []
        prior_index = -1
        for offset in range(len(shingles) - window_size + 1):
            window = shingles[offset : offset + window_size]
            relative, chosen = min(
                enumerate(window),
                key=lambda item: (item[1].value, -item[0]),
            )
            absolute = offset + relative
            if absolute != prior_index:
                selected.append(chosen)
                prior_index = absolute
    by_hash: dict[int, list[Fingerprint]] = {}
    for item in selected:
        by_hash.setdefault(item.value, []).append(item)
    return by_hash


def _fragment(file_content: str, start_line: int, end_line: int) -> str:
    lines = file_content.splitlines()
    start = max(0, start_line - 1)
    return "\n".join(lines[start : min(len(lines), max(start + 1, end_line))][:5])[:1000]


async def _prepare_submission(
    db: AsyncSession,
    submission: Submission,
    *,
    starter_files: list[dict[str, Any]],
    shingle_size: int,
    window_size: int,
    max_bytes: int,
) -> PreparedSubmission:
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        raise DomainError(500, "SNAPSHOT_MISSING", "Submission snapshot is missing")
    total = sum(len(str(file.get("content", "")).encode()) for file in snapshot.files)
    if total > max_bytes:
        raise DomainError(413, "SUBMISSION_TOO_LARGE", "Submission exceeds similarity scan limit")
    starter_by_path = {
        str(item.get("path", "")): _lex(str(item.get("path", "")), str(item.get("content", "")))
        for item in starter_files
    }
    tokens: list[LexToken] = []
    for file in sorted(snapshot.files, key=lambda item: str(item.get("path", ""))):
        path = str(file.get("path", ""))
        file_tokens = _lex(path, str(file.get("content", "")))
        file_tokens = _strip_starter(file_tokens, starter_by_path.get(path, []), shingle_size)
        tokens.extend(file_tokens)
    return PreparedSubmission(
        submission,
        snapshot,
        tokens,
        _fingerprints(tokens, shingle_size, window_size),
    )


def _match_evidence(
    first: PreparedSubmission,
    second: PreparedSubmission,
    shared: set[int],
    limit: int,
) -> list[dict[str, Any]]:
    files_a = {
        str(row.get("path", "")): str(row.get("content", "")) for row in first.snapshot.files
    }
    files_b = {
        str(row.get("path", "")): str(row.get("content", "")) for row in second.snapshot.files
    }
    evidence: list[dict[str, Any]] = []
    for fingerprint_hash in sorted(shared):
        occurrence_a = first.fingerprints[fingerprint_hash][0]
        occurrence_b = second.fingerprints[fingerprint_hash][0]
        token_a_start = first.tokens[occurrence_a.start]
        token_a_end = first.tokens[occurrence_a.end]
        token_b_start = second.tokens[occurrence_b.start]
        token_b_end = second.tokens[occurrence_b.end]
        evidence.append(
            {
                "fingerprint": f"{fingerprint_hash:016x}",
                "normalized_tokens": [
                    token.normalized
                    for token in first.tokens[occurrence_a.start : occurrence_a.end + 1]
                ],
                "a": {
                    "path": token_a_start.path,
                    "start_line": token_a_start.line,
                    "end_line": token_a_end.line,
                    "fragment": _fragment(
                        files_a.get(token_a_start.path, ""), token_a_start.line, token_a_end.line
                    ),
                },
                "b": {
                    "path": token_b_start.path,
                    "start_line": token_b_start.line,
                    "end_line": token_b_end.line,
                    "fragment": _fragment(
                        files_b.get(token_b_start.path, ""), token_b_start.line, token_b_end.line
                    ),
                },
            }
        )
        if len(evidence) >= limit:
            break
    return evidence


async def run_similarity_analysis(
    db: AsyncSession,
    *,
    assessment_id: uuid.UUID,
    teacher_id: uuid.UUID,
    settings: Settings,
    task_version_id: uuid.UUID | None = None,
    minimum_score: Decimal | None = None,
    system_access: bool = False,
) -> SimilarityAnalysis:
    assessment = await db.get(Assessment, assessment_id)
    if assessment is None:
        raise DomainError(404, "ASSESSMENT_NOT_FOUND", "Assessment was not found")
    if not system_access:
        await require_membership(
            db,
            principal_id=teacher_id,
            course_id=assessment.course_id,
            role=CourseRole.TEACHER,
        )
    require_decision_support(assessment)
    if task_version_id is None:
        versions = list(
            (
                await db.scalars(
                    select(TaskVersion)
                    .join(Attempt, Attempt.assigned_task_version_id == TaskVersion.id)
                    .where(Attempt.assessment_id == assessment.id)
                    .distinct()
                )
            ).all()
        )
        if len(versions) != 1:
            raise DomainError(422, "TASK_VERSION_REQUIRED", "Select one task version for this scan")
        task_version = versions[0]
    else:
        task_version = await db.get(TaskVersion, task_version_id)
        belongs = await db.scalar(
            select(Attempt.id).where(
                Attempt.assessment_id == assessment.id,
                Attempt.assigned_task_version_id == task_version_id,
            )
        )
        if task_version is None or belongs is None:
            raise DomainError(
                422, "INVALID_TASK_VERSION", "Task version is not used by this assessment"
            )
    threshold = Decimal(
        str(minimum_score if minimum_score is not None else settings.plagiarism_min_score)
    )
    if threshold < 0 or threshold > 1:
        raise DomainError(
            422, "INVALID_SIMILARITY_THRESHOLD", "Minimum score must be between 0 and 1"
        )
    analysis = SimilarityAnalysis(
        assessment_id=assessment.id,
        task_version_id=task_version.id,
        requested_by_id=teacher_id,
        config={
            "shingle_size": settings.plagiarism_shingle_size,
            "window_size": settings.plagiarism_winnow_window,
            "minimum_score": str(threshold),
            "starter_excluded": True,
        },
        state=AnalysisState.RUNNING.value,
        started_at=utcnow(),
    )
    db.add(analysis)
    await db.flush()
    rows = list(
        (
            await db.execute(
                select(Submission, Attempt)
                .join(Attempt, Attempt.id == Submission.attempt_id)
                .where(
                    Attempt.assessment_id == assessment.id,
                    Attempt.assigned_task_version_id == task_version.id,
                )
                .order_by(Submission.submitted_at.desc())
            )
        ).all()
    )
    latest: dict[uuid.UUID, Submission] = {}
    for submission, attempt in rows:
        latest.setdefault(attempt.principal_id, submission)
    submissions = list(latest.values())
    if len(submissions) > settings.plagiarism_max_submissions:
        analysis.state = AnalysisState.FAILED.value
        analysis.error = "Submission count exceeds configured scan limit"
        raise DomainError(413, "SIMILARITY_SCAN_TOO_LARGE", analysis.error)
    prepared: list[PreparedSubmission] = []
    total_bytes = 0
    for submission in submissions:
        item = await _prepare_submission(
            db,
            submission,
            starter_files=task_version.starter_files,
            shingle_size=settings.plagiarism_shingle_size,
            window_size=settings.plagiarism_winnow_window,
            max_bytes=settings.plagiarism_max_submission_bytes,
        )
        total_bytes += sum(len(str(row.get("content", "")).encode()) for row in item.snapshot.files)
        if total_bytes > settings.plagiarism_max_scan_bytes:
            analysis.state = AnalysisState.FAILED.value
            analysis.error = "Total source size exceeds configured scan limit"
            raise DomainError(413, "SIMILARITY_SCAN_TOO_LARGE", analysis.error)
        prepared.append(item)
    analysis.submission_count = len(prepared)
    for first, second in combinations(prepared, 2):
        analysis.comparison_count += 1
        keys_a = set(first.fingerprints)
        keys_b = set(second.fingerprints)
        shared = keys_a & keys_b
        union = keys_a | keys_b
        score = Decimal(len(shared)) / Decimal(len(union)) if union else Decimal(0)
        if not shared or score < threshold:
            continue
        match = SimilarityMatch(
            analysis_id=analysis.id,
            submission_a_id=first.submission.id,
            submission_b_id=second.submission.id,
            manifest_hash_a=first.snapshot.manifest_hash,
            manifest_hash_b=second.snapshot.manifest_hash,
            score=score.quantize(Decimal("0.000001")),
            fingerprint_count_a=len(keys_a),
            fingerprint_count_b=len(keys_b),
            shared_fingerprint_count=len(shared),
            evidence=_match_evidence(
                first,
                second,
                shared,
                settings.plagiarism_max_evidence,
            ),
        )
        db.add(match)
        await db.flush()
        db.add(PlagiarismCase(match_id=match.id))
        analysis.match_count += 1
    analysis.state = AnalysisState.COMPLETED.value
    analysis.completed_at = utcnow()
    await db.flush()
    return analysis


def _pseudonym(secret: str, label: str, value: object) -> str:
    digest = hmac.new(secret.encode(), f"{label}:{value}".encode(), hashlib.sha256).hexdigest()
    return f"{label}_{digest}"


def _safe_changes(changes: list[Any], file_paths: dict[str, str]) -> list[Any]:
    safe: list[Any] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        item = dict(change)
        file_id = str(item.pop("file_id", ""))
        if file_id:
            item["file_path"] = file_paths.get(file_id, "")
        safe.append(item)
    return safe


async def create_authorship_job(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    teacher_id: uuid.UUID,
    settings: Settings,
    system_access: bool = False,
) -> tuple[AuthorshipAnalysisJob, dict[str, Any]]:
    submission = await db.get(Submission, submission_id)
    if submission is None:
        raise DomainError(404, "SUBMISSION_NOT_FOUND", "Submission was not found")
    attempt = await db.get(Attempt, submission.attempt_id)
    assessment = await db.get(Assessment, attempt.assessment_id) if attempt else None
    course = await db.get(Course, assessment.course_id) if assessment else None
    principal = await db.get(ExternalPrincipal, attempt.principal_id) if attempt else None
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    task_version = (
        await db.get(TaskVersion, attempt.assigned_task_version_id)
        if attempt and attempt.assigned_task_version_id
        else None
    )
    if not all((attempt, assessment, course, principal, snapshot, task_version)):
        raise DomainError(500, "AUTHORSHIP_CONTEXT_MISSING", "Submission evidence is incomplete")
    if not system_access:
        await require_membership(
            db,
            principal_id=teacher_id,
            course_id=course.id,
            role=CourseRole.TEACHER,
        )
    require_decision_support(assessment)
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    if workspace is None:
        raise DomainError(500, "WORKSPACE_MISSING", "Submission workspace is missing")
    events = list(
        (
            await db.scalars(
                select(EditEvent)
                .where(
                    EditEvent.workspace_id == workspace.id,
                    EditEvent.sequence <= snapshot.revision,
                )
                .order_by(EditEvent.sequence)
            )
        ).all()
    )
    secret = settings.authorship_pseudonym_secret.get_secret_value()
    if not secret:
        if not settings.debug:
            raise DomainError(
                503, "AUTHORSHIP_NOT_CONFIGURED", "Authorship pseudonym key is missing"
            )
        secret = settings.secret_key.get_secret_value() + ":authorship-dev"
    file_paths = {str(row.get("id", "")): str(row.get("path", "")) for row in snapshot.files}
    payload = {
        "schema_version": "1.0",
        "pseudonyms": {
            "course": _pseudonym(secret, "course", course.id),
            "assessment": _pseudonym(secret, "assessment", assessment.id),
            "student": _pseudonym(secret, "student", principal.id),
            "submission": _pseudonym(secret, "submission", submission.id),
        },
        "submission": {
            "manifest_hash": snapshot.manifest_hash,
            "snapshot_revision": snapshot.revision,
            "submitted_at": submission.submitted_at.isoformat(),
            "started_at": attempt.started_at.isoformat(),
            "deadline_at": attempt.deadline_at.isoformat() if attempt.deadline_at else None,
            "files": [
                {
                    "path": str(row.get("path", "")),
                    "language": str(row.get("language", "")),
                    "content": str(row.get("content", "")),
                    "content_hash": str(row.get("content_hash", "")),
                }
                for row in snapshot.files
            ],
            "starter_files": [
                {
                    "path": str(row.get("path", "")),
                    "content": str(row.get("content", "")),
                    "content_hash": hashlib.sha256(
                        str(row.get("content", "")).encode()
                    ).hexdigest(),
                }
                for row in task_version.starter_files
            ],
        },
        "edit_history": {
            "event_count": len(events),
            "ordered_by": "sequence",
            "event_chain_head": snapshot.event_chain_head,
            "events": [
                {
                    "sequence": event.sequence,
                    "epoch": event.epoch,
                    "source": event.source,
                    "event_type": event.event_type,
                    "file_path": file_paths.get(str(event.file_id), ""),
                    "changes": _safe_changes(event.changes, file_paths),
                    "previous_hash": event.previous_hash,
                    "event_hash": event.event_hash,
                    "received_at": event.received_at.isoformat(),
                }
                for event in events
            ],
        },
    }
    job = AuthorshipAnalysisJob(
        submission_id=submission.id,
        requested_by_id=teacher_id,
        manifest_hash=snapshot.manifest_hash,
        payload_hash=canonical_hash(payload),
        state=AuthorshipAnalysisState.PENDING.value,
    )
    db.add(job)
    await db.flush()
    return job, payload


async def mark_authorship_running(db: AsyncSession, job_id: uuid.UUID) -> AuthorshipAnalysisJob:
    job = await db.scalar(
        select(AuthorshipAnalysisJob).where(AuthorshipAnalysisJob.id == job_id).with_for_update()
    )
    if job is None:
        raise DomainError(404, "AUTHORSHIP_JOB_NOT_FOUND", "Authorship job was not found")
    job.state = AuthorshipAnalysisState.RUNNING.value
    job.started_at = utcnow()
    await db.flush()
    return job


async def complete_authorship_job(
    db: AsyncSession,
    *,
    job_id: uuid.UUID,
    result: AuthorshipResult | None = None,
    error_code: str = "",
    error: str = "",
    invalid: bool = False,
) -> AuthorshipAnalysisJob:
    job = await db.scalar(
        select(AuthorshipAnalysisJob).where(AuthorshipAnalysisJob.id == job_id).with_for_update()
    )
    if job is None:
        raise DomainError(404, "AUTHORSHIP_JOB_NOT_FOUND", "Authorship job was not found")
    if result is not None:
        if result.manifest_hash != job.manifest_hash:
            invalid = True
            error_code = "MANIFEST_MISMATCH"
            error = "Analyzer result refers to a different source manifest"
        else:
            db.add(
                AuthorshipAnalysisResult(
                    job_id=job.id,
                    manifest_hash=result.manifest_hash,
                    probability=result.probability,
                    confidence=result.confidence,
                    uncertainty=result.uncertainty,
                    analyzer=result.analyzer,
                    model=result.model,
                    calibration=result.calibration,
                    features=result.features,
                    warnings=result.warnings,
                    response_hash=result.response_hash,
                )
            )
            job.state = AuthorshipAnalysisState.COMPLETED.value
    if result is None or invalid:
        job.state = (
            AuthorshipAnalysisState.INVALID.value
            if invalid
            else AuthorshipAnalysisState.FAILED.value
        )
        job.error_code = error_code[:64]
        job.error = error[:4000]
    job.completed_at = utcnow()
    await db.flush()
    return job


async def transition_plagiarism_case(
    db: AsyncSession,
    *,
    case_id: uuid.UUID,
    teacher_id: uuid.UUID,
    state: str,
    teacher_comment: str,
    request_id: str = "",
) -> PlagiarismCase:
    case = await db.scalar(
        select(PlagiarismCase).where(PlagiarismCase.id == case_id).with_for_update()
    )
    if case is None:
        raise DomainError(404, "PLAGIARISM_CASE_NOT_FOUND", "Plagiarism case was not found")
    match = await db.get(SimilarityMatch, case.match_id)
    analysis = await db.get(SimilarityAnalysis, match.analysis_id) if match else None
    assessment = await db.get(Assessment, analysis.assessment_id) if analysis else None
    if assessment is None:
        raise DomainError(500, "PLAGIARISM_CONTEXT_MISSING", "Plagiarism case context is missing")
    await require_membership(
        db,
        principal_id=teacher_id,
        course_id=assessment.course_id,
        role=CourseRole.TEACHER,
    )
    target = state.upper()
    terminal = {
        PlagiarismCaseState.CONFIRMED.value,
        PlagiarismCaseState.DISMISSED.value,
        PlagiarismCaseState.INCONCLUSIVE.value,
    }
    transitions = {
        PlagiarismCaseState.SUSPECTED.value: {
            PlagiarismCaseState.REVIEWING.value,
            *terminal,
        },
        PlagiarismCaseState.REVIEWING.value: terminal,
        **{value: {PlagiarismCaseState.REVIEWING.value} for value in terminal},
    }
    if target not in transitions.get(case.state, set()):
        raise DomainError(
            409, "INVALID_CASE_TRANSITION", "Plagiarism case transition is not allowed"
        )
    if target in terminal and not teacher_comment.strip():
        raise DomainError(422, "CASE_COMMENT_REQUIRED", "A teacher comment is required")
    previous = case.state
    case.state = target
    case.teacher_comment = teacher_comment.strip()
    case.updated_by_id = teacher_id
    case.state_changed_at = utcnow()
    db.add(
        audit_row(
            actor_id=teacher_id,
            action="plagiarism_case.transitioned",
            object_type="PlagiarismCase",
            object_id=case.id,
            course_id=assessment.course_id,
            request_id=request_id,
            metadata={"from": previous, "to": target},
        )
    )
    await db.flush()
    return case
