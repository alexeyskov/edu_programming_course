"""Durable coordination for independent solutions in one Moodle Quiz attempt."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attempts import Attempt, MoodleQuizQuestion, Workspace, WorkspaceFile
from app.models.courses import Course
from app.models.tasks import Assessment, TaskVersion
from app.services.common import DomainError, language_for_path, sha256_text
from app.services.moodle_quiz_runtime import (
    PreparedMoodleQuizAttempt,
    materialize_prepared_task_version,
    moodle_question_local_max_score,
    prepared_binding_matches_attempt,
)


async def quiz_question_for_attempt(
    db: AsyncSession, attempt_id: uuid.UUID
) -> MoodleQuizQuestion | None:
    return await db.scalar(
        select(MoodleQuizQuestion).where(MoodleQuizQuestion.attempt_id == attempt_id)
    )


async def quiz_session_questions(
    db: AsyncSession, root_attempt_id: uuid.UUID
) -> list[MoodleQuizQuestion]:
    return list(
        (
            await db.scalars(
                select(MoodleQuizQuestion)
                .where(MoodleQuizQuestion.root_attempt_id == root_attempt_id)
                .order_by(MoodleQuizQuestion.position)
            )
        ).all()
    )


async def ensure_quiz_session(
    db: AsyncSession,
    *,
    root: Attempt,
    assessment: Assessment,
    prepared: PreparedMoodleQuizAttempt,
) -> None:
    """Materialize every solution atomically while start holds the parent lock."""

    # Imports are lazy because historical import also uses workspace constants.
    from app.services.moodle_history import _ensure_quiz_question_context
    from app.services.workspace import create_snapshot, refresh_workspace_hash

    existing = await quiz_session_questions(db, root.id)
    if len(prepared.questions) < 2:
        if existing:
            raise DomainError(
                409, "MOODLE_ATTEMPT_BINDING_CONFLICT", "Moodle question list changed"
            )
        return
    if existing:
        if [row.question_slot for row in existing] != [
            question.question_slot for question in prepared.questions
        ]:
            raise DomainError(
                409, "MOODLE_ATTEMPT_BINDING_CONFLICT", "Moodle question list changed"
            )
        for binding, question in zip(existing, prepared.questions, strict=True):
            attempt = await db.get(Attempt, binding.attempt_id)
            version = (
                await db.get(TaskVersion, attempt.assigned_task_version_id)
                if attempt is not None and attempt.assigned_task_version_id is not None
                else None
            )
            if (
                attempt is None
                or attempt.principal_id != root.principal_id
                or not prepared_binding_matches_attempt(attempt.integrity_policy, question)
                or version is None
                or version.statement != question.question_text
                or binding.question_max_mark != question.question_max_mark
            ):
                raise DomainError(
                    409, "MOODLE_ATTEMPT_BINDING_CONFLICT", "Moodle question binding changed"
                )
            attempt.expected_end_at = root.expected_end_at
            attempt.deadline_at = root.deadline_at
            if "moodle_sync_timeout_seconds" in (root.integrity_policy or {}):
                attempt.integrity_policy = {
                    **dict(attempt.integrity_policy or {}),
                    "moodle_sync_timeout_seconds": root.integrity_policy[
                        "moodle_sync_timeout_seconds"
                    ],
                }
        return

    course = await db.get(Course, assessment.course_id)
    if course is None:
        raise DomainError(500, "COURSE_MISSING", "Moodle course is missing")
    for position, question in enumerate(prepared.questions, start=1):
        if question.question_max_mark is None:
            raise DomainError(
                502, "INVALID_MOODLE_PREPARATION", "Moodle question mark is unconfirmed"
            )
        title = f"Задание {position}"
        if position == 1:
            attempt = root
        else:
            child, base_version = await _ensure_quiz_question_context(
                db,
                course=course,
                parent=assessment,
                response={
                    "response_id": question.question_slot,
                    "question_text": question.question_text,
                    "grade_max": str(moodle_question_local_max_score(question.question_max_mark)),
                },
                position=position,
                cmid=prepared.cmid,
                multi_file=question.answer_transport == "ESSAY_ATTACHMENT",
            )
            version = await materialize_prepared_task_version(
                db, base_version=base_version, prepared=question
            )
            last_sequence = await db.scalar(
                select(func.max(Attempt.sequence)).where(
                    Attempt.assessment_id == child.id,
                    Attempt.principal_id == root.principal_id,
                )
            )
            attempt = Attempt(
                assessment_id=child.id,
                assigned_task_version_id=version.id,
                principal_id=root.principal_id,
                sequence=(last_sequence or 0) + 1,
                started_at=root.started_at,
                expected_end_at=root.expected_end_at,
                deadline_at=root.deadline_at,
                client_context=dict(root.client_context or {}),
                integrity_policy={
                    **dict(root.integrity_policy or {}),
                    "moodle_question_slot": question.question_slot,
                    "moodle_answer_transport": question.answer_transport,
                    "moodle_available_answer_transports": list(
                        question.available_answer_transports
                    ),
                },
            )
            db.add(attempt)
            await db.flush()
            workspace = Workspace(attempt_id=attempt.id, multi_file=version.multi_file)
            db.add(workspace)
            await db.flush()
            path = "main.c" if version.language == "C" else "main.cpp"
            db.add(
                WorkspaceFile(
                    workspace_id=workspace.id,
                    path=path,
                    language=language_for_path(path),
                    content="",
                    content_hash=sha256_text(""),
                )
            )
            await db.flush()
            await refresh_workspace_hash(db, workspace)
            await create_snapshot(db, workspace, "ATTEMPT_STARTED")
        attempt.integrity_policy = {
            **dict(attempt.integrity_policy or {}),
            "moodle_quiz_root_attempt_id": str(root.id),
            "moodle_parent_assessment_id": str(assessment.id),
            "moodle_question_max_mark": str(question.question_max_mark),
        }
        db.add(
            MoodleQuizQuestion(
                root_attempt_id=root.id,
                attempt_id=attempt.id,
                question_slot=question.question_slot,
                position=position,
                title=title,
                question_max_mark=question.question_max_mark,
            )
        )
    await db.flush()


__all__ = ["ensure_quiz_session", "quiz_question_for_attempt", "quiz_session_questions"]
