"""Read-only, review-scoped details for manual import notifications.

Acknowledgements use problem identity, not job/run ids: retrying an unchanged
archive does not resurrect it, while another attempt or changed answer does.
Never expose connector bodies, cookies, arbitrary URLs, or exception strings.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any
from urllib.parse import urlencode, urlsplit

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attempts import Attempt, Submission
from app.models.courses import Course
from app.models.identity import ExternalPrincipal, LMSConnection
from app.models.integration import HistoryWarningDismissal, SyncOutbox
from app.models.tasks import Assessment
from app.services.moodle_history_diagnostics import history_import_diagnostic, history_warning_codes
from app.services.policy import review_student_ids_for_course

_SOURCE_CODES = {
    "ARCHIVE_SOURCE_OMITTED", "ARCHIVE_CHECKSUM_MISMATCH", "ARTIFACT_OMITTED",
    "ANSWER_OMITTED", "FILE_TOO_LARGE", "DOWNLOAD_TIMEOUT",
}
_OMISSION_MESSAGES = {
    "ARCHIVE_HAS_NO_SUPPORTED_SOURCE_FILES": (
        "В архиве нет поддерживаемых исходников C/C++. Просмотрите вложение в Moodle "
        "или запросите исходники у студента."
    ),
    "ARCHIVE_HAS_NO_READABLE_SOURCE_FILES": "В архиве не удалось прочитать исходники C/C++.",
    "ARCHIVE_CHECKSUM_MISMATCH": "Не совпала контрольная сумма архива. Нужна исправная копия.",
    "ARCHIVE_INVALID_OR_ENCRYPTED": (
        "Архив повреждён, зашифрован или имеет неподдерживаемый формат."
    ),
    "ARCHIVE_EXPANDED_SIZE_LIMIT": "Распакованные файлы превышают безопасный лимит размера.",
    "ARCHIVE_SOURCE_FILE_LIMIT": "Исходный файл в архиве превышает безопасный лимит размера.",
    "ARCHIVE_MEMBER_LIMIT": "В архиве слишком много файлов для безопасной распаковки.",
    "ARCHIVE_EXTRACTION_TIMEOUT": "Не удалось распаковать архив за отведённое время.",
    "ARCHIVE_EXTRACTION_INCOMPLETE": "Архив распакован не полностью.",
    "ARCHIVE_READER_UNAVAILABLE": "На сервере недоступен инструмент распаковки архивов.",
    "ARCHIVE_DUPLICATE_PATH": "В архиве есть файлы с одинаковыми путями.",
}


def _text(value: object, limit: int = 255) -> str:
    return str(value or "")[:limit]


def _refs(row: SyncOutbox) -> list[dict]:
    refs = (row.payload or {}).get("attempt_refs")
    return [ref for ref in refs if isinstance(ref, dict)] if isinstance(refs, list) else []


def _moodle_url(base_url: str, module: str, cmid: str, attempt: str, user: str) -> str | None:
    base = urlsplit(base_url)
    if base.scheme not in {"http", "https"} or not base.netloc or base.username or base.password:
        return None
    if not re.fullmatch(r"[0-9]{1,20}", cmid):
        return None
    if module == "quiz" and re.fullmatch(r"[0-9]{1,20}", attempt):
        path, query = "quiz/review.php", {"attempt": attempt, "cmid": cmid}
    elif module == "assign" and re.fullmatch(r"[0-9]{1,20}", user):
        path, query = "assign/view.php", {"id": cmid, "action": "grader", "userid": user}
    else:
        return None
    # Keep Moodle's installation subpath, but never propagate query credentials.
    origin = f"{base.scheme}://{base.netloc}{base.path.rstrip('/')}"
    return f"{origin}/mod/{path}?{urlencode(query)}"


def _omissions(submission: Submission) -> list[dict]:
    receipt = submission.external_receipt or {}
    values = receipt.get("source_refresh_omissions", receipt.get("source_omissions", []))
    return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []


def _source_identity(receipt: dict) -> object:
    # Grades/comments may change the Moodle revision without changing the bad
    # archive. Prefer byte digests so reviewing an answer cannot revive it.
    observations = receipt.get("lms_response_observations")
    digests = sorted({
        (_text(item.get("kind")), _text(item.get("filename")), _text(item.get("sha256")))
        for item in observations if isinstance(item, dict) and item.get("sha256")
    }) if isinstance(observations, list) else []
    return digests or _text(receipt.get("external_revision"))


async def add_history_warning_details(
    db: AsyncSession, *, course_id: uuid.UUID, principal_id: uuid.UUID,
    statuses: list[dict[str, Any]], rows: list[SyncOutbox],
    allow_system_settings_read: bool = False,
) -> None:
    problems = [row for row in rows if row.state in {"FAILED", "BLOCKED"}
                or (row.receipt or {}).get("warning_count")]
    for status in statuses:
        status.update(warnings=[], warnings_dismissed=False)
    if not problems:
        return
    course = await db.get(Course, course_id)
    if course is None or not course.catalog_enabled or course.archived_at is not None:
        return
    connection = await db.get(LMSConnection, course.connection_id)
    subjects = {_text(ref.get("user_id")) for row in problems for ref in _refs(row)} - {""}
    students_query = select(ExternalPrincipal).where(
        ExternalPrincipal.connection_id == course.connection_id,
        ExternalPrincipal.external_subject.in_(subjects),
    )
    if not allow_system_settings_read:
        allowed = await review_student_ids_for_course(
            db, principal_id=principal_id, course_id=course_id,
        )
        students_query = students_query.where(ExternalPrincipal.id.in_(allowed))
    students = {student.external_subject: student
                for student in (await db.scalars(students_query)).all()}

    # Fetch metadata in bulk, not files/snapshots and not one query per student.
    attempts = {_text(ref.get("attempt_id"), 160) for row in problems for ref in _refs(row)}
    submissions = (await db.execute(
        select(Submission, Assessment, ExternalPrincipal.external_subject)
        .join(Attempt, Attempt.id == Submission.attempt_id)
        .join(Assessment, Assessment.id == Attempt.assessment_id)
        .join(ExternalPrincipal, ExternalPrincipal.id == Attempt.principal_id)
        .where(
            Assessment.course_id == course_id,
            Attempt.state != "VOID",
            Attempt.principal_id.in_([student.id for student in students.values()]),
            Submission.external_receipt["moodle_parent_attempt_id"].as_string().in_(attempts),
        ).order_by(Submission.submitted_at.desc(), Submission.id)
    )).all() if students else []
    by_attempt: dict[tuple, dict[str, Submission]] = {}
    for submission, assessment, subject in submissions:
        receipt = submission.external_receipt or {}
        parent_id = (assessment.policy or {}).get("moodle_parent_assessment_id") or assessment.id
        key = (str(parent_id), _text(receipt.get("lms_module")), _text(receipt.get("lms_cmid")),
               _text(receipt.get("moodle_parent_attempt_id"), 160), subject)
        by_attempt.setdefault(key, {}).setdefault(
            _text(receipt.get("moodle_response_id")), submission,
        )

    grouped: dict[uuid.UUID, dict[str, dict]] = {}
    for row in problems:
        code, message = history_import_diagnostic([row])
        payload = row.payload or {}
        module, cmid = _text(payload.get("module")), _text(payload.get("cmid"))
        for ref in _refs(row) or [{}]:
            subject, attempt = _text(ref.get("user_id")), _text(ref.get("attempt_id"), 160)
            student = students.get(subject)
            visible = student is not None or (allow_system_settings_read and bool(subject))
            if not visible and not allow_system_settings_read and (
                ref or payload.get("detail_key") or code in _SOURCE_CODES
            ):
                # A foreign student's problem is not a course-wide warning.
                # Do not replace it with an anonymous banner for other teachers.
                continue
            matches = list(by_attempt.get(
                (str(row.aggregate_id), module, cmid, attempt, subject), {},
            ).values()) if visible else []
            affected = (
                [item for item in matches if _omissions(item)] if code in _SOURCE_CODES else []
            )
            # One line per affected question; otherwise name the whole attempt.
            for submission in affected or matches[:1] or [None]:
                receipt = submission.external_receipt if submission else {}
                omissions = _omissions(submission) if submission in affected else []
                reasons = sorted({_text(item.get("reason")) for item in omissions})
                explanation = " ".join(_OMISSION_MESSAGES[reason] for reason in reasons
                                       if reason in _OMISSION_MESSAGES) or message
                response_id = _text(receipt.get("moodle_response_id")) if omissions else ""
                identity = [
                    "history-warning-v1", str(row.aggregate_id), code,
                    history_warning_codes((row.receipt or {}).get("warning_codes", [])),
                    module if visible else "", cmid if visible else "",
                    subject if visible else "", attempt if visible else "", response_id,
                    _source_identity(receipt) if omissions else "",
                    sorted({(_text(item.get("kind")), _text(item.get("filename")),
                             _text(item.get("reason"))) for item in omissions}),
                ]
                warning_id = hashlib.sha256(
                    json.dumps(identity, ensure_ascii=True).encode()
                ).hexdigest()
                position = receipt.get("moodle_response_position") if omissions else None
                detail = {
                    "id": warning_id, "code": code, "message": explanation,
                    "student_name": (student.display_name if student else
                                     _text(ref.get("display_name")) or None) if visible else None,
                    "attempt_id": attempt or None if visible else None,
                    "response_label": f"Задание {position}" if isinstance(position, int) else None,
                    "submission_id": submission.id if submission else None,
                    "moodle_url": _moodle_url(
                        connection.base_url, module, cmid, attempt, subject,
                    ) if connection and visible else None,
                }
                grouped.setdefault(row.aggregate_id, {})[warning_id] = detail

    dismissed = set((await db.scalars(select(HistoryWarningDismissal.warning_id).where(
        HistoryWarningDismissal.principal_id == principal_id,
        HistoryWarningDismissal.assessment_id.in_(grouped),
    ))).all())
    for status in statuses:
        details = grouped.get(status["assessment_id"], {})
        status["warnings"] = sorted(
            (item for key, item in details.items() if key not in dismissed),
            key=lambda item: (item["student_name"] or "", item["attempt_id"] or "", item["id"]),
        )
        status["warnings_dismissed"] = bool(details) and not status["warnings"]


async def save_warning_dismissals(
    db: AsyncSession, *, principal_id: uuid.UUID, assessment_id: uuid.UUID,
    requested_ids: list[str], visible_ids: set[str],
) -> list[str]:
    # Short actor-scoped lock makes two tabs' acknowledgements idempotent.
    # No Moodle calls, no course/attempt locks and no changes to sync state.
    await db.execute(update(ExternalPrincipal).where(ExternalPrincipal.id == principal_id).values(
        updated_at=ExternalPrincipal.updated_at,
    ).execution_options(synchronize_session=False))
    existing = set((await db.scalars(select(HistoryWarningDismissal.warning_id).where(
        HistoryWarningDismissal.principal_id == principal_id,
        HistoryWarningDismissal.assessment_id == assessment_id,
    ))).all())
    acknowledged = set(requested_ids) & (visible_ids | existing)
    db.add_all([HistoryWarningDismissal(
        principal_id=principal_id, assessment_id=assessment_id, warning_id=warning_id,
    ) for warning_id in acknowledged - existing])
    await db.flush()
    return sorted(acknowledged)
