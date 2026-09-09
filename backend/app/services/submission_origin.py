from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.attempts import Attempt, Submission
from app.models.integration import LMSSubmissionFingerprint
from app.models.tasks import Assessment

OriginVerificationState = Literal[
    "VERIFIED",
    "EXTERNAL_ORIGIN",
    "MISMATCH",
    "PENDING",
    "UNAVAILABLE",
]

ONLINE_TEXT_TRANSPORTS = frozenset({"ESSAY_ONLINE_TEXT", "ASSIGN_ONLINE_TEXT"})
TEXT_CANONICALIZATION = "MOODLE_TEXT_V1"
BINARY_CANONICALIZATION = "BINARY_EXACT_V1"


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def canonical_moodle_text_bytes(value: str | bytes) -> bytes:
    """Canonicalize the complete Moodle editor value on both connector sides.

    Moodle themes expose editor content either as CRLF text or rendered HTML,
    and rendered containers add boundary blank lines.  Internal whitespace,
    indentation, tabs and blank lines are retained exactly.
    """

    text = value.decode("utf-8") if isinstance(value, bytes) else value
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines).encode("utf-8")


def content_digests(content: bytes) -> tuple[str, str]:
    # nosec B324: MD5 is compatibility metadata only; SHA-256 is authoritative.
    return hashlib.md5(content, usedforsecurity=False).hexdigest(), hashlib.sha256(
        content
    ).hexdigest()


def comparison_content(content: bytes, answer_transport: str) -> tuple[bytes, str]:
    if answer_transport in ONLINE_TEXT_TRANSPORTS:
        return canonical_moodle_text_bytes(content), TEXT_CANONICALIZATION
    return content, BINARY_CANONICALIZATION


