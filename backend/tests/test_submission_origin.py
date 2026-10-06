from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.api.reviews import get_submission
from app.auth.context import AuthContext
from app.models.attempts import (
    Attempt,
    EditEvent,
    RunRequest,
    Snapshot,
    Submission,
    Workspace,
    WorkspaceFile,
)
from app.models.courses import Course
from app.models.identity import ExternalPrincipal, LMSConnection
from app.models.integration import LMSSubmissionFingerprint, SyncOutbox
from app.models.tasks import Assessment
from app.services.submission_origin import (
    BINARY_CANONICALIZATION,
    TEXT_CANONICALIZATION,
    canonical_moodle_text_bytes,
    comparison_content,
    content_digests,
    historical_response_observations,
    submission_origin_verification,
)


async def _submission(
    db,
    *,
    assessment: Assessment,
    principal: ExternalPrincipal,
    sequence: int,
    source: str,
    submitted_at: datetime,
    external_receipt: dict | None = None,
) -> tuple[Attempt, Submission, Snapshot]:
    attempt = Attempt(
        assessment_id=assessment.id,
        principal_id=principal.id,
        sequence=sequence,
        state="SUBMITTED",
        started_at=submitted_at - timedelta(minutes=30),
        submitted_at=submitted_at,
        submission_source=source,
    )
    db.add(attempt)
    await db.flush()
    workspace = Workspace(
        attempt_id=attempt.id,
        current_revision=1,
        current_hash=f"{sequence:064x}",
    )
    db.add(workspace)
    await db.flush()
    snapshot = Snapshot(
        workspace_id=workspace.id,
        revision=1,
        manifest_hash=f"{sequence + 10:064x}",
        files=[],
        reason="SUBMISSION",
    )
    db.add(snapshot)
    await db.flush()
    submission = Submission(
        attempt_id=attempt.id,
        snapshot_id=snapshot.id,
        revision=1,
        source=source,
        submitted_at=submitted_at,
        lms_export_state="DELIVERED" if source != "MOODLE_IMPORT" else "IMPORTED",
        external_receipt=external_receipt or {},
    )
    db.add(submission)
    await db.flush()
    return attempt, submission, snapshot


async def _origin_case(
    db,
    *,
    answer_transport: str = "ESSAY_ONLINE_TEXT",
    delivered_content: bytes = b"int main() {}",
    imported_receipt: dict | None = None,
    with_fingerprint: bool = True,
) -> tuple[Submission, Attempt, Assessment]:
    now = datetime.now(UTC)
    connection = LMSConnection(
        name="Moodle",
        provider="MOODLE",
        base_url="https://moodle.test",
    )
    db.add(connection)
    await db.flush()
    principal = ExternalPrincipal(
        connection_id=connection.id,
        external_subject="student-1",
        display_name="Student One",
    )
    db.add(principal)
    await db.flush()
    course = Course(
        connection_id=connection.id,
        external_id="course-549",
        title="C++",
        catalog_enabled=True,
    )
    db.add(course)
    await db.flush()
    assessment = Assessment(
        course_id=course.id,
        type="TEST",
        title="Independent work",
        status="PUBLISHED",
        created_by_id=principal.id,
    )
    db.add(assessment)
    await db.flush()

    local_attempt, local_submission, local_snapshot = await _submission(
        db,
        assessment=assessment,
        principal=principal,
        sequence=1,
        source="STUDENT",
        submitted_at=now,
    )
    module = "assign" if answer_transport.startswith("ASSIGN_") else "quiz"
    receipt = {
        "lms_module": module,
        "lms_cmid": "23461",
        "moodle_parent_attempt_id": "remote-attempt-17" if module == "quiz" else "",
        "moodle_response_id": "1" if module == "quiz" else "submission",
    }
    receipt.update(imported_receipt or {})
    if "lms_response_observations" in receipt:
        receipt.setdefault("lms_response_observations_complete", True)
    imported_attempt, imported_submission, _ = await _submission(
        db,
        assessment=assessment,
        principal=principal,
        sequence=2,
        source="MOODLE_IMPORT",
        submitted_at=now + timedelta(minutes=1),
        external_receipt=receipt,
    )

    if with_fingerprint:
        outbox = SyncOutbox(
            connection_id=connection.id,
            course_id=course.id,
            attempt_id=local_attempt.id,
            event_type="SUBMISSION",
            aggregate_type="SUBMISSION",
            aggregate_id=local_submission.id,
            idempotency_key=f"origin-{local_submission.id}",
            state="DELIVERED",
            delivered_at=now,
        )
        db.add(outbox)
        await db.flush()
        artifact_md5, artifact_sha256 = content_digests(delivered_content)
        compared, canonicalization = comparison_content(delivered_content, answer_transport)
        comparison_md5, comparison_sha256 = content_digests(compared)
        db.add(
            LMSSubmissionFingerprint(
                outbox_id=outbox.id,
                connection_id=connection.id,
                course_id=course.id,
                assessment_id=assessment.id,
                principal_id=principal.id,
                attempt_id=local_attempt.id,
                submission_id=local_submission.id,
                snapshot_id=local_snapshot.id,
                module=module,
                external_activity_id="23461",
                external_attempt_id="remote-attempt-17",
                external_question_slot="1",
                answer_transport=answer_transport,
                artifact_filename=(
                    "main.cpp"
                    if answer_transport in {"ESSAY_ONLINE_TEXT", "ASSIGN_ONLINE_TEXT"}
                    else "submission.zip"
                ),
                artifact_size=len(delivered_content),
                artifact_md5=artifact_md5,
                artifact_sha256=artifact_sha256,
                comparison_md5=comparison_md5,
                comparison_sha256=comparison_sha256,
                canonicalization=canonicalization,
                delivered_at=now,
            )
        )
        await db.flush()

    return imported_submission, imported_attempt, assessment


