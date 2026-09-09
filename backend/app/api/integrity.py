from __future__ import annotations

import uuid
from collections.abc import Iterable
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.context import CurrentAuth
from app.core.config import Settings
from app.db.base import utcnow
from app.db.session import get_db
from app.integrations.authorship import AuthorshipTransport
from app.integrations.errors import IntegrationError, IntegrationProtocolError
from app.models.analysis import (
    AuthorshipAnalysisJob,
    AuthorshipAnalysisResult,
    PlagiarismCase,
    SimilarityAnalysis,
    SimilarityMatch,
)
from app.models.attempts import Attempt, Snapshot, Submission
from app.models.courses import (
    Course,
    CourseGroup,
    CourseMembership,
    CourseMembershipGroup,
)
from app.models.enums import AuthorshipAnalysisState, CourseRole
from app.models.identity import ExternalPrincipal
from app.models.tasks import Assessment
from app.schemas.integrity import (
    AuthorshipAnalysisRead,
    AuthorshipAnalysisResultRead,
    AuthorshipAnalysisTriggerRequest,
    PlagiarismCaseRead,
    PlagiarismCaseUpdateRequest,
    SimilarityAnalysisRead,
    SimilarityAnalysisTriggerRequest,
    SimilarityComparisonRead,
    SimilarityEvidenceFragment,
    SimilarityMatchRead,
    SimilaritySourceFileRead,
    SimilaritySubmissionSideRead,
)
from app.services.common import DomainError, language_for_path
from app.services.integrity import (
    complete_authorship_job,
    create_authorship_job,
    mark_authorship_running,
    run_similarity_analysis,
    transition_plagiarism_case,
)
from app.services.policy import (
    require_membership,
    require_submission_review_access,
    visible_submission_ids_for_assessment,
    visible_submission_ids_for_review,
)
from app.services.teacher_tokens import teacher_membership_is_authorized

router = APIRouter(tags=["integrity"])
Database = Annotated[AsyncSession, Depends(get_db)]


def _domain_error(exc: DomainError) -> None:
    detail: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.details:
        detail["details"] = exc.details
    raise HTTPException(status_code=exc.status_code, detail=detail) from exc


async def _teacher_course_ids(db: AsyncSession, principal_id: uuid.UUID) -> set[uuid.UUID]:
    if not await teacher_membership_is_authorized(db, principal_id):
        return set()
    now = utcnow()
    rows = (
        await db.execute(
            select(CourseMembership.course_id, CourseMembership.role)
            .join(Course, Course.id == CourseMembership.course_id)
            .where(
                CourseMembership.principal_id == principal_id,
                CourseMembership.active.is_(True),
                Course.catalog_enabled.is_(True),
                Course.archived_at.is_(None),
                (CourseMembership.valid_until.is_(None) | (CourseMembership.valid_until > now)),
            )
        )
    ).all()
    roles: dict[uuid.UUID, set[str]] = {}
    for course_id, role in rows:
        roles.setdefault(course_id, set()).add(role)
    return {
        course_id
        for course_id, projected_roles in roles.items()
        if projected_roles == {CourseRole.TEACHER.value}
    }


async def _require_teacher(
    db: AsyncSession,
    *,
    principal_id: uuid.UUID,
    course_id: uuid.UUID,
) -> None:
    try:
        await require_membership(
            db,
            principal_id=principal_id,
            course_id=course_id,
            role=CourseRole.TEACHER,
        )
    except DomainError as exc:
        _domain_error(exc)


async def _submission_course_id(db: AsyncSession, submission_id: uuid.UUID) -> uuid.UUID:
    course_id = await db.scalar(
        select(Assessment.course_id)
        .join(Attempt, Attempt.assessment_id == Assessment.id)
        .join(Submission, Submission.attempt_id == Attempt.id)
        .where(Submission.id == submission_id)
    )
    if course_id is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SUBMISSION_NOT_FOUND", "message": "Submission was not found"},
        )
    return course_id