def historical_response_observations(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Return bounded digests of the exact response values fetched from an LMS."""

    observations: list[dict[str, Any]] = []
    responses = item.get("responses")
    if not isinstance(responses, list):
        return observations
    for response in responses[:128]:
        if not isinstance(response, dict):
            continue
        answer = response.get("answer_text")
        if isinstance(answer, str) and (answer or response.get("answer_complete") is True):
            raw = answer.encode("utf-8")
            compared = canonical_moodle_text_bytes(answer)
            raw_md5, raw_sha256 = content_digests(raw)
            comparison_md5, comparison_sha256 = content_digests(compared)
            observations.append(
                {
                    "kind": "ONLINE_TEXT",
                    "filename": "",
                    "size": len(raw),
                    "md5": raw_md5,
                    "sha256": raw_sha256,
                    "comparison_md5": comparison_md5,
                    "comparison_sha256": comparison_sha256,
                    "canonicalization": TEXT_CANONICALIZATION,
                }
            )
        artifacts = response.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        for artifact in artifacts[:128]:
            if not isinstance(artifact, dict) or artifact.get("downloaded") is not True:
                continue
            encoded = artifact.get("content_base64")
            if not isinstance(encoded, str):
                continue
            try:
                content = base64.b64decode(encoded, validate=True)
            except (binascii.Error, ValueError):
                continue
            md5, sha256 = content_digests(content)
            reported_sha256 = artifact.get("sha256")
            if isinstance(reported_sha256, str) and reported_sha256 and reported_sha256 != sha256:
                continue
            observations.append(
                {
                    "kind": "FILE",
                    "filename": str(artifact.get("filename", ""))[:255],
                    "size": len(content),
                    "md5": md5,
                    "sha256": sha256,
                    "comparison_md5": md5,
                    "comparison_sha256": sha256,
                    "canonicalization": BINARY_CANONICALIZATION,
                }
            )
    return observations[:256]


def _parent_assessment_id(assessment: Assessment) -> uuid.UUID:
    raw = dict(assessment.policy or {}).get("moodle_parent_assessment_id")
    try:
        return uuid.UUID(str(raw)) if raw else assessment.id
    except (TypeError, ValueError, AttributeError):
        return assessment.id


def _activity_identity(
    assessment: Assessment,
    receipt: dict[str, Any],
) -> tuple[str, str]:
    """Return the immutable Moodle activity key carried by import metadata.

    New historical imports persist the module/cmid on the Submission receipt.
    The assessment policy remains a safe fallback for submissions imported by
    the immediately preceding release.
    """

    policy = dict(assessment.policy or {})
    policy_mapping = policy.get("lms_activity_mapping")
    policy_mapping = policy_mapping if isinstance(policy_mapping, dict) else {}
    module = str(receipt.get("lms_module") or policy_mapping.get("module") or "").lower()
    activity_id = str(receipt.get("lms_cmid") or policy_mapping.get("cmid") or "").strip()
    return module, activity_id


def _fingerprint_matches_envelope(
    fingerprint: LMSSubmissionFingerprint,
    observations: list[dict[str, Any]],
) -> bool:
    """Compare the complete Moodle response envelope with one delivery.

    A matching managed file beside an additional Moodle answer is not the same
    response.  Requiring one observation also catches a student adding another
    attachment while retaining the original managed artifact.
    """

    if len(observations) != 1:
        return False
    observation = observations[0]
    expected_kind = (
        "ONLINE_TEXT" if fingerprint.answer_transport in ONLINE_TEXT_TRANSPORTS else "FILE"
    )
    if observation.get("kind") != expected_kind:
        return False
    if expected_kind == "FILE" and observation.get("filename") != fingerprint.artifact_filename:
        return False
    # SHA-256 is authoritative. MD5 remains persisted only for operational
    # compatibility and must not decide whether a student's answer is trusted.
    return observation.get("comparison_sha256") == fingerprint.comparison_sha256


async def submission_origin_verification(
    db: AsyncSession,
    *,
    submission: Submission,
    attempt: Attempt,
    assessment: Assessment,
) -> dict[str, Any]:
    """Compare an imported Moodle response with the last successful local delivery."""

    parent_assessment_id = _parent_assessment_id(assessment)
    if submission.source != "MOODLE_IMPORT":
        expected = await db.scalar(
            select(LMSSubmissionFingerprint)
            .where(LMSSubmissionFingerprint.submission_id == submission.id)
            .order_by(LMSSubmissionFingerprint.delivered_at.desc())
        )
        if expected is None:
            return {
                "state": "PENDING",
                "transport": None,
                "checked_at": None,
                "message": "Ответ создан в Мехмат.Практикуме, но LMS ещё не подтвердила доставку.",
            }
        return {
            "state": "PENDING",
            "transport": expected.answer_transport,
            "checked_at": expected.delivered_at,
            "message": (
                "LMS подтвердила доставку ответа из Мехмат.Практикума; "
                "совпадение содержимого будет проверено при обратной синхронизации."
            ),
        }

    receipt = dict(submission.external_receipt or {})
    module, external_activity_id = _activity_identity(assessment, receipt)
    conditions = [
        LMSSubmissionFingerprint.course_id == assessment.course_id,
        LMSSubmissionFingerprint.principal_id == attempt.principal_id,
    ]
    if module in {"quiz", "assign"} and external_activity_id:
        conditions.extend(
            [
                LMSSubmissionFingerprint.module == module,
                LMSSubmissionFingerprint.external_activity_id == external_activity_id,
            ]
        )
    else:
        # Compatibility fallback for receipts created before activity identity
        # was persisted. It may find candidates, but cannot produce a red result
        # unless the remaining correlation is unambiguous below.
        conditions.append(LMSSubmissionFingerprint.assessment_id == parent_assessment_id)

    activity_candidates = list(
        (
            await db.scalars(
                select(LMSSubmissionFingerprint)
                .where(*conditions)
                .order_by(LMSSubmissionFingerprint.delivered_at.desc())
                .limit(50)
            )
        ).all()
    )
    parent_attempt_id = str(receipt.get("moodle_parent_attempt_id", "")).strip()
    response_id = str(receipt.get("moodle_response_id", "")).strip()
    if module == "quiz":
        if not parent_attempt_id or not response_id:
            if not activity_candidates:
                return {
                    "state": "EXTERNAL_ORIGIN",
                    "transport": None,
                    "checked_at": submission.updated_at,
                    "message": (
                        "Работа сдана напрямую через LMS; контрольной суммы Мехмат.Практикума нет."
                    ),
                }
            return {
                "state": "UNAVAILABLE",
                "transport": activity_candidates[0].answer_transport,
                "checked_at": submission.updated_at,
                "message": (
                    "LMS не передала номер попытки или задания; безопасное сравнение невозможно."
                ),
            }
        candidates = [
            row
            for row in activity_candidates
            if row.external_attempt_id == parent_attempt_id
            and row.external_question_slot == response_id
        ]
    else:
        # Assignment receipts do not expose a stable remote submission id.
        # A future local delivery must never be attached to an older direct-LMS answer.
        cutoff = _as_utc(submission.submitted_at) + timedelta(minutes=15)
        candidates = [row for row in activity_candidates if _as_utc(row.delivered_at) <= cutoff]
    if not candidates:
        return {
            "state": "EXTERNAL_ORIGIN",
            "transport": None,
            "checked_at": submission.updated_at,
            "message": "Работа сдана напрямую через LMS; контрольной суммы Мехмат.Практикума нет.",
        }

    expected = candidates[0]
    if receipt.get("lms_response_observations_complete") is not True:
        return {
            "state": "UNAVAILABLE",
            "transport": expected.answer_transport,
            "checked_at": submission.updated_at,
            "message": (
                "LMS передала ответ не полностью; сравнение будет повторено после синхронизации."
            ),
        }
    raw_observations = receipt.get("lms_response_observations")
    observations = (
        [row for row in raw_observations if isinstance(row, dict)]
        if isinstance(raw_observations, list)
        else []
    )
    matched_candidate = next(
        (row for row in candidates if _fingerprint_matches_envelope(row, observations)),
        None,
    )
    if matched_candidate is not None:
        return {
            "state": "VERIFIED",
            "transport": matched_candidate.answer_transport,
            "checked_at": submission.updated_at,
            "message": (
                "Ответ написан и отправлен через Мехмат.Практикум; содержимое в LMS совпадает."
            ),
        }

    if module not in {"quiz", "assign"}:
        return {
            "state": "UNAVAILABLE",
            "transport": expected.answer_transport,
            "checked_at": submission.updated_at,
            "message": (
                "LMS не передала идентификатор типа задания; "
                "расхождение нельзя подтвердить однозначно."
            ),
        }

    return {
        "state": "MISMATCH",
        "transport": expected.answer_transport,
        "checked_at": submission.updated_at,
        "message": "Ответ в LMS отличается от ответа, отправленного через Мехмат.Практикум.",
    }