async def _additional_fingerprint(
    db,
    *,
    template: LMSSubmissionFingerprint,
    delivered_content: bytes,
    delivered_at: datetime,
    question_slot: str | None = None,
) -> LMSSubmissionFingerprint:
    outbox = SyncOutbox(
        connection_id=template.connection_id,
        course_id=template.course_id,
        attempt_id=template.attempt_id,
        event_type="SUBMISSION",
        aggregate_type="SUBMISSION",
        aggregate_id=template.submission_id,
        idempotency_key=f"origin-extra-{template.id}-{delivered_at.timestamp()}",
        state="DELIVERED",
        delivered_at=delivered_at,
    )
    db.add(outbox)
    await db.flush()
    artifact_md5, artifact_sha256 = content_digests(delivered_content)
    compared, canonicalization = comparison_content(
        delivered_content,
        template.answer_transport,
    )
    comparison_md5, comparison_sha256 = content_digests(compared)
    fingerprint = LMSSubmissionFingerprint(
        outbox_id=outbox.id,
        connection_id=template.connection_id,
        course_id=template.course_id,
        assessment_id=template.assessment_id,
        principal_id=template.principal_id,
        attempt_id=template.attempt_id,
        submission_id=template.submission_id,
        snapshot_id=template.snapshot_id,
        module=template.module,
        external_activity_id=template.external_activity_id,
        external_attempt_id=template.external_attempt_id,
        external_question_slot=(
            question_slot if question_slot is not None else template.external_question_slot
        ),
        answer_transport=template.answer_transport,
        artifact_filename=template.artifact_filename,
        artifact_size=len(delivered_content),
        artifact_md5=artifact_md5,
        artifact_sha256=artifact_sha256,
        comparison_md5=comparison_md5,
        comparison_sha256=comparison_sha256,
        canonicalization=canonicalization,
        delivered_at=delivered_at,
    )
    db.add(fingerprint)
    await db.flush()
    return fingerprint


def test_canonical_moodle_text_preserves_program_formatting() -> None:
    source = "\r\n\tint main() {\r\n\r\n  return\u00a00;\t \r\n}\r\n\r\n"

    assert canonical_moodle_text_bytes(source) == (b"\tint main() {\n\n  return 0;\t \n}")
    assert canonical_moodle_text_bytes(source.encode()) == canonical_moodle_text_bytes(source)