async def _assessment_course_id(db: AsyncSession, assessment_id: uuid.UUID) -> uuid.UUID:
    course_id = await db.scalar(select(Assessment.course_id).where(Assessment.id == assessment_id))
    if course_id is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "ASSESSMENT_NOT_FOUND", "message": "Assessment was not found"},
        )
    return course_id


async def _similarity_visibility(
    db: AsyncSession,
    *,
    auth: CurrentAuth,
    assessment_id: uuid.UUID,
) -> set[uuid.UUID] | None:
    """Return ``None`` for system-wide read, otherwise exact assigned submissions."""

    if auth.has_capability("SYSTEM_SETTINGS"):
        return None
    course_id = await _assessment_course_id(db, assessment_id)
    await _require_teacher(db, principal_id=auth.principal_id, course_id=course_id)
    return await visible_submission_ids_for_assessment(
        db,
        principal_id=auth.principal_id,
        assessment_id=assessment_id,
    )


async def _authorship_read(
    db: AsyncSession,
    job: AuthorshipAnalysisJob,
) -> AuthorshipAnalysisRead:
    result = await db.scalar(
        select(AuthorshipAnalysisResult).where(AuthorshipAnalysisResult.job_id == job.id)
    )
    if job.state == AuthorshipAnalysisState.COMPLETED.value and result is None:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "AUTHORSHIP_RESULT_MISSING",
                "message": "Completed authorship analysis has no validated result",
            },
        )
    result_read = None
    if result is not None:
        result_read = AuthorshipAnalysisResultRead(
            manifest_hash=result.manifest_hash,
            probability=result.probability,
            confidence=result.confidence,
            uncertainty=result.uncertainty,
            analyzer=result.analyzer,
            model=result.model,
            calibration=result.calibration,
            features=result.features,
            warnings=[str(item) for item in result.warnings],
            response_hash=result.response_hash,
            created_at=result.created_at,
        )
    return AuthorshipAnalysisRead(
        id=job.id,
        submission_id=job.submission_id,
        manifest_hash=job.manifest_hash,
        payload_hash=job.payload_hash,
        export_schema_version=job.export_schema_version,
        state=job.state,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_code=job.error_code,
        error=job.error,
        result=result_read,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _evidence_read(raw: object) -> SimilarityEvidenceFragment | None:
    if not isinstance(raw, dict):
        return None
    first = raw.get("a")
    second = raw.get("b")
    tokens = raw.get("normalized_tokens")
    if not isinstance(first, dict) or not isinstance(second, dict):
        return None
    try:
        return SimilarityEvidenceFragment(
            file_a=str(first.get("path", "")),
            start_line_a=int(first.get("start_line", 0)),
            end_line_a=int(first.get("end_line", 0)),
            file_b=str(second.get("path", "")),
            start_line_b=int(second.get("start_line", 0)),
            end_line_b=int(second.get("end_line", 0)),
            token_count=max(1, len(tokens) if isinstance(tokens, list) else 1),
            excerpt_a=str(first.get("fragment", "")),
            excerpt_b=str(second.get("fragment", "")),
        )
    except (TypeError, ValueError):
        return None


def _case_read(case: PlagiarismCase) -> PlagiarismCaseRead:
    return PlagiarismCaseRead(
        id=case.id,
        match_id=case.match_id,
        state=case.state,
        teacher_comment=case.teacher_comment,
        updated_by_id=case.updated_by_id,
        state_changed_at=case.state_changed_at,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


async def _similarity_read(
    db: AsyncSession,
    analysis: SimilarityAnalysis,
    *,
    visible_submission_ids: set[uuid.UUID] | None = None,
) -> SimilarityAnalysisRead:
    matches = list(
        (
            await db.scalars(
                select(SimilarityMatch)
                .where(SimilarityMatch.analysis_id == analysis.id)
                .order_by(SimilarityMatch.score.desc(), SimilarityMatch.created_at)
            )
        ).all()
    )
    if visible_submission_ids is not None:
        matches = [
            match
            for match in matches
            if match.submission_a_id in visible_submission_ids
            or match.submission_b_id in visible_submission_ids
        ]
    cases_by_match: dict[uuid.UUID, PlagiarismCase] = {}
    if matches:
        cases = list(
            (
                await db.scalars(
                    select(PlagiarismCase).where(
                        PlagiarismCase.match_id.in_([match.id for match in matches])
                    )
                )
            ).all()
        )
        cases_by_match = {case.match_id: case for case in cases}
    match_reads = [_similarity_match_read(match, cases_by_match.get(match.id)) for match in matches]
    if visible_submission_ids is None:
        submission_count = analysis.submission_count
        comparison_count = analysis.comparison_count
        match_count = analysis.match_count
    else:
        # Scoped readers must not learn aggregate sizes for students outside
        # their assigned groups.  Counts describe the returned evidence only.
        scoped_submission_ids = {
            submission_id
            for match in matches
            for submission_id in (match.submission_a_id, match.submission_b_id)
        }
        submission_count = len(scoped_submission_ids)
        comparison_count = len(matches)
        match_count = len(matches)
    return SimilarityAnalysisRead(
        id=analysis.id,
        assessment_id=analysis.assessment_id,
        task_version_id=analysis.task_version_id,
        algorithm_version=analysis.algorithm_version,
        config=analysis.config,
        state=analysis.state,
        submission_count=submission_count,
        comparison_count=comparison_count,
        match_count=match_count,
        started_at=analysis.started_at,
        completed_at=analysis.completed_at,
        error=analysis.error,
        matches=match_reads,
        created_at=analysis.created_at,
        updated_at=analysis.updated_at,
    )


def _similarity_match_read(
    match: SimilarityMatch,
    case: PlagiarismCase | None,
) -> SimilarityMatchRead:
    evidence = [item for raw in match.evidence if (item := _evidence_read(raw)) is not None]
    return SimilarityMatchRead(
        id=match.id,
        submission_a_id=match.submission_a_id,
        submission_b_id=match.submission_b_id,
        manifest_hash_a=match.manifest_hash_a,
        manifest_hash_b=match.manifest_hash_b,
        score=match.score,
        fingerprint_count_a=match.fingerprint_count_a,
        fingerprint_count_b=match.fingerprint_count_b,
        shared_fingerprint_count=match.shared_fingerprint_count,
        evidence=evidence,
        case=_case_read(case) if case else None,
    )


async def _analyze_authorship(
    request: Request,
    payload: dict[str, Any],
    manifest_hash: str,
):
    injected = getattr(request.app.state, "authorship_transport", None)
    if injected is not None:
        return await injected.analyze(payload, manifest_hash=manifest_hash)
    settings: Settings = request.app.state.settings
    async with httpx.AsyncClient() as client:
        transport = AuthorshipTransport(settings, client)
        return await transport.analyze(payload, manifest_hash=manifest_hash)


@router.get(
    "/submissions/{submission_id}/authorship-analyses",
    response_model=list[AuthorshipAnalysisRead],
)
async def list_submission_authorship(
    submission_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> list[AuthorshipAnalysisRead]:
    try:
        await require_submission_review_access(
            db,
            principal_id=auth.principal_id,
            submission_id=submission_id,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
        )
    except DomainError as exc:
        _domain_error(exc)
    rows = list(
        (
            await db.scalars(
                select(AuthorshipAnalysisJob)
                .where(AuthorshipAnalysisJob.submission_id == submission_id)
                .order_by(AuthorshipAnalysisJob.created_at.desc())
            )
        ).all()
    )
    return [await _authorship_read(db, row) for row in rows]


@router.post(
    "/submissions/{submission_id}/authorship-analyses",
    response_model=AuthorshipAnalysisRead,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_authorship(
    submission_id: uuid.UUID,
    _body: AuthorshipAnalysisTriggerRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> AuthorshipAnalysisRead:
    settings: Settings = request.app.state.settings
    try:
        async with db.begin():
            await require_submission_review_access(
                db,
                principal_id=auth.principal_id,
                submission_id=submission_id,
                allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
            )
            job, payload = await create_authorship_job(
                db,
                submission_id=submission_id,
                teacher_id=auth.principal_id,
                settings=settings,
                system_access=auth.has_capability("SYSTEM_SETTINGS"),
            )
            await mark_authorship_running(db, job.id)
            job_id = job.id
            manifest_hash = job.manifest_hash
    except DomainError as exc:
        _domain_error(exc)

    try:
        result = await _analyze_authorship(request, payload, manifest_hash)
    except IntegrationProtocolError as exc:
        async with db.begin():
            job = await complete_authorship_job(
                db,
                job_id=job_id,
                error_code=exc.code,
                error=str(exc),
                invalid=True,
            )
    except IntegrationError as exc:
        async with db.begin():
            job = await complete_authorship_job(
                db,
                job_id=job_id,
                error_code=exc.code,
                error=str(exc),
            )
    else:
        async with db.begin():
            job = await complete_authorship_job(db, job_id=job_id, result=result)
    return await _authorship_read(db, job)


@router.get("/authorship-analyses", response_model=list[AuthorshipAnalysisRead])
async def list_authorship(
    auth: CurrentAuth,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuthorshipAnalysisRead]:
    system_read = auth.has_capability("SYSTEM_SETTINGS")
    submission_ids = await visible_submission_ids_for_review(
        db,
        principal_id=auth.principal_id,
        allow_system_settings_read=system_read,
    )
    if not submission_ids:
        return []
    rows = list(
        (
            await db.scalars(
                select(AuthorshipAnalysisJob)
                .where(AuthorshipAnalysisJob.submission_id.in_(submission_ids))
                .order_by(AuthorshipAnalysisJob.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    return [await _authorship_read(db, row) for row in rows]


@router.get("/authorship-analyses/{analysis_id}", response_model=AuthorshipAnalysisRead)
async def get_authorship(
    analysis_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> AuthorshipAnalysisRead:
    job = await db.get(AuthorshipAnalysisJob, analysis_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "AUTHORSHIP_JOB_NOT_FOUND", "message": "Job was not found"},
        )
    try:
        await require_submission_review_access(
            db,
            principal_id=auth.principal_id,
            submission_id=job.submission_id,
            allow_system_settings_read=auth.has_capability("SYSTEM_SETTINGS"),
        )
    except DomainError as exc:
        _domain_error(exc)
    return await _authorship_read(db, job)


@router.get(
    "/assessments/{assessment_id}/similarity-analyses",
    response_model=list[SimilarityAnalysisRead],
)
async def list_assessment_similarity(
    assessment_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> list[SimilarityAnalysisRead]:
    visibility = await _similarity_visibility(db, auth=auth, assessment_id=assessment_id)
    rows = list(
        (
            await db.scalars(
                select(SimilarityAnalysis)
                .where(SimilarityAnalysis.assessment_id == assessment_id)
                .order_by(SimilarityAnalysis.created_at.desc())
            )
        ).all()
    )
    return [await _similarity_read(db, row, visible_submission_ids=visibility) for row in rows]


@router.post(
    "/assessments/{assessment_id}/similarity-analyses",
    response_model=SimilarityAnalysisRead,
    status_code=status.HTTP_201_CREATED,
)
async def trigger_similarity(
    assessment_id: uuid.UUID,
    body: SimilarityAnalysisTriggerRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> SimilarityAnalysisRead:
    course_id = await _assessment_course_id(db, assessment_id)
    system_access = auth.has_capability("SYSTEM_SETTINGS")
    if system_access:
        visibility = None
    else:
        await _require_teacher(db, principal_id=auth.principal_id, course_id=course_id)
        visibility = await visible_submission_ids_for_assessment(
            db,
            principal_id=auth.principal_id,
            assessment_id=assessment_id,
        )
        if not visibility:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "SUBMISSION_REVIEW_SCOPE_REQUIRED",
                    "message": "No submitted works are assigned to this teacher",
                },
            )
    settings: Settings = request.app.state.settings
    try:
        analysis = await run_similarity_analysis(
            db,
            assessment_id=assessment_id,
            teacher_id=auth.principal_id,
            settings=settings,
            task_version_id=body.task_version_id,
            system_access=system_access,
        )
    except DomainError as exc:
        await db.commit()
        _domain_error(exc)
    except Exception:
        await db.rollback()
        raise
    else:
        await db.commit()
    return await _similarity_read(db, analysis, visible_submission_ids=visibility)


async def _global_similarity_rows(
    db: AsyncSession,
    course_ids: Iterable[uuid.UUID],
    *,
    limit: int,
    offset: int,
) -> list[SimilarityAnalysis]:
    return list(
        (
            await db.scalars(
                select(SimilarityAnalysis)
                .join(Assessment, Assessment.id == SimilarityAnalysis.assessment_id)
                .where(Assessment.course_id.in_(list(course_ids)))
                .order_by(SimilarityAnalysis.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )


@router.get("/similarity-analyses", response_model=list[SimilarityAnalysisRead])
async def list_similarity(
    auth: CurrentAuth,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SimilarityAnalysisRead]:
    system_read = auth.has_capability("SYSTEM_SETTINGS")
    course_ids = (
        set(
            (
                await db.scalars(
                    select(Course.id).where(
                        Course.catalog_enabled.is_(True),
                        Course.archived_at.is_(None),
                    )
                )
            ).all()
        )
        if system_read
        else await _teacher_course_ids(db, auth.principal_id)
    )
    if not course_ids:
        return []
    rows = await _global_similarity_rows(db, course_ids, limit=limit, offset=offset)
    result: list[SimilarityAnalysisRead] = []
    for row in rows:
        visibility = (
            None
            if system_read
            else await visible_submission_ids_for_assessment(
                db,
                principal_id=auth.principal_id,
                assessment_id=row.assessment_id,
            )
        )
        result.append(await _similarity_read(db, row, visible_submission_ids=visibility))
    return result


@router.get("/similarity-analyses/{analysis_id}", response_model=SimilarityAnalysisRead)
async def get_similarity(
    analysis_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> SimilarityAnalysisRead:
    analysis = await db.get(SimilarityAnalysis, analysis_id)
    if analysis is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SIMILARITY_NOT_FOUND", "message": "Analysis was not found"},
        )
    visibility = await _similarity_visibility(
        db,
        auth=auth,
        assessment_id=analysis.assessment_id,
    )
    return await _similarity_read(db, analysis, visible_submission_ids=visibility)


async def _comparison_side(
    db: AsyncSession,
    *,
    submission_id: uuid.UUID,
    course_id: uuid.UUID,
) -> SimilaritySubmissionSideRead:
    row = (
        await db.execute(
            select(Submission, Attempt, ExternalPrincipal)
            .join(Attempt, Attempt.id == Submission.attempt_id)
            .join(ExternalPrincipal, ExternalPrincipal.id == Attempt.principal_id)
            .where(Submission.id == submission_id)
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(
            status_code=500,
            detail={"code": "PLAGIARISM_CONTEXT_MISSING", "message": "Submission is missing"},
        )
    submission, attempt, student = row
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    if snapshot is None:
        raise HTTPException(
            status_code=500,
            detail={"code": "SNAPSHOT_MISSING", "message": "Submission snapshot is missing"},
        )
    group_names = list(
        (
            await db.scalars(
                select(CourseGroup.name)
                .join(
                    CourseMembershipGroup,
                    CourseMembershipGroup.coursegroup_id == CourseGroup.id,
                )
                .join(
                    CourseMembership,
                    CourseMembership.id == CourseMembershipGroup.coursemembership_id,
                )
                .where(
                    CourseMembership.principal_id == attempt.principal_id,
                    CourseMembership.course_id == course_id,
                    CourseMembership.role == CourseRole.STUDENT.value,
                )
                .order_by(CourseGroup.name)
            )
        ).all()
    )
    files: list[SimilaritySourceFileRead] = []
    for raw in snapshot.files:
        if not isinstance(raw, dict):
            continue
        path = str(raw.get("path", "")).strip()
        if not path:
            continue
        files.append(
            SimilaritySourceFileRead(
                path=path,
                language=str(raw.get("language") or language_for_path(path)),
                content=str(raw.get("content", "")),
            )
        )
    return SimilaritySubmissionSideRead(
        submission_id=submission.id,
        student_name=student.display_name,
        student_group=", ".join(dict.fromkeys(group_names)),
        submitted_at=submission.submitted_at,
        files=files,
    )


@router.get(
    "/similarity-matches/{match_id}/comparison",
    response_model=SimilarityComparisonRead,
)
async def get_similarity_comparison(
    match_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> SimilarityComparisonRead:
    """Expose an out-of-scope peer only inside one evidence-backed pair."""

    match = await db.get(SimilarityMatch, match_id)
    analysis = await db.get(SimilarityAnalysis, match.analysis_id) if match else None
    assessment = await db.get(Assessment, analysis.assessment_id) if analysis else None
    if match is None or analysis is None or assessment is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SIMILARITY_MATCH_NOT_FOUND", "message": "Match was not found"},
        )
    visibility = (
        None
        if auth.has_capability("SYSTEM_SETTINGS")
        else await visible_submission_ids_for_assessment(
            db,
            principal_id=auth.principal_id,
            assessment_id=assessment.id,
        )
    )
    if visibility is not None and not {
        match.submission_a_id,
        match.submission_b_id,
    }.intersection(visibility):
        # Hide the existence of unrelated plagiarism pairs.
        raise HTTPException(
            status_code=404,
            detail={"code": "SIMILARITY_MATCH_NOT_FOUND", "message": "Match was not found"},
        )
    case = await db.scalar(select(PlagiarismCase).where(PlagiarismCase.match_id == match.id))
    return SimilarityComparisonRead(
        match=_similarity_match_read(match, case),
        assessment_id=assessment.id,
        assessment_title=assessment.title,
        left=await _comparison_side(
            db,
            submission_id=match.submission_a_id,
            course_id=assessment.course_id,
        ),
        right=await _comparison_side(
            db,
            submission_id=match.submission_b_id,
            course_id=assessment.course_id,
        ),
    )


async def _case_course_id(db: AsyncSession, case: PlagiarismCase) -> uuid.UUID:
    course_id = await db.scalar(
        select(Assessment.course_id)
        .join(SimilarityAnalysis, SimilarityAnalysis.assessment_id == Assessment.id)
        .join(SimilarityMatch, SimilarityMatch.analysis_id == SimilarityAnalysis.id)
        .where(SimilarityMatch.id == case.match_id)
    )
    if course_id is None:
        raise HTTPException(
            status_code=500,
            detail={"code": "PLAGIARISM_CONTEXT_MISSING", "message": "Case context is missing"},
        )
    return course_id


@router.get("/plagiarism-cases", response_model=list[PlagiarismCaseRead])
async def list_plagiarism_cases(
    auth: CurrentAuth,
    db: Database,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[PlagiarismCaseRead]:
    system_read = auth.has_capability("SYSTEM_SETTINGS")
    course_ids = (
        set(
            (
                await db.scalars(
                    select(Course.id).where(
                        Course.catalog_enabled.is_(True),
                        Course.archived_at.is_(None),
                    )
                )
            ).all()
        )
        if system_read
        else await _teacher_course_ids(db, auth.principal_id)
    )
    if not course_ids:
        return []
    rows = list(
        (
            await db.scalars(
                select(PlagiarismCase)
                .join(SimilarityMatch, SimilarityMatch.id == PlagiarismCase.match_id)
                .join(SimilarityAnalysis, SimilarityAnalysis.id == SimilarityMatch.analysis_id)
                .join(Assessment, Assessment.id == SimilarityAnalysis.assessment_id)
                .where(Assessment.course_id.in_(course_ids))
                .order_by(PlagiarismCase.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).all()
    )
    if system_read:
        return [_case_read(row) for row in rows]
    visible_by_assessment: dict[uuid.UUID, set[uuid.UUID]] = {}
    allowed: list[PlagiarismCaseRead] = []
    for row in rows:
        context = (
            await db.execute(
                select(SimilarityMatch, SimilarityAnalysis)
                .join(
                    SimilarityAnalysis,
                    SimilarityAnalysis.id == SimilarityMatch.analysis_id,
                )
                .where(SimilarityMatch.id == row.match_id)
            )
        ).one_or_none()
        if context is None:
            continue
        match, analysis = context
        visible = visible_by_assessment.get(analysis.assessment_id)
        if visible is None:
            visible = await visible_submission_ids_for_assessment(
                db,
                principal_id=auth.principal_id,
                assessment_id=analysis.assessment_id,
            )
            visible_by_assessment[analysis.assessment_id] = visible
        if {match.submission_a_id, match.submission_b_id}.intersection(visible):
            allowed.append(_case_read(row))
    return allowed


@router.get("/plagiarism-cases/{case_id}", response_model=PlagiarismCaseRead)
async def get_plagiarism_case(
    case_id: uuid.UUID,
    auth: CurrentAuth,
    db: Database,
) -> PlagiarismCaseRead:
    case = await db.get(PlagiarismCase, case_id)
    if case is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "PLAGIARISM_CASE_NOT_FOUND", "message": "Case was not found"},
        )
    match = await db.get(SimilarityMatch, case.match_id)
    analysis = await db.get(SimilarityAnalysis, match.analysis_id) if match else None
    if match is None or analysis is None:
        raise HTTPException(
            status_code=500,
            detail={"code": "PLAGIARISM_CONTEXT_MISSING", "message": "Case context is missing"},
        )
    visibility = await _similarity_visibility(
        db,
        auth=auth,
        assessment_id=analysis.assessment_id,
    )
    if visibility is not None and not {
        match.submission_a_id,
        match.submission_b_id,
    }.intersection(visibility):
        raise HTTPException(
            status_code=404,
            detail={"code": "PLAGIARISM_CASE_NOT_FOUND", "message": "Case was not found"},
        )
    return _case_read(case)


@router.patch("/plagiarism-cases/{case_id}", response_model=PlagiarismCaseRead)
async def update_plagiarism_case(
    case_id: uuid.UUID,
    body: PlagiarismCaseUpdateRequest,
    request: Request,
    auth: CurrentAuth,
    db: Database,
) -> PlagiarismCaseRead:
    try:
        async with db.begin():
            case_hint = await db.get(PlagiarismCase, case_id)
            match = await db.get(SimilarityMatch, case_hint.match_id) if case_hint else None
            analysis = await db.get(SimilarityAnalysis, match.analysis_id) if match else None
            if case_hint is None or match is None or analysis is None:
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "PLAGIARISM_CASE_NOT_FOUND",
                        "message": "Case was not found",
                    },
                )
            visible = (
                None
                if auth.has_capability("SYSTEM_SETTINGS")
                else await visible_submission_ids_for_assessment(
                    db,
                    principal_id=auth.principal_id,
                    assessment_id=analysis.assessment_id,
                )
            )
            if visible is not None and not {
                match.submission_a_id,
                match.submission_b_id,
            }.intersection(visible):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "code": "PLAGIARISM_CASE_NOT_FOUND",
                        "message": "Case was not found",
                    },
                )
            case = await transition_plagiarism_case(
                db,
                case_id=case_id,
                teacher_id=auth.principal_id,
                state=body.state.value,
                teacher_comment=body.teacher_comment,
                request_id=getattr(request.state, "request_id", ""),
            )
    except DomainError as exc:
        _domain_error(exc)
    return _case_read(case)


__all__ = ["router"]