async def _local_writing_events(db):
    delivery = await db.scalar(select(LMSSubmissionFingerprint))
    snapshot = await db.get(Snapshot, delivery.snapshot_id)
    snapshot.revision = 3
    snapshot.created_at = delivery.delivered_at
    path = "src/main.cpp"
    main = WorkspaceFile(
        workspace_id=snapshot.workspace_id, path=path,
        content="int main() {}", content_hash="a" * 64,
    )
    deleted = WorkspaceFile(
        workspace_id=snapshot.workspace_id, path="include/unused.h", content="",
        content_hash="b" * 64, created_revision=1, deleted_revision=3,
    )
    db.add_all([main, deleted])
    await db.flush()
    snapshot.files = [{
        "id": str(main.id), "path": path,
        "content": main.content, "content_hash": main.content_hash,
    }]
    rows = [
        (1, deleted, "FILE_CREATE", {
            "type": "file_create", "path": deleted.path, "content_hash": "b" * 64,
        }),
        (2, main, "REPLACE_CONTENT", {
            "type": "contiguous_delta", "offset": 0, "delete_count": 0,
            "insert_text": main.content, "offset_encoding": "unicode_codepoint",
            "previous_content_hash": "0" * 64, "content_hash": "a" * 64,
        }),
        (3, deleted, "FILE_DELETE", {
            "type": "file_delete", "path": deleted.path, "content_hash": "b" * 64,
        }),
        (4, main, "REPLACE_CONTENT", {
            "type": "contiguous_delta", "offset": 0, "delete_count": 0,
            "insert_text": "// later", "offset_encoding": "unicode_codepoint",
            "previous_content_hash": "a" * 64, "content_hash": "c" * 64,
        }),
    ]
    for sequence, file, event_type, change in rows:
        db.add(EditEvent(
            workspace_id=snapshot.workspace_id, epoch=1, sequence=sequence,
            client_request_id=f"history-{sequence}", event_type=event_type, file_id=file.id,
            changes=[change], event_hash=f"{sequence:064x}",
            received_at=delivery.delivered_at + timedelta(seconds=sequence - 4),
        ))
    for seconds, origin in [
        (-1, "STUDENT_ATTEMPT"), (10, "STUDENT_ATTEMPT"), (-1, "TEACHER_EXPERIMENT"),
    ]:
        db.add(RunRequest(
            attempt_id=delivery.attempt_id, requested_by_id=delivery.principal_id,
            origin=origin, revision=3, build_profile="cpp-clang-c++20-multi",
            created_at=delivery.delivered_at + timedelta(seconds=seconds),
        ))
    db.add(Snapshot(
        workspace_id=snapshot.workspace_id, revision=4, manifest_hash="c" * 64,
        files=[], reason="PERIODIC", created_at=delivery.delivered_at + timedelta(seconds=10),
    ))
    await db.flush()
    return delivery


async def _review_import(db, submission, attempt):
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    snapshot.reason = "LMS_IMPORT"
    await db.flush()
    return await get_submission(submission.id, AuthContext(
        principal_id=attempt.principal_id, display_name="System reviewer",
        session_id=uuid.uuid4(), session_key="test", roles=("TEACHER",),
        capabilities=("SYSTEM_SETTINGS",),
    ), db)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["ESSAY_ONLINE_TEXT", "ASSIGN_FILE"])
async def test_verified_moodle_readback_recovers_file_history_from_exact_local_snapshot(
    db, transport,
):
    content = b"int main() {}"
    response = {"answer_text": content.decode()} if transport == "ESSAY_ONLINE_TEXT" else {
        "artifacts": [{
            "filename": "submission.zip", "downloaded": True,
            "content_base64": base64.b64encode(content).decode(),
        }],
    }
    submission, attempt, _ = await _origin_case(
        db, answer_transport=transport, delivered_content=content,
        imported_receipt={
            "lms_response_observations": historical_response_observations({
                "responses": [response],
            }),
        },
    )
    await _local_writing_events(db)

    result = await _review_import(db, submission, attempt)

    assert result.origin_verification.state == "VERIFIED"
    edits = [row for row in result.history if row.type == "edit"]
    assert [(row.label, row.file_path, row.revision) for row in edits] == [
        ("Создание файла", "include/unused.h", 1),
        ("Изменение файла", "src/main.cpp", 2),
        ("Удаление файла", "include/unused.h", 3),
    ]
    assert len([row for row in result.history if row.type == "run"]) == 1
    assert max(row.revision for row in result.history) == 3
    assert result.history[-1].label == "Синхронизировано с Moodle"
    assert all(row.detail != "История редактирования в Moodle недоступна" for row in result.history)
    # Read-back repairs display on every read without mutating the imported answer.
    again = await _review_import(db, submission, attempt)
    assert [row.id for row in again.history] == [row.id for row in result.history]
    assert again.files == result.files == []
    assert submission.source == "MOODLE_IMPORT"


@pytest.mark.asyncio
async def test_checkpoint_history_is_available_without_a_local_submission(db):
    observations = historical_response_observations({
        "responses": [{"answer_text": "int main() {}"}],
    })
    submission, attempt, _ = await _origin_case(
        db, imported_receipt={"lms_response_observations": observations},
    )
    delivery = await _local_writing_events(db)
    local_submission = await db.get(Submission, delivery.submission_id)
    delivery.submission_id = None
    await db.flush()
    await db.delete(local_submission)
    await db.flush()

    result = await _review_import(db, submission, attempt)

    assert len([row for row in result.history if row.type == "edit"]) == 3
    assert result.history[-1].label == "Синхронизировано с Moodle"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", [
    "changed", "incomplete", "other_attempt", "other_question",
    "other_student", "other_course", "no_delivery", "missing_identity",
])
async def test_unverified_moodle_history_never_borrows_another_answer_history(db, case):
    observations = historical_response_observations({
        "responses": [{"answer_text": "int main() {}"}],
    })
    receipt = {"lms_response_observations": observations}
    if case == "changed":
        receipt["lms_response_observations"] = historical_response_observations({
            "responses": [{"answer_text": "different code"}],
        })
    elif case == "incomplete":
        receipt["lms_response_observations_complete"] = False
    elif case == "other_attempt":
        receipt["moodle_parent_attempt_id"] = "another-attempt"
    elif case == "other_question":
        receipt["moodle_response_id"] = "2"
    elif case == "missing_identity":
        receipt["lms_cmid"] = ""
        receipt["lms_module"] = ""
    submission, attempt, assessment = await _origin_case(db, imported_receipt=receipt)
    delivery = await _local_writing_events(db)
    if case == "other_student":
        principal = await db.get(ExternalPrincipal, attempt.principal_id)
        other = ExternalPrincipal(
            connection_id=principal.connection_id, external_subject="other", display_name="Other",
        )
        db.add(other)
        await db.flush()
        delivery.principal_id = other.id
    elif case == "other_course":
        course = await db.get(Course, assessment.course_id)
        other = Course(
            connection_id=course.connection_id, external_id="another-course", title="Other",
        )
        db.add(other)
        await db.flush()
        delivery.course_id = other.id
    elif case == "no_delivery":
        await db.delete(delivery)
    await db.flush()

    result = await _review_import(db, submission, attempt)

    assert len(result.history) == 1
    assert result.history[0].label == "Импортировано из Moodle"
    assert result.history[0].detail == "История редактирования в Moodle недоступна"


def test_historical_response_observations_cover_essay_and_exact_file() -> None:
    attachment = b"PK\x03\x04\x00student archive\xff"
    response = {
        "responses": [
            {
                "answer_text": "\r\n\tint main() {\r\n  return 0;\r\n}\r\n",
                "answer_complete": True,
                "artifacts": [
                    {
                        "filename": "solution.zip",
                        "downloaded": True,
                        "content_base64": base64.b64encode(attachment).decode(),
                        "sha256": hashlib.sha256(attachment).hexdigest(),
                    },
                    {
                        "filename": "corrupted.zip",
                        "downloaded": True,
                        "content_base64": base64.b64encode(attachment).decode(),
                        "sha256": "0" * 64,
                    },
                ],
            }
        ]
    }

    observations = historical_response_observations(response)

    assert [row["kind"] for row in observations] == ["ONLINE_TEXT", "FILE"]
    essay, artifact = observations
    essay_compared = b"\tint main() {\n  return 0;\n}"
    assert essay["canonicalization"] == TEXT_CANONICALIZATION
    assert essay["comparison_md5"] == content_digests(essay_compared)[0]
    assert essay["comparison_sha256"] == content_digests(essay_compared)[1]
    assert artifact == {
        "kind": "FILE",
        "filename": "solution.zip",
        "size": len(attachment),
        "md5": content_digests(attachment)[0],
        "sha256": content_digests(attachment)[1],
        "comparison_md5": content_digests(attachment)[0],
        "comparison_sha256": content_digests(attachment)[1],
        "canonicalization": BINARY_CANONICALIZATION,
    }


@pytest.mark.asyncio
async def test_imported_submission_without_local_delivery_is_external_origin(db) -> None:
    submission, attempt, assessment = await _origin_case(db, with_fingerprint=False)

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "EXTERNAL_ORIGIN"
    assert result["transport"] is None


@pytest.mark.asyncio
async def test_local_delivery_waits_for_historical_readback_before_green(db) -> None:
    await _origin_case(db)
    fingerprint = await db.scalar(select(LMSSubmissionFingerprint))
    assert fingerprint is not None and fingerprint.submission_id is not None
    submission = await db.get(Submission, fingerprint.submission_id)
    attempt = await db.get(Attempt, fingerprint.attempt_id)
    assessment = await db.get(Assessment, fingerprint.assessment_id)
    assert submission is not None and attempt is not None and assessment is not None

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "PENDING"
    assert "обратной синхронизации" in result["message"]


@pytest.mark.asyncio
async def test_imported_submission_without_observed_content_is_unavailable(db) -> None:
    submission, attempt, assessment = await _origin_case(db)

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "UNAVAILABLE"
    assert result["transport"] == "ESSAY_ONLINE_TEXT"


@pytest.mark.asyncio
async def test_imported_essay_with_changed_content_is_mismatch(db) -> None:
    observations = historical_response_observations(
        {"responses": [{"answer_text": "int main() { return 7; }"}]}
    )
    submission, attempt, assessment = await _origin_case(
        db,
        delivered_content=b"int main() { return 0; }",
        imported_receipt={"lms_response_observations": observations},
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "MISMATCH"
    assert result["transport"] == "ESSAY_ONLINE_TEXT"


@pytest.mark.asyncio
async def test_imported_essay_with_equivalent_moodle_formatting_is_verified(db) -> None:
    delivered = b"\tint main() {\r\n  return 0;\r\n}\r\n"
    observations = historical_response_observations(
        {"responses": [{"answer_text": "\n\tint main() {\n  return 0;\n}\n\n"}]}
    )
    submission, attempt, assessment = await _origin_case(
        db,
        delivered_content=delivered,
        imported_receipt={"lms_response_observations": observations},
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "VERIFIED"
    assert result["transport"] == "ESSAY_ONLINE_TEXT"


@pytest.mark.asyncio
async def test_imported_file_with_identical_bytes_is_verified(db) -> None:
    archive = b"PK\x03\x04\x00\x01student solution\xff"
    observations = historical_response_observations(
        {
            "responses": [
                {
                    "artifacts": [
                        {
                            "filename": "submission.zip",
                            "downloaded": True,
                            "content_base64": base64.b64encode(archive).decode(),
                            "sha256": hashlib.sha256(archive).hexdigest(),
                        }
                    ]
                }
            ]
        }
    )
    submission, attempt, assessment = await _origin_case(
        db,
        answer_transport="ASSIGN_FILE",
        delivered_content=archive,
        imported_receipt={"lms_response_observations": observations},
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "VERIFIED"
    assert result["transport"] == "ASSIGN_FILE"


@pytest.mark.asyncio
async def test_quiz_matches_activity_attempt_slot_and_all_candidates(db) -> None:
    delivered = b"int main() { return 2; }"
    observations = historical_response_observations(
        {"responses": [{"answer_text": delivered.decode()}]}
    )
    submission, attempt, assessment = await _origin_case(
        db,
        delivered_content=b"int main() { return 1; }",
        imported_receipt={
            "moodle_response_id": "2",
            "lms_response_observations": observations,
        },
    )
    template = await db.scalar(select(LMSSubmissionFingerprint))
    assert template is not None
    await _additional_fingerprint(
        db,
        template=template,
        delivered_content=delivered,
        delivered_at=template.delivered_at + timedelta(seconds=1),
        question_slot="2",
    )
    # A newer checkpoint for the same question must not hide an older exact
    # delivery that still matches Moodle.
    await _additional_fingerprint(
        db,
        template=template,
        delivered_content=b"int main() { return 99; }",
        delivered_at=template.delivered_at + timedelta(seconds=2),
        question_slot="2",
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "VERIFIED"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "receipt_patch",
    [
        {"lms_cmid": "99999"},
        {"moodle_parent_attempt_id": "another-attempt"},
        {"moodle_response_id": "99"},
    ],
)
async def test_quiz_requires_exact_activity_attempt_and_slot(db, receipt_patch) -> None:
    delivered = b"int main() {}"
    observations = historical_response_observations(
        {"responses": [{"answer_text": delivered.decode()}]}
    )
    submission, attempt, assessment = await _origin_case(
        db,
        delivered_content=delivered,
        imported_receipt={
            **receipt_patch,
            "lms_response_observations": observations,
        },
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "EXTERNAL_ORIGIN"


@pytest.mark.asyncio
async def test_complete_quiz_envelope_rejects_extra_attachment(db) -> None:
    delivered = b"int main() { return 0; }"
    extra = b"unmanaged"
    observations = historical_response_observations(
        {
            "responses": [
                {
                    "answer_text": delivered.decode(),
                    "artifacts": [
                        {
                            "filename": "extra.txt",
                            "downloaded": True,
                            "content_base64": base64.b64encode(extra).decode(),
                            "sha256": hashlib.sha256(extra).hexdigest(),
                        }
                    ],
                }
            ]
        }
    )
    submission, attempt, assessment = await _origin_case(
        db,
        delivered_content=delivered,
        imported_receipt={"lms_response_observations": observations},
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "MISMATCH"


@pytest.mark.asyncio
async def test_incomplete_moodle_envelope_is_unavailable(db) -> None:
    observations = historical_response_observations(
        {"responses": [{"answer_text": "int main() {}"}]}
    )
    submission, attempt, assessment = await _origin_case(
        db,
        imported_receipt={
            "lms_response_observations": observations,
            "lms_response_observations_complete": False,
        },
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "UNAVAILABLE"


@pytest.mark.asyncio
async def test_assignment_mismatch_checks_every_managed_checkpoint(db) -> None:
    remote = b"remote answer"
    observations = historical_response_observations(
        {
            "responses": [
                {
                    "artifacts": [
                        {
                            "filename": "submission.zip",
                            "downloaded": True,
                            "content_base64": base64.b64encode(remote).decode(),
                            "sha256": hashlib.sha256(remote).hexdigest(),
                        }
                    ]
                }
            ]
        }
    )
    submission, attempt, assessment = await _origin_case(
        db,
        answer_transport="ASSIGN_FILE",
        delivered_content=b"first managed answer",
        imported_receipt={"lms_response_observations": observations},
    )
    template = await db.scalar(select(LMSSubmissionFingerprint))
    assert template is not None
    await _additional_fingerprint(
        db,
        template=template,
        delivered_content=b"second managed answer",
        delivered_at=template.delivered_at + timedelta(seconds=1),
    )

    result = await submission_origin_verification(
        db,
        submission=submission,
        attempt=attempt,
        assessment=assessment,
    )

    assert result["state"] == "MISMATCH"
