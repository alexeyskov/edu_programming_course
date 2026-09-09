from __future__ import annotations

import base64
import copy
import hashlib
import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy import delete, func, select

from app.api.reviews import _submission_review_group, list_submissions
from app.api.system import list_sync_outbox
from app.auth.context import AuthContext
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    encrypt_moodle_browser_state,
)
from app.models.attempts import Attempt, Snapshot, Submission, Workspace, WorkspaceFile
from app.models.courses import Course, CourseMembership
from app.models.enums import AttemptState, CourseRole, SyncOutboxState
from app.models.identity import (
    ExternalPrincipal,
    LMSConnection,
    MoodleCredential,
    TeacherAccessToken,
    TeacherTokenGrant,
)
from app.models.integration import ExternalMapping, SyncOutbox
from app.models.review import ReviewClaim, ReviewDecision
from app.models.tasks import Assessment, AssessmentItem, TaskBankItem, TaskVersion
from app.services import moodle_history as moodle_history_service
from app.services.common import DomainError
from app.services.moodle_attempt_selection import (
    MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE,
)
from app.services.moodle_history import (
    _imported_grade,
    _materialize_flat_historical_submissions,
    enqueue_historical_submission_imports,
    historical_source_files,
    materialize_historical_submissions,
)
from app.services.policy import visible_submission_ids_for_review
from app.services.review import claim_submission, finalize_review
from app.services.sync import (
    ClaimedOutboxEvent,
    ConnectionTarget,
    _BlockedDelivery,
    _BrowserDeliveryResult,
    _prepare_grade,
    process_outbox_once,
)


def _encoded_artifact(filename: str, content: bytes) -> dict[str, Any]:
    return {
        "filename": filename,
        "downloaded": True,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "size_bytes": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _browser_state(marker: str) -> dict[str, Any]:
    return {
        "cookies": [
            {
                "name": "MoodleSession",
                "value": marker,
                "domain": "moodle.example.test",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax",
            }
        ],
        "origins": [],
    }


async def _seed_history_target(
    session_factory,
    *,
    settings=None,
    cmid: int = 777,
) -> dict[str, uuid.UUID]:
    async with session_factory() as db, db.begin():
        connection = LMSConnection(
            name="Moodle",
            provider="MOODLE",
            base_url="https://moodle.example.test",
            config={
                "auth_mode": "PLUGINLESS",
                "pluginless_transport": "PLAYWRIGHT",
            },
        )
        db.add(connection)
        await db.flush()
        teacher = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Teacher",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add_all([teacher, course])
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=teacher.id,
                role=CourseRole.TEACHER.value,
            )
        )
        token = TeacherAccessToken(
            public_id=teacher.id.hex[:16],
            label="History import test",
            secret_hash="$argon2id$test-fixture-not-used-for-login",
            created_by_id=teacher.id,
        )
        db.add(token)
        await db.flush()
        db.add(TeacherTokenGrant(token_id=token.id, principal_id=teacher.id))

        task = TaskBankItem(
            course_id=course.id,
            slug=f"moodle-{cmid}",
            created_by_id=teacher.id,
        )
        db.add(task)
        await db.flush()
        version = TaskVersion(
            item_id=task.id,
            number=1,
            title="Historical Moodle task",
            statement="Imported task",
            content_hash="a" * 64,
            status="PUBLISHED",
            authored_by_id=teacher.id,
        )
        assessment = Assessment(
            course_id=course.id,
            title="Historical Moodle assessment",
            max_score=Decimal("10"),
            status="PUBLISHED",
            created_by_id=teacher.id,
        )
        db.add_all([version, assessment])
        await db.flush()
        db.add(
            AssessmentItem(
                assessment_id=assessment.id,
                task_version_id=version.id,
                position=0,
                points=Decimal("10"),
            )
        )
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type="mod_quiz",
                external_id=str(cmid),
                external_revision="activity-r1",
                metadata_json={"module": "quiz", "cmid": cmid},
            )
        )
        if settings is not None:
            db.add(
                MoodleCredential(
                    connection_id=connection.id,
                    principal_id=teacher.id,
                    kind=BROWSER_STATE_CREDENTIAL_KIND,
                    encrypted_secret=encrypt_moodle_browser_state(
                        _browser_state("before"),
                        settings,
                        connection_id=connection.id,
                        principal_id=teacher.id,
                    ),
                    status="ACTIVE",
                )
            )
        await db.flush()
        return {
            "connection_id": connection.id,
            "course_id": course.id,
            "teacher_id": teacher.id,
            "assessment_id": assessment.id,
        }


def _finished_item(*, revision: str = "attempt-r1") -> dict[str, Any]:
    return {
        "external_id": "quiz:777:attempt:123",
        "external_revision": revision,
        "attempt_id": "attempt:123",
        "user_id": "99",
        "display_name": "Student",
        "state": "FINISHED",
        "submitted_at_epoch": 1_777_000_000,
        "module": "quiz",
        "cmid": 777,
        "grade": "8.5",
        "grade_max": "10",
        "comment": "Checked in Moodle",
        "responses": [
            {
                "response_id": "slot-1",
                "question_text": "Write a program",
                "answer_text": "int main() { return 0; }",
                "answer_complete": True,
                "grade": "8.5",
                "grade_max": "10",
                "comment": "Correct",
                "artifacts": [],
            }
        ],
    }


def _multi_essay_item(*, revision: str = "multi-attempt-r1") -> dict[str, Any]:
    return {
        **_finished_item(revision=revision),
        "grade": "8",
        "grade_max": "10",
        "comment": "Aggregate Quiz feedback",
        "responses": [
            {
                "response_id": "11",
                "question_text": "Implement Time comparison",
                "answer_text": "int first_inline = 1;",
                "answer_complete": True,
                "grade": "3",
                "grade_max": "4",
                "comment": "First question feedback",
                "artifacts": [_encoded_artifact("time.cpp", b"int time_file = 1;\n")],
            },
            {
                "response_id": "12",
                "question_text": "Implement Date comparison",
                "answer_text": "int second_inline = 2;",
                "answer_complete": True,
                "grade": "5",
                "grade_max": "6",
                "comment": "Second question feedback",
                "artifacts": [],
            },
        ],
    }


def test_historical_source_files_safely_converts_inline_zip_and_omissions() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("src/main.cpp", "int from_archive = 1;\n")
        archive.writestr("include/value.hpp", "#pragma once\n")
        archive.writestr("include/detail.inc", "constexpr int detail = 7;\n")
        archive.writestr("fixtures/input.txt", "42\n")
        archive.writestr("other/main.cpp", "int duplicate_name = 2;\n")
        archive.writestr("../escape.cpp", "must not be imported\n")
        archive.writestr("assets/image.png", b"\x89PNG\x00")
        symlink = zipfile.ZipInfo("include/link.hpp")
        symlink.external_attr = 0o120777 << 16
        archive.writestr(symlink, "../outside.hpp")

    files = historical_source_files(
        {
            "responses": [
                {
                    "answer_text": "int inline_answer = 3;",
                    "artifacts": [
                        _encoded_artifact("solution.zip", buffer.getvalue()),
                        _encoded_artifact("extra.h", b"#pragma once\n"),
                        _encoded_artifact("values.txt", b"1 2 3\n"),
                        {
                            "filename": "missing.cpp",
                            "downloaded": False,
                            "omission_reason": "DOWNLOAD_FAILED",
                        },
                    ],
                }
            ]
        }
    )

    by_path = {item["path"]: item["content"] for item in files}
    assert by_path == {
        "src/main.cpp": "int from_archive = 1;\n",
        "include/value.hpp": "#pragma once\n",
        "include/detail.inc": "constexpr int detail = 7;\n",
        "fixtures/input.txt": "42\n",
        "other/main.cpp": "int duplicate_name = 2;\n",
        "extra.h": "#pragma once\n",
        "values.txt": "1 2 3\n",
        "moodle-online-text.cpp": "int inline_answer = 3;",
    }
    assert "include/link.hpp" not in by_path
    assert all(".." not in path for path in by_path)
    assert all(
        item["content_hash"] == hashlib.sha256(item["content"].encode()).hexdigest()
        for item in files
    )

    omitted = historical_source_files(
        {
            "responses": [
                {
                    "answer_complete": False,
                    "artifacts": [{"filename": "missing.cpp", "downloaded": False}],
                }
            ]
        }
    )
    assert [item["path"] for item in omitted] == ["moodle-import.txt"]
    assert "не был доступен" in omitted[0]["content"]

    whitespace = historical_source_files(
        {
            "responses": [
                {
                    "answer_text": "\tint main() {\n\t    int value{};  \n\t}\n",
                    "artifacts": [],
                }
            ]
        }
    )
    assert whitespace[0]["content"] == "\tint main() {\n\t    int value{};  \n\t}\n"


def test_historical_source_files_extracts_bounded_7z_project(monkeypatch) -> None:
    members = {
        "src/main.cpp": b"int main() { return 0; }\n",
        "include/value.hpp": b"#pragma once\n",
        "fixtures/input.txt": b"42\n",
        "notes/readme.md": b"not a workspace source\n",
        "../escape.cpp": b"must not escape\n",
        "include/link.hpp": b"../outside.hpp",
    }

    class FakeSevenZipFile:
        def __init__(self, _stream, *, mode: str) -> None:
            assert mode == "r"

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def list(self):
            return [
                SimpleNamespace(
                    filename=name,
                    uncompressed=len(content),
                    is_directory=False,
                    is_symlink=name == "include/link.hpp",
                )
                for name, content in members.items()
            ]

        def extract(self, *, targets, factory) -> None:
            for target in targets:
                assert PurePosixPath(target).is_absolute() is False
                factory.create(target).write(members[target])

    monkeypatch.setattr(
        moodle_history_service,
        "_py7zr",
        SimpleNamespace(SevenZipFile=FakeSevenZipFile),
    )
    omissions: list[dict[str, str]] = []
    files = historical_source_files(
        {
            "responses": [
                {
                    "response_id": "essay-4",
                    "answer_text": "",
                    "artifacts": [_encoded_artifact("SAM4.7z", b"valid-7z-fixture")],
                }
            ]
        },
        import_omissions=omissions,
    )

    assert {row["path"]: row["content"] for row in files} == {
        "src/main.cpp": "int main() { return 0; }\n",
        "include/value.hpp": "#pragma once\n",
        "fixtures/input.txt": "42\n",
    }
    assert omissions == []


def test_historical_source_files_extracts_real_7z_project() -> None:
    """Keep the declared py7zr dependency compatible with real Moodle archives."""

    assert moodle_history_service._py7zr is not None
    archive_bytes = io.BytesIO()
    with moodle_history_service._py7zr.SevenZipFile(archive_bytes, mode="w") as archive:
        archive.writestr(b"int main() { return 0; }\n", "src/main.cpp")
        archive.writestr(b"#pragma once\n", "include/value.hpp")
        archive.writestr(b"42\n", "fixtures/input.txt")
        archive.writestr(b"not imported\n", "notes/readme.md")

    omissions: list[dict[str, str]] = []
    files = historical_source_files(
        {
            "responses": [
                {
                    "response_id": "essay-real-7z",
                    "answer_text": "",
                    "artifacts": [
                        _encoded_artifact("student-project.7z", archive_bytes.getvalue())
                    ],
                }
            ]
        },
        import_omissions=omissions,
    )

    assert {row["path"]: row["content"] for row in files} == {
        "src/main.cpp": "int main() { return 0; }\n",
        "include/value.hpp": "#pragma once\n",
        "fixtures/input.txt": "42\n",
    }
    assert omissions == []


def test_historical_7z_without_library_is_explicit_not_generic(monkeypatch) -> None:
    monkeypatch.setattr(moodle_history_service, "_py7zr", None)
    omissions: list[dict[str, str]] = []
    files = historical_source_files(
        {
            "responses": [
                {
                    "response_id": "essay-4",
                    "answer_text": "",
                    "artifacts": [_encoded_artifact("SAM4.7z", b"7z-content")],
                }
            ]
        },
        import_omissions=omissions,
    )

    assert [row["path"] for row in files] == ["moodle-archive-import-error.txt"]
    assert "SAM4.7z" in files[0]["content"]
    assert "SEVEN_ZIP_SUPPORT_UNAVAILABLE" in files[0]["content"]
    assert omissions == [
        {
            "kind": "ATTACHMENT_ARCHIVE",
            "response_id": "essay-4",
            "filename": "SAM4.7z",
            "reason": "SEVEN_ZIP_SUPPORT_UNAVAILABLE",
        }
    ]


async def test_materialized_7z_capability_failure_marks_source_incomplete(
    app_bundle,
    monkeypatch,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _finished_item()
    item["responses"][0]["answer_text"] = ""
    item["responses"][0]["artifacts"] = [_encoded_artifact("SAM4.7z", b"7z-content")]
    monkeypatch.setattr(moodle_history_service, "_py7zr", None)

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        submission = await db.scalar(select(Submission))
        workspace_file = await db.scalar(select(WorkspaceFile))

    assert stats.created == 1
    assert submission is not None and workspace_file is not None
    assert workspace_file.path == "moodle-archive-import-error.txt"
    assert submission.external_receipt["source_complete"] is False
    assert submission.external_receipt["source_omissions"] == [
        {
            "kind": "ATTACHMENT_ARCHIVE",
            "response_id": "slot-1",
            "filename": "SAM4.7z",
            "reason": "SEVEN_ZIP_SUPPORT_UNAVAILABLE",
        }
    ]


def test_imported_grade_normalizes_and_caps_remote_scales() -> None:
    assert _imported_grade("85", "100", Decimal("10")) == (
        Decimal("8.50"),
        "PROPORTIONAL_TO_REMOTE_MAX",
    )
    assert _imported_grade("120", "100", Decimal("10")) == (
        Decimal("10.00"),
        "PROPORTIONAL_TO_REMOTE_MAX_CLAMPED",
    )
    assert _imported_grade("85", None, Decimal("10")) == (
        Decimal("10.00"),
        "DIRECT_LOCAL_SCALE_CLAMPED",
    )
    assert _imported_grade("-1", "100", Decimal("10")) == (None, None)


async def test_materialize_creates_idempotent_submission_and_imported_applied_grade(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _finished_item()

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        created = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        unchanged = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    assert created.created == 1
    assert unchanged.unchanged == 1
    async with session_factory() as db:
        attempt = await db.scalar(select(Attempt))
        submission = await db.scalar(select(Submission))
        snapshot = await db.scalar(select(Snapshot))
        decision = await db.scalar(select(ReviewDecision))
        files = list((await db.scalars(select(WorkspaceFile))).all())
        submission_count = await db.scalar(select(func.count(Submission.id)))
        decision_count = await db.scalar(select(func.count(ReviewDecision.id)))

    assert attempt is not None and attempt.state == AttemptState.SUBMITTED.value
    assert attempt.submission_source == "MOODLE_IMPORT"
    assert submission is not None and submission.source == "MOODLE_IMPORT"
    assert submission.external_receipt["has_edit_history"] is False
    assert snapshot is not None and snapshot.reason == "LMS_IMPORT"
    assert [(item.path, item.content) for item in files] == [
        ("main.cpp", "int main() { return 0; }")
    ]
    assert decision is not None
    assert decision.status == "APPLIED"
    assert decision.lms_export_state == "IMPORTED"
    assert decision.grade == Decimal("8.50")
    assert decision.comment == "Checked in Moodle"
    assert decision.criterion_scores["remote_grade"] == "8.5"
    assert decision.criterion_scores["remote_grade_max"] == "10"
    assert decision.criterion_scores["local_grade"] == "8.50"
    assert decision.criterion_scores["local_grade_max"] == "10.00"
    assert decision.criterion_scores["normalization"] == "PROPORTIONAL_TO_REMOTE_MAX"
    assert decision.criterion_scores["responses"][0]["response_id"] == "slot-1"
    assert submission_count == 1
    assert decision_count == 1


async def test_legacy_source_snapshot_is_rematerialized_once_without_remote_revision_change(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _finished_item()
    formatted = "int main() {\n    return 0;\n}\n"
    item["responses"][0]["answer_text"] = "int main(){return 0;}"
    item["responses"][0]["artifacts"] = [_encoded_artifact("solution.cpp", formatted.encode())]

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        first = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == "moodle_historical_submission"
            )
        )
        source = await db.scalar(select(WorkspaceFile))
        assert mapping is not None and source is not None
        legacy_metadata = dict(mapping.metadata_json or {})
        legacy_metadata.pop("historical_source_materialization_version", None)
        mapping.metadata_json = legacy_metadata
        db.add(
            WorkspaceFile(
                workspace_id=source.workspace_id,
                path="main.cpp",
                language="CPP",
                content="int main(){return 0;}\n",
                content_hash=hashlib.sha256(b"int main(){return 0;}\n").hexdigest(),
                created_revision=0,
            )
        )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        refreshed = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        unchanged = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == "moodle_historical_submission"
            )
        )
        sources = list((await db.scalars(select(WorkspaceFile))).all())
        snapshots = await db.scalar(select(func.count(Snapshot.id)))

    assert first.created == 1
    assert refreshed.updated == 1
    assert unchanged.unchanged == 1
    assert snapshots == 2
    assert {(source.path, source.content) for source in sources} == {
        ("solution.cpp", formatted),
        ("moodle-online-text.cpp", "int main(){return 0;}"),
    }
    assert mapping is not None
    assert mapping.metadata_json["historical_source_materialization_version"] == 5


async def test_imported_comment_signature_becomes_separate_reviewer_identity(app_bundle) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _finished_item()
    item["responses"][0]["reviewer_name"] = "Герасименко Т."

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        created = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        decision = await db.scalar(select(ReviewDecision))
        assert decision is not None
        reviewer = await db.get(ExternalPrincipal, decision.reviewer_id)

    assert created.created == 1
    assert reviewer is not None
    assert reviewer.display_name == "Герасименко Т."
    assert reviewer.active is False
    assert reviewer.preferences["source"] == "MOODLE_COMMENT_SIGNATURE"
    assert decision.comment == "Checked in Moodle"
    assert decision.criterion_scores["responses"][0]["reviewer_name"] == "Герасименко Т."


async def test_multi_essay_quiz_creates_independent_tasks_submissions_and_grades(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _multi_essay_item()

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        created = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        # Reproduce an interrupted/pre-fix migration: split mappings are
        # current, while the denormalized receipts still name the parent Quiz
        # attempt.  An unchanged sync must repair every identity pair.
        current_mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.external_type == "moodle_historical_submission"
                    )
                )
            ).all()
        )
        for current_mapping in current_mappings:
            current_submission = await db.get(Submission, current_mapping.local_id)
            assert current_submission is not None
            current_submission.external_receipt = {
                **dict(current_submission.external_receipt or {}),
                "external_id": item["external_id"],
            }
        unchanged = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        submissions = list((await db.scalars(select(Submission))).all())
        attempts = {row.id: row for row in (await db.scalars(select(Attempt))).all()}
        assessments = {row.id: row for row in (await db.scalars(select(Assessment))).all()}
        decisions = list((await db.scalars(select(ReviewDecision))).all())
        mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.external_type == "moodle_historical_submission"
                    )
                )
            ).all()
        )
        question_mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.external_type == "moodle_quiz_essay_question"
                    )
                )
            ).all()
        )
        versions = {row.id: row for row in (await db.scalars(select(TaskVersion))).all()}
        workspace_files = list((await db.scalars(select(WorkspaceFile))).all())
        snapshots = {row.id: row for row in (await db.scalars(select(Snapshot))).all()}
        workspaces = {row.id: row for row in (await db.scalars(select(Workspace))).all()}
        grouped_submission = submissions[0]
        grouped_attempt = attempts[grouped_submission.attempt_id]
        grouped_assessment = assessments[grouped_attempt.assessment_id]
        review_group = await _submission_review_group(
            db,
            submission=grouped_submission,
            attempt=grouped_attempt,
            assessment=grouped_assessment,
        )

    assert created.created == 2
    assert unchanged.unchanged == 2
    assert len(submissions) == 2
    assert len(mappings) == 2
    assert len(question_mappings) == 2
    assert {mapping.metadata_json["response_id"] for mapping in question_mappings} == {
        "11",
        "12",
    }
    assert {
        assessments[attempts[submission.attempt_id].assessment_id].title
        for submission in submissions
    } == {
        "Historical Moodle assessment · задание 1",
        "Historical Moodle assessment · задание 2",
    }
    assert {
        versions[attempts[submission.attempt_id].assigned_task_version_id].statement
        for submission in submissions
    } == {"Implement Time comparison", "Implement Date comparison"}
    assert {decision.grade for decision in decisions if decision.status == "APPLIED"} == {
        Decimal("3.00"),
        Decimal("5.00"),
    }
    assert {decision.comment for decision in decisions if decision.status == "APPLIED"} == {
        "First question feedback",
        "Second question feedback",
    }
    assert {mapping.metadata_json["moodle_parent_external_id"] for mapping in mappings} == {
        item["external_id"]
    }
    assert {mapping.metadata_json["moodle_response_id"] for mapping in mappings} == {
        "11",
        "12",
    }
    assert {mapping.metadata_json["moodle_parent_attempt_id"] for mapping in mappings} == {
        item["attempt_id"]
    }
    mapping_by_submission = {mapping.local_id: mapping for mapping in mappings}
    assert all(
        submission.external_receipt["external_id"]
        == mapping_by_submission[submission.id].external_id
        for submission in submissions
    )
    # Each Essay remains an independent submission.  Within one response the
    # file plugin and online-text plugin are preserved independently.
    submission_by_response = {
        submission.external_receipt["moodle_response_id"]: submission for submission in submissions
    }
    first_snapshot = snapshots[submission_by_response["11"].snapshot_id]
    second_snapshot = snapshots[submission_by_response["12"].snapshot_id]
    assert {row["path"] for row in first_snapshot.files} == {
        "time.cpp",
        "moodle-online-text.cpp",
    }
    assert {row["path"] for row in second_snapshot.files} == {"main.cpp"}
    assert len(workspace_files) == 3
    assert review_group is not None
    assert review_group.title == "Historical Moodle assessment"
    assert [row.position for row in review_group.items] == [1, 2]
    assert {row.submission_id for row in review_group.items} == {
        submission.id for submission in submissions
    }
    assert [row.title for row in review_group.items] == [
        "Historical Moodle assessment · задание 1",
        "Historical Moodle assessment · задание 2",
    ]
    assert [row.status for row in review_group.items] == ["GRADED", "GRADED"]
    first_attempt = attempts[submission_by_response["11"].attempt_id]
    second_attempt = attempts[submission_by_response["12"].attempt_id]
    assert assessments[first_attempt.assessment_id].multi_file is True
    assert assessments[second_attempt.assessment_id].multi_file is False
    assert versions[first_attempt.assigned_task_version_id].multi_file is True
    assert versions[first_attempt.assigned_task_version_id].build_profile.endswith("-multi")
    assert versions[second_attempt.assigned_task_version_id].multi_file is False
    assert versions[second_attempt.assigned_task_version_id].build_profile.endswith("-single")
    assert workspaces[first_snapshot.workspace_id].multi_file is True
    assert workspaces[second_snapshot.workspace_id].multi_file is False


async def test_review_queue_paginates_multi_question_attempt_as_one_unit(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    grouped_item = _multi_essay_item()
    standalone_item = _finished_item(revision="standalone-r1")
    standalone_item.update(
        {
            "external_id": "quiz:777:attempt:999",
            "attempt_id": "attempt:999",
            # A different student keeps this as another queue unit. A second
            # attempt by the same student is intentionally collapsed to the
            # latest completed Moodle attempt.
            "user_id": "100",
            "display_name": "Another student",
            "submitted_at_epoch": grouped_item["submitted_at_epoch"] + 60,
        }
    )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        created = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[grouped_item, standalone_item],
        )
        submissions = list((await db.scalars(select(Submission))).all())
        pending_submission = next(
            submission
            for submission in submissions
            if dict(submission.external_receipt or {}).get("moodle_response_id") == "12"
            and dict(submission.external_receipt or {}).get("moodle_parent_attempt_id")
            == grouped_item["attempt_id"]
        )
        pending_submission_id = pending_submission.id
        await db.execute(
            delete(ReviewDecision).where(ReviewDecision.submission_id == pending_submission.id)
        )
        claimed_submission = next(
            submission
            for submission in submissions
            if dict(submission.external_receipt or {}).get("moodle_response_id") == "11"
            and dict(submission.external_receipt or {}).get("moodle_parent_attempt_id")
            == grouped_item["attempt_id"]
        )
        teacher = await db.get(ExternalPrincipal, ids["teacher_id"])
        assert teacher is not None
        other_teacher = ExternalPrincipal(
            connection_id=teacher.connection_id,
            external_subject="other-queue-reviewer",
            display_name="Other reviewer",
        )
        db.add(other_teacher)
        await db.flush()
        db.add(
            ReviewClaim(
                submission_id=claimed_submission.id,
                owner_id=other_teacher.id,
                lease_expires_at=datetime.now(UTC) + timedelta(minutes=5),
                heartbeat_at=datetime.now(UTC),
            )
        )

    auth = AuthContext(
        principal_id=ids["teacher_id"],
        display_name="Teacher",
        session_id=uuid.uuid4(),
        session_key="queue-grouping-test",
        roles=(CourseRole.TEACHER.value,),
        capabilities=("SYSTEM_SETTINGS",),
    )
    async with session_factory() as db:
        first_page = await list_submissions("all", auth, db, offset=0, limit=1)
        second_page = await list_submissions("all", auth, db, offset=1, limit=1)
        exhausted = await list_submissions("all", auth, db, offset=2, limit=1)

    assert created.created == 3
    assert len(first_page) == 1
    assert len(second_page) == 1
    assert exhausted == []
    units = first_page + second_page
    grouped = next(item for item in units if item.review_group is not None)
    standalone = next(item for item in units if item.review_group is None)
    assert grouped.assessment_title == "Historical Moodle assessment"
    assert grouped.id == pending_submission_id
    assert grouped.status == "UNGRADED"
    assert grouped.score == Decimal("3.00")
    assert grouped.max_score == Decimal("10.00")
    assert grouped.review_group is not None
    assert [item.position for item in grouped.review_group.items] == [1, 2]
    assert [item.status for item in grouped.review_group.items] == ["CLAIMED", "UNGRADED"]
    assert standalone.assessment_title.endswith("· задание 1")
    assert standalone.score == Decimal("8.50")


async def test_quiz_review_group_does_not_mix_repeated_attempts(app_bundle) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    first_item = _multi_essay_item(revision="attempt-one-r1")
    second_item = copy.deepcopy(first_item)
    second_item.update(
        {
            "external_id": "quiz:777:attempt:124",
            "external_revision": "attempt-two-r1",
            "attempt_id": "attempt:124",
            # Moodle report timestamps have second precision. The stable
            # remote attempt identity breaks a legitimate equality.
            "submitted_at_epoch": first_item["submitted_at_epoch"],
        }
    )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            # Import the newest Moodle attempt first. Local sequence therefore
            # makes the older attempt look newer and must never drive review
            # selection.
            items=[second_item, first_item],
        )

    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Submission, Attempt, Assessment)
                    .join(Attempt, Attempt.id == Submission.attempt_id)
                    .join(Assessment, Assessment.id == Attempt.assessment_id)
                )
            ).all()
        )
        first_submission, first_attempt, first_assessment = next(
            row
            for row in rows
            if row[0].external_receipt["moodle_parent_attempt_id"] == "attempt:123"
        )
        group = await _submission_review_group(
            db,
            submission=first_submission,
            attempt=first_attempt,
            assessment=first_assessment,
        )
        auth = AuthContext(
            principal_id=ids["teacher_id"],
            display_name="Teacher",
            session_id=uuid.uuid4(),
            session_key="repeated-attempt-selection",
            roles=(CourseRole.TEACHER.value,),
            capabilities=("SYSTEM_SETTINGS",),
        )
        queue = await list_submissions("all", auth, db, offset=0, limit=100)

    assert stats.created == 4
    assert len(rows) == 4
    assert len({attempt.id for _submission, attempt, _assessment in rows}) == 4
    assert group is not None
    assert len(group.items) == 2
    grouped_ids = {row.submission_id for row in group.items}
    assert {
        submission.external_receipt["moodle_parent_attempt_id"]
        for submission, _attempt, _assessment in rows
        if submission.id in grouped_ids
    } == {"attempt:123"}
    # Both completed Moodle attempts stay persisted as history, while the
    # active queue exposes only the latest remote completion. The selected
    # attempt was imported first and has the lower local sequence.
    assert len(queue) == 1
    assert queue[0].review_group is not None
    queued_ids = {item.submission_id for item in queue[0].review_group.items}
    assert {
        submission.external_receipt["moodle_parent_attempt_id"]
        for submission, _attempt, _assessment in rows
        if submission.id in queued_ids
    } == {"attempt:124"}
    assert {
        attempt.sequence for submission, attempt, _assessment in rows if submission.id in queued_ids
    } == {1}
    assert {
        attempt.sequence
        for submission, attempt, _assessment in rows
        if submission.external_receipt["moodle_parent_attempt_id"] == "attempt:123"
    } == {2}


async def test_historical_quiz_grade_targets_only_latest_completed_attempt(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    older = _finished_item(revision="older-r1")
    older.update(
        {
            "external_id": "quiz:777:123",
            "attempt_id": "123",
        }
    )
    newer = copy.deepcopy(older)
    newer.update(
        {
            "external_id": "quiz:777:124",
            "external_revision": "newer-r1",
            "attempt_id": "124",
            "submitted_at_epoch": older["submitted_at_epoch"] + 60,
        }
    )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[older],
        )

    async with session_factory() as db, db.begin():
        older_submission = await db.scalar(select(Submission))
        assert older_submission is not None
        # The review can be reserved while this is still the latest attempt.
        await claim_submission(
            db,
            submission_id=older_submission.id,
            teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[newer],
        )

    async with session_factory() as db, db.begin():
        submissions = list((await db.scalars(select(Submission))).all())
        by_remote_attempt = {
            row.external_receipt["moodle_parent_attempt_id"]: row for row in submissions
        }
        older_submission = by_remote_attempt["123"]
        newer_submission = by_remote_attempt["124"]

        # A stale deep-link cannot reserve an older attempt after a newer
        # completion appears.
        with pytest.raises(DomainError) as claim_error:
            await claim_submission(
                db,
                submission_id=older_submission.id,
                teacher_id=ids["teacher_id"],
                allow_system_settings_read=True,
            )
        assert claim_error.value.code == "MOODLE_ATTEMPT_SUPERSEDED"

        # The finalization path repeats the check because the claim above was
        # legitimately acquired before the new Moodle attempt was imported.
        with pytest.raises(DomainError) as error:
            await finalize_review(
                db,
                submission_id=older_submission.id,
                teacher_id=ids["teacher_id"],
                grade=Decimal("7"),
                comment="Must not grade an older Moodle attempt",
                allow_system_settings_read=True,
            )
        assert error.value.code == "MOODLE_ATTEMPT_SUPERSEDED"

        await claim_submission(
            db,
            submission_id=newer_submission.id,
            teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        provenance = copy.deepcopy(newer_submission.external_receipt)
        decision = await finalize_review(
            db,
            submission_id=newer_submission.id,
            teacher_id=ids["teacher_id"],
            grade=Decimal("9"),
            comment="Latest Moodle attempt",
            allow_system_settings_read=True,
        )

    assert decision.grade == Decimal("9")
    # Exact attempt/slot provenance must survive until the outbox worker
    # validates it against ExternalMapping and writes this decision to Moodle.
    assert newer_submission.external_receipt == provenance


async def test_newer_in_progress_quiz_attempt_suppresses_completed_review_until_finished(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    older = _finished_item(revision="older-completed-r1")
    newer_active = copy.deepcopy(older)
    newer_active.update(
        {
            "external_id": "quiz:777:attempt:124",
            "external_revision": "newer-active-r1",
            "attempt_id": "attempt:124",
            "state": "IN_PROGRESS",
            "submitted_at_epoch": older["submitted_at_epoch"] + 60,
        }
    )
    auth = AuthContext(
        principal_id=ids["teacher_id"],
        display_name="Teacher",
        session_id=uuid.uuid4(),
        session_key="active-retry-selection",
        roles=(CourseRole.TEACHER.value,),
        capabilities=("SYSTEM_SETTINGS",),
    )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[older],
        )
        older_submission = await db.scalar(select(Submission))
        assert older_submission is not None
        older_submission_id = older_submission.id
        await claim_submission(
            db,
            submission_id=older_submission_id,
            teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        skipped = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[newer_active],
        )
        # A later page (or a retry of an earlier cursor) cannot regress the
        # durable marker back to the older completed attempt.
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[older],
        )
        assert skipped.skipped == 1

    async with session_factory() as db, db.begin():
        queue = await list_submissions("all", auth, db, offset=0, limit=100)
        marker = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == MOODLE_ATTEMPT_OBSERVATION_EXTERNAL_TYPE
            )
        )
        assert marker is not None
        assert marker.metadata_json["remote_attempt_id"] == "attempt:124"
        assert marker.metadata_json["state"] == "IN_PROGRESS"
        assert marker.metadata_json["reviewable"] is False
        assert queue == []

        with pytest.raises(DomainError) as claim_error:
            await claim_submission(
                db,
                submission_id=older_submission_id,
                teacher_id=ids["teacher_id"],
                allow_system_settings_read=True,
            )
        assert claim_error.value.code == "MOODLE_ATTEMPT_SUPERSEDED"

        # The claim was legitimately acquired before Moodle exposed the retry;
        # finalization repeats the same current-attempt check.
        with pytest.raises(DomainError) as finalize_error:
            await finalize_review(
                db,
                submission_id=older_submission_id,
                teacher_id=ids["teacher_id"],
                grade=Decimal("7"),
                comment="Must not grade while a newer retry is active",
                allow_system_settings_read=True,
            )
        assert finalize_error.value.code == "MOODLE_ATTEMPT_SUPERSEDED"

    newer_finished = copy.deepcopy(newer_active)
    newer_finished.update(
        {
            "external_revision": "newer-finished-r2",
            "state": "GRADED",
            "grade": "9",
        }
    )
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        created = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[newer_finished],
        )
        assert created.created == 1

    async with session_factory() as db, db.begin():
        queue = await list_submissions("all", auth, db, offset=0, limit=100)
        assert len(queue) == 1
        queued_submission = await db.get(Submission, queue[0].id)
        assert queued_submission is not None
        assert queued_submission.external_receipt["moodle_parent_attempt_id"] == "attempt:124"
        await claim_submission(
            db,
            submission_id=queued_submission.id,
            teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )


async def test_pending_grade_export_is_blocked_when_newer_attempt_is_in_progress(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory)
    older = _finished_item(revision="pending-grade-r1")

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[older],
        )
        submission = await db.scalar(select(Submission))
        assert submission is not None
        await claim_submission(
            db,
            submission_id=submission.id,
            teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        await finalize_review(
            db,
            submission_id=submission.id,
            teacher_id=ids["teacher_id"],
            grade=Decimal("8"),
            comment="Decision made before the retry appeared",
            allow_system_settings_read=True,
        )

    newer_active = copy.deepcopy(older)
    newer_active.update(
        {
            "external_id": "quiz:777:attempt:124",
            "external_revision": "pending-grade-active-r2",
            "attempt_id": "attempt:124",
            "state": "IN_PROGRESS",
            "submitted_at_epoch": older["submitted_at_epoch"] + 60,
        }
    )
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[newer_active],
        )

    async with session_factory() as db:
        event = await db.scalar(
            select(SyncOutbox).where(SyncOutbox.event_type == "review.decision")
        )
        connection = await db.get(LMSConnection, ids["connection_id"])
        assert event is not None and connection is not None
        claimed = ClaimedOutboxEvent(
            id=event.id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            connection_id=event.connection_id,
            course_id=event.course_id,
            attempt_id=event.attempt_id,
            idempotency_key=event.idempotency_key,
            payload=event.payload,
            attempts=event.attempts,
            locked_at=event.created_at,
        )
        target = ConnectionTarget(
            id=connection.id,
            base_url=connection.base_url,
            service_token=None,
            mode="PLUGINLESS",
            transport="PLAYWRIGHT",
        )
        with pytest.raises(_BlockedDelivery) as blocked:
            await _prepare_grade(db, settings, claimed, target)
        assert blocked.value.code == "SUPERSEDED"


async def test_app_authored_moodle_retry_supersedes_older_local_submission(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory)
    now = datetime.now(UTC)

    async def create_local_attempt(
        db,
        *,
        assessment: Assessment,
        version: TaskVersion,
        student: ExternalPrincipal,
        sequence: int,
        remote_attempt_id: str,
        submitted: bool,
    ) -> tuple[Attempt, Submission | None]:
        attempt = Attempt(
            assessment_id=assessment.id,
            assigned_task_version_id=version.id,
            principal_id=student.id,
            sequence=sequence,
            state=(AttemptState.SUBMITTED.value if submitted else AttemptState.ACTIVE.value),
            started_at=now + timedelta(minutes=sequence),
            submitted_at=(now + timedelta(minutes=sequence) if submitted else None),
            integrity_policy={
                "moodle_course_id": "549",
                "moodle_cmid": 777,
                "moodle_attempt_id": remote_attempt_id,
                "moodle_question_slot": "1",
                "moodle_runtime_prepared": True,
            },
        )
        db.add(attempt)
        await db.flush()
        workspace = Workspace(attempt_id=attempt.id, current_revision=0)
        db.add(workspace)
        await db.flush()
        if not submitted:
            return attempt, None
        snapshot = Snapshot(
            workspace_id=workspace.id,
            revision=0,
            event_chain_head="",
            manifest_hash=hashlib.sha256(remote_attempt_id.encode()).hexdigest(),
            files=[],
            reason="SUBMISSION",
        )
        db.add(snapshot)
        await db.flush()
        submission = Submission(
            attempt_id=attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
            submitted_at=attempt.submitted_at,
        )
        db.add(submission)
        await db.flush()
        return attempt, submission

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        version = await db.scalar(
            select(TaskVersion)
            .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
            .where(AssessmentItem.assessment_id == ids["assessment_id"])
        )
        assert course is not None and assessment is not None and version is not None
        student = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject="99",
            display_name="Student",
        )
        db.add(student)
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=student.id,
                role=CourseRole.STUDENT.value,
            )
        )
        _, older_submission = await create_local_attempt(
            db,
            assessment=assessment,
            version=version,
            student=student,
            sequence=1,
            remote_attempt_id="141716",
            submitted=True,
        )
        assert older_submission is not None
        older_submission_id = older_submission.id
        student_id = student.id
        await claim_submission(
            db,
            submission_id=older_submission.id,
            teacher_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        await finalize_review(
            db,
            submission_id=older_submission.id,
            teacher_id=ids["teacher_id"],
            grade=Decimal("8"),
            comment="Decision before the student starts a retry",
            allow_system_settings_read=True,
        )

    async with session_factory() as db, db.begin():
        assessment = await db.get(Assessment, ids["assessment_id"])
        student = await db.get(ExternalPrincipal, student_id)
        version = await db.scalar(
            select(TaskVersion)
            .join(AssessmentItem, AssessmentItem.task_version_id == TaskVersion.id)
            .where(AssessmentItem.assessment_id == ids["assessment_id"])
        )
        assert assessment is not None and student is not None and version is not None
        newer_attempt, _ = await create_local_attempt(
            db,
            assessment=assessment,
            version=version,
            student=student,
            sequence=2,
            remote_attempt_id="141717",
            submitted=False,
        )
        newer_attempt_id = newer_attempt.id

    auth = AuthContext(
        principal_id=ids["teacher_id"],
        display_name="Teacher",
        session_id=uuid.uuid4(),
        session_key="app-native-retry-selection",
        roles=(CourseRole.TEACHER.value,),
        capabilities=("SYSTEM_SETTINGS",),
    )
    async with session_factory() as db:
        assert await list_submissions("all", auth, db, offset=0, limit=100) == []
        with pytest.raises(DomainError) as claim_error:
            await claim_submission(
                db,
                submission_id=older_submission_id,
                teacher_id=ids["teacher_id"],
                allow_system_settings_read=True,
            )
        assert claim_error.value.code == "MOODLE_ATTEMPT_SUPERSEDED"

        event = await db.scalar(
            select(SyncOutbox).where(SyncOutbox.event_type == "review.decision")
        )
        connection = await db.get(LMSConnection, ids["connection_id"])
        assert event is not None and connection is not None
        claimed = ClaimedOutboxEvent(
            id=event.id,
            event_type=event.event_type,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            connection_id=event.connection_id,
            course_id=event.course_id,
            attempt_id=event.attempt_id,
            idempotency_key=event.idempotency_key,
            payload=event.payload,
            attempts=event.attempts,
            locked_at=event.created_at,
        )
        target = ConnectionTarget(
            id=connection.id,
            base_url=connection.base_url,
            service_token=None,
            mode="PLUGINLESS",
            transport="PLAYWRIGHT",
        )
        with pytest.raises(_BlockedDelivery) as blocked:
            await _prepare_grade(db, settings, claimed, target)
        assert blocked.value.code == "SUPERSEDED"

    async with session_factory() as db, db.begin():
        newer_attempt = await db.get(Attempt, newer_attempt_id)
        workspace = await db.scalar(
            select(Workspace).where(Workspace.attempt_id == newer_attempt_id)
        )
        assert newer_attempt is not None and workspace is not None
        newer_attempt.state = AttemptState.SUBMITTED.value
        newer_attempt.submitted_at = now + timedelta(minutes=3)
        snapshot = Snapshot(
            workspace_id=workspace.id,
            revision=0,
            event_chain_head="",
            manifest_hash=hashlib.sha256(b"141717").hexdigest(),
            files=[],
            reason="SUBMISSION",
        )
        db.add(snapshot)
        await db.flush()
        newer_submission = Submission(
            attempt_id=newer_attempt.id,
            snapshot_id=snapshot.id,
            revision=1,
            source="MANUAL",
            submitted_at=newer_attempt.submitted_at,
        )
        db.add(newer_submission)
        await db.flush()
        newer_submission_id = newer_submission.id

    async with session_factory() as db:
        queue = await list_submissions("all", auth, db, offset=0, limit=100)
        assert [row.id for row in queue] == [newer_submission_id]

    imported_copy = _finished_item(revision="app-native-reverse-sync-r1")
    imported_copy.update(
        {
            "external_id": "quiz:777:141717",
            "attempt_id": "141717",
            "user_id": "99",
            "state": "GRADED",
            "submitted_at_epoch": int((now + timedelta(minutes=3)).timestamp()),
            "grade": "9",
        }
    )
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        imported = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[imported_copy],
        )
        assert imported.created == 1

    async with session_factory() as db:
        queue = await list_submissions("all", auth, db, offset=0, limit=100)
        all_submissions = list((await db.scalars(select(Submission))).all())
        assert len(queue) == 1
        canonical = await db.get(Submission, queue[0].id)
        assert canonical is not None and canonical.source == "MOODLE_IMPORT"
        assert len(all_submissions) == 3
        assert await db.get(Submission, newer_submission_id) is not None
        with pytest.raises(DomainError) as duplicate_claim:
            await claim_submission(
                db,
                submission_id=newer_submission_id,
                teacher_id=ids["teacher_id"],
                allow_system_settings_read=True,
            )
        assert duplicate_claim.value.code == "MOODLE_ATTEMPT_SUPERSEDED"


async def test_quiz_response_cleanup_requires_complete_crawl_and_restores_reappearing_item(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    initial = _multi_essay_item(revision="complete-r1")
    initial["responses_complete"] = True

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[initial],
        )

    # A failed or bounded question-page crawl may expose only part of the Quiz.
    # Absence in that response is not evidence that another question was removed.
    partial = copy.deepcopy(initial)
    partial["external_revision"] = "partial-r2"
    partial["responses"] = partial["responses"][:1]
    partial["responses_complete"] = False
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[partial],
        )

    async with session_factory() as db:
        partial_rows = list(
            (
                await db.execute(
                    select(Submission, Attempt).join(Attempt, Attempt.id == Submission.attempt_id)
                )
            ).all()
        )
    assert {
        row.external_receipt["moodle_response_id"]: attempt.state for row, attempt in partial_rows
    } == {"11": AttemptState.SUBMITTED.value, "12": AttemptState.SUBMITTED.value}

    # The same one-question projection is authoritative only when the connector
    # confirms that every review page was traversed successfully.
    complete_removal = copy.deepcopy(partial)
    complete_removal["external_revision"] = "complete-r3"
    complete_removal["responses_complete"] = True
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[complete_removal],
        )

    async with session_factory() as db:
        retired_rows = list(
            (
                await db.execute(
                    select(Submission, Attempt).join(Attempt, Attempt.id == Submission.attempt_id)
                )
            ).all()
        )
    retired = {
        row.external_receipt["moodle_response_id"]: (row, attempt) for row, attempt in retired_rows
    }
    assert retired["11"][1].state == AttemptState.SUBMITTED.value
    assert retired["12"][1].state == AttemptState.VOID.value
    assert retired["12"][0].external_receipt["retired_by_authoritative_quiz_refresh"] is True

    # Moodle can expose the same stable slot again after a quiz edit/regrade.
    # Its audit rows are reused and made visible instead of being duplicated.
    reappeared = copy.deepcopy(initial)
    reappeared["external_revision"] = "complete-r4"
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[reappeared],
        )

    async with session_factory() as db:
        restored_rows = list(
            (
                await db.execute(
                    select(Submission, Attempt).join(Attempt, Attempt.id == Submission.attempt_id)
                )
            ).all()
        )
    assert len(restored_rows) == 2
    assert all(attempt.state == AttemptState.SUBMITTED.value for _, attempt in restored_rows)
    assert all(
        row.external_receipt.get("retired_by_authoritative_quiz_refresh") is not True
        for row, _attempt in restored_rows
    )


async def test_quiz_review_group_ignores_void_question_children(app_bundle) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _multi_essay_item()
    third = copy.deepcopy(item["responses"][1])
    third.update(
        {
            "response_id": "13",
            "question_text": "Implement Duration comparison",
            "answer_text": "int third_inline = 3;",
            "comment": "Third question feedback",
        }
    )
    item["responses"].append(third)
    item["responses_complete"] = True

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        rows = list(
            (
                await db.execute(
                    select(Submission, Attempt, Assessment)
                    .join(Attempt, Attempt.id == Submission.attempt_id)
                    .join(Assessment, Assessment.id == Attempt.assessment_id)
                )
            ).all()
        )
        by_response = {
            submission.external_receipt["moodle_response_id"]: (
                submission,
                attempt,
                child,
            )
            for submission, attempt, child in rows
        }
        by_response["12"][1].state = AttemptState.VOID.value
        await db.flush()
        submission, attempt, child = by_response["11"]
        group = await _submission_review_group(
            db,
            submission=submission,
            attempt=attempt,
            assessment=child,
        )

    assert group is not None
    assert [row.position for row in group.items] == [1, 3]
    assert by_response["12"][0].id not in {row.submission_id for row in group.items}


async def test_multi_essay_import_migrates_legacy_combined_submission_without_duplicate(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _multi_essay_item()

    # Simulate data produced by the pre-split importer: both Essay answers are
    # files in one Submission bound to the parent Quiz assessment.
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        legacy = await _materialize_flat_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        migrated = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        submissions = list((await db.scalars(select(Submission))).all())
        attempts = list((await db.scalars(select(Attempt))).all())
        mappings = list(
            (
                await db.scalars(
                    select(ExternalMapping).where(
                        ExternalMapping.external_type == "moodle_historical_submission"
                    )
                )
            ).all()
        )
        snapshots = {row.id: row for row in (await db.scalars(select(Snapshot))).all()}
        decisions = list((await db.scalars(select(ReviewDecision))).all())

    assert legacy.created == 1
    assert migrated.updated == 1
    assert migrated.created == 1
    assert len(submissions) == 2
    assert len(attempts) == 2
    assert all(attempt.assessment_id != ids["assessment_id"] for attempt in attempts)
    assert len(mappings) == 2
    assert all(mapping.external_id != item["external_id"] for mapping in mappings)
    assert {mapping.metadata_json["moodle_response_id"] for mapping in mappings} == {
        "11",
        "12",
    }
    mapping_by_submission = {mapping.local_id: mapping for mapping in mappings}
    assert all(
        submission.external_receipt["external_id"]
        == mapping_by_submission[submission.id].external_id
        for submission in submissions
    )
    assert {
        frozenset(raw["path"] for raw in snapshots[submission.snapshot_id].files)
        for submission in submissions
    } == {
        frozenset({"main.cpp"}),
        frozenset({"time.cpp", "moodle-online-text.cpp"}),
    }
    assert len([row for row in decisions if row.status == "APPLIED"]) == 2


async def test_multi_essay_import_retires_preexisting_aggregate_if_split_rows_exist(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = _multi_essay_item()

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        # Simulate a partially migrated database in which the old aggregate was
        # recreated alongside already existing split rows.
        await _materialize_flat_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )
        refreshed = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        submissions = list((await db.scalars(select(Submission))).all())
        attempts = list((await db.scalars(select(Attempt))).all())
        visible = await visible_submission_ids_for_review(
            db,
            principal_id=ids["teacher_id"],
            allow_system_settings_read=True,
        )
        legacy_mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == "moodle_historical_submission",
                ExternalMapping.external_id == item["external_id"],
            )
        )

    assert refreshed.unchanged == 2
    assert len(submissions) == 3  # retired row is retained for provenance/audit
    assert len(visible) == 2
    assert len([attempt for attempt in attempts if attempt.state == AttemptState.VOID.value]) == 1
    assert legacy_mapping is not None
    assert legacy_mapping.metadata_json["superseded_by_quiz_split"] is True


async def test_multi_essay_refresh_retires_old_position_based_question_identities(
    app_bundle,
) -> None:
    """A connector upgrade must not leave two visible ``задание 1`` rows."""

    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    old = _multi_essay_item(revision="old-position-identities")
    old["responses_complete"] = True
    old["responses"] = [
        {
            **old["responses"][0],
            "response_id": "position-1",
            "grade": None,
            "grade_max": "3",
            "comment": "",
            "artifacts": [],
            "answer_text": "",
            "answer_complete": False,
        },
        {
            **old["responses"][1],
            "response_id": "position-2",
        },
    ]
    current = _multi_essay_item(revision="stable-slot-identities")
    current["responses_complete"] = True

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[old],
        )
        refreshed = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[current],
        )

    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Submission, Attempt, Assessment)
                    .join(Attempt, Attempt.id == Submission.attempt_id)
                    .join(Assessment, Assessment.id == Attempt.assessment_id)
                )
            ).all()
        )
        active = [row for row in rows if row[1].state != AttemptState.VOID.value]
        current_first = next(
            row for row in active if row[0].external_receipt["moodle_response_id"] == "11"
        )
        group = await _submission_review_group(
            db,
            submission=current_first[0],
            attempt=current_first[1],
            assessment=current_first[2],
        )

    assert refreshed.created == 2
    assert len(rows) == 4  # old rows remain immutable audit records
    assert len(active) == 2
    assert {row[0].external_receipt["moodle_response_id"] for row in active} == {"11", "12"}
    assert group is not None
    assert [item.position for item in group.items] == [1, 2]


async def test_multi_essay_incomplete_refresh_hides_duplicate_visual_positions(
    app_bundle,
) -> None:
    """A non-authoritative refresh may retain audit rows but never duplicate tabs."""

    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    old = _multi_essay_item(revision="incomplete-old-position-identities")
    old["responses_complete"] = False
    old["responses"] = [
        {**old["responses"][0], "response_id": "position-1"},
        {**old["responses"][1], "response_id": "position-2"},
    ]
    current = _multi_essay_item(revision="incomplete-stable-slot-identities")
    current["responses_complete"] = False

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[old],
        )
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[current],
        )

    async with session_factory() as db:
        rows = list(
            (
                await db.execute(
                    select(Submission, Attempt, Assessment)
                    .join(Attempt, Attempt.id == Submission.attempt_id)
                    .join(Assessment, Assessment.id == Attempt.assessment_id)
                )
            ).all()
        )
        active = [row for row in rows if row[1].state != AttemptState.VOID.value]
        canonical_first = next(
            row for row in active if row[0].external_receipt["moodle_response_id"] == "11"
        )
        group = await _submission_review_group(
            db,
            submission=canonical_first[0],
            attempt=canonical_first[1],
            assessment=canonical_first[2],
        )

    # Incomplete Moodle pagination deliberately prevents destructive cleanup.
    assert len(active) == 4
    assert group is not None
    assert [item.position for item in group.items] == [1, 2]
    assert {item.submission_id for item in group.items} == {
        row[0].id for row in active if row[0].external_receipt["moodle_response_id"] in {"11", "12"}
    }


async def test_multi_essay_revision_updates_only_changed_question_submission(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    initial = _multi_essay_item()
    changed = _multi_essay_item(revision="multi-attempt-r2")
    changed["responses"][1] = {
        **changed["responses"][1],
        "answer_text": "int second_inline = 22;",
        "grade": "6",
        "comment": "Second answer regraded",
        "artifacts": [_encoded_artifact("date.cpp", b"int date_file = 22;\n")],
    }

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[initial],
        )
        refreshed = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[changed],
        )

    async with session_factory() as db:
        submissions = list((await db.scalars(select(Submission))).all())
        decisions = list(
            (await db.scalars(select(ReviewDecision).order_by(ReviewDecision.created_at))).all()
        )
        attempts = {row.id: row for row in (await db.scalars(select(Attempt))).all()}
        assessments = {row.id: row for row in (await db.scalars(select(Assessment))).all()}
        versions = {row.id: row for row in (await db.scalars(select(TaskVersion))).all()}
        snapshots = {row.id: row for row in (await db.scalars(select(Snapshot))).all()}
        workspaces = {row.id: row for row in (await db.scalars(select(Workspace))).all()}

    assert refreshed.unchanged == 1
    assert refreshed.updated == 1
    assert len(submissions) == 2
    assert len([row for row in decisions if row.status == "APPLIED"]) == 2
    assert any(
        row.status == "APPLIED"
        and row.grade == Decimal("6.00")
        and row.comment == "Second answer regraded"
        for row in decisions
    )
    changed_submission = next(
        row for row in submissions if row.external_receipt["moodle_response_id"] == "12"
    )
    changed_attempt = attempts[changed_submission.attempt_id]
    changed_snapshot = snapshots[changed_submission.snapshot_id]
    assert assessments[changed_attempt.assessment_id].multi_file is True
    assert versions[changed_attempt.assigned_task_version_id].multi_file is True
    assert versions[changed_attempt.assigned_task_version_id].number == 2
    assert workspaces[changed_snapshot.workspace_id].multi_file is True
    assert {row["path"] for row in changed_snapshot.files} == {
        "date.cpp",
        "moodle-online-text.cpp",
    }


async def test_materialize_normalizes_moodle_grade_without_mutating_published_task(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = {
        **_finished_item(),
        "grade": "85",
        "grade_max": "100",
    }

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        decision = await db.scalar(select(ReviewDecision))
        assessment = await db.get(Assessment, ids["assessment_id"])
        version = await db.scalar(select(TaskVersion))

    assert stats.created == 1
    assert decision is not None and decision.grade == Decimal("8.50")
    assert decision.criterion_scores["remote_grade"] == "85"
    assert decision.criterion_scores["remote_grade_max"] == "100"
    assert decision.criterion_scores["local_grade"] == "8.50"
    assert decision.criterion_scores["local_grade_max"] == "10.00"
    assert decision.criterion_scores["normalization"] == "PROPORTIONAL_TO_REMOTE_MAX"
    assert assessment is not None
    assert assessment.status == "PUBLISHED"
    assert assessment.max_score == Decimal("10.00")
    assert version is not None
    assert version.status == "PUBLISHED"
    assert version.max_score == Decimal("10.00")


async def test_unchanged_history_revision_repairs_a_missing_imported_grade(app_bundle) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    initially_ungraded = {
        **_finished_item(),
        "grade": None,
        "grade_max": "100",
    }
    now_graded = {
        **initially_ungraded,
        "grade": "85",
    }

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        created = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[initially_ungraded],
        )
        repaired = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[now_graded],
        )

    async with session_factory() as db:
        decisions = list((await db.scalars(select(ReviewDecision))).all())
        submissions = await db.scalar(select(func.count(Submission.id)))

    assert created.created == 1
    assert repaired.unchanged == 1
    assert submissions == 1
    assert len(decisions) == 1
    assert decisions[0].status == "APPLIED"
    assert decisions[0].grade == Decimal("8.50")


async def test_ungraded_refresh_supersedes_an_erroneous_imported_grade(app_bundle) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    incorrectly_graded = {
        **_finished_item(),
        "grade": "3",
        "grade_max": "3",
        "comment": "",
    }
    actually_ungraded = {
        **incorrectly_graded,
        # Keep the connector revision unchanged to exercise the repair path
        # used by already-imported production rows.
        "grade": None,
        "state": "SUBMITTED",
        "responses": [
            {
                **incorrectly_graded["responses"][0],
                "grade": None,
                "grade_max": "3",
                "comment": "",
            }
        ],
    }

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[incorrectly_graded],
        )
        repaired = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[actually_ungraded],
        )

    async with session_factory() as db:
        decisions = list((await db.scalars(select(ReviewDecision))).all())
        applied = list(
            (
                await db.scalars(
                    select(ReviewDecision).where(ReviewDecision.status == "APPLIED")
                )
            ).all()
        )

    assert repaired.unchanged == 1
    assert len(decisions) == 1
    assert decisions[0].lms_export_state == "IMPORTED"
    assert decisions[0].status == "SUPERSEDED"
    assert applied == []


async def test_materialize_skips_in_progress_attempt(app_bundle) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = {**_finished_item(), "state": "IN_PROGRESS"}

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        count = await db.scalar(select(func.count(Submission.id)))
    assert stats.skipped == 1
    assert count == 0


async def test_teacher_test_attempt_does_not_create_an_ambiguous_student_role(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    item = {
        **_finished_item(),
        "external_id": "quiz:777:attempt:teacher-test",
        "user_id": "42",
        "display_name": "Teacher",
    }

    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        stats = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[item],
        )

    async with session_factory() as db:
        roles = set(
            (
                await db.scalars(
                    select(CourseMembership.role).where(
                        CourseMembership.course_id == ids["course_id"],
                        CourseMembership.principal_id == ids["teacher_id"],
                    )
                )
            ).all()
        )
        attempt = await db.scalar(select(Attempt))

    assert stats.created == 1
    assert roles == {CourseRole.TEACHER.value}
    assert attempt is not None and attempt.principal_id == ids["teacher_id"]


async def test_enqueue_history_import_is_per_assessment_and_idempotent(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        teacher = await db.get(ExternalPrincipal, ids["teacher_id"])
        assert course is not None and teacher is not None
        second = Assessment(
            course_id=course.id,
            title="Assignment history",
            created_by_id=teacher.id,
        )
        db.add(second)
        await db.flush()
        db.add(
            ExternalMapping(
                connection_id=course.connection_id,
                local_type="core.assessment",
                local_id=second.id,
                external_type="mod_assign",
                external_id="778",
                metadata_json={"module": "assign", "cmid": 778},
            )
        )
        await db.flush()
        first_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="42"
        )
        second_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="42"
        )
        first_chain = list(
            (
                await db.scalars(
                    select(SyncOutbox).where(SyncOutbox.event_type == "moodle.history.import")
                )
            ).all()
        )
        for event in first_chain:
            event.state = SyncOutboxState.DELIVERED.value
        await db.flush()
        refresh_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="42"
        )

    async with session_factory() as db:
        events = list(
            (
                await db.scalars(
                    select(SyncOutbox)
                    .where(SyncOutbox.event_type == "moodle.history.import")
                    .order_by(SyncOutbox.aggregate_id)
                )
            ).all()
        )
    assert first_count == 2
    assert second_count == 0
    assert refresh_count == 2
    assert len(events) == 4
    assert {event.aggregate_id for event in events} == {
        ids["assessment_id"],
        second.id,
    }
    assert {event.payload["module"] for event in events} == {"quiz", "assign"}
    assert sum(event.state == SyncOutboxState.PENDING.value for event in events) == 2
    assert sum(event.state == SyncOutboxState.DELIVERED.value for event in events) == 2


async def test_history_outbox_page_materializes_and_queues_next_cursor(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assert course is not None
        assert (
            await enqueue_historical_submission_imports(
                db, course=course, actor_external_subject="42"
            )
            == 1
        )
        initial = await db.scalar(
            select(SyncOutbox).where(SyncOutbox.event_type == "moodle.history.import")
        )
        assert initial is not None
        initial_id = initial.id
        initial_limit = initial.payload["limit"]

    captured: dict[str, Any] = {}

    class FakeBridge:
        async def discover_historical_submissions(
            self,
            payload: dict[str, Any],
        ) -> _BrowserDeliveryResult:
            captured.update(payload)
            return _BrowserDeliveryResult(
                value={
                    "course_id": "549",
                    "activity": {"module": "quiz", "cmid": 777},
                    "items": [_finished_item()],
                    "next_cursor": "1:0",
                    "complete": False,
                    "warnings": [],
                },
                storage_state=_browser_state("after"),
            )

    def bridge_factory(_settings, target, _client):
        assert target.transport == "PLAYWRIGHT"
        assert target.principal_id == ids["teacher_id"]
        assert target.browser_state == _browser_state("before")
        return FakeBridge()

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=bridge_factory,
            client=client,
        )

    async with session_factory() as db:
        events = list(
            (
                await db.scalars(
                    select(SyncOutbox)
                    .where(SyncOutbox.event_type == "moodle.history.import")
                    .order_by(SyncOutbox.created_at, SyncOutbox.id)
                )
            ).all()
        )
        submissions = await db.scalar(select(func.count(Submission.id)))
        decisions = await db.scalar(select(func.count(ReviewDecision.id)))
        credential = await db.scalar(
            select(MoodleCredential).where(MoodleCredential.principal_id == ids["teacher_id"])
        )

    assert captured == {
        "course_id": "549",
        "actor_external_subject": "42",
        "module": "quiz",
        "cmid": 777,
        "cursor": "0:0",
        "limit": initial_limit,
        "priority_only": True,
    }
    assert len(events) == 2
    delivered = next(event for event in events if event.id == initial_id)
    next_page = next(event for event in events if event.id != initial_id)
    assert delivered.state == SyncOutboxState.DELIVERED.value
    assert delivered.receipt == {
        "status": "DELIVERED",
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "warning_count": 0,
        "complete": False,
        "next_page_queued": True,
        "full_scan_queued": False,
        "priority_only": True,
        "actor_external_subject": "42",
    }
    assert next_page.state == SyncOutboxState.PENDING.value
    assert next_page.payload["cursor"] == "1:0"
    assert next_page.payload["actor_external_subject"] == "42"
    assert next_page.payload["priority_only"] is True
    assert submissions == 1
    assert decisions == 1
    assert credential is not None and credential.revision == 2
    assert credential.lease_owner is None and credential.lease_expires_at is None


async def test_empty_history_import_completes_without_an_error_or_submission(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assert course is not None
        await enqueue_historical_submission_imports(db, course=course, actor_external_subject="42")
        event = await db.scalar(
            select(SyncOutbox).where(SyncOutbox.event_type == "moodle.history.import")
        )
        assert event is not None
        event.payload = {**event.payload, "priority_only": False}
        event_id = event.id

    class EmptyBridge:
        async def discover_historical_submissions(
            self, payload: dict[str, Any],
        ) -> _BrowserDeliveryResult:
            return _BrowserDeliveryResult(
                value={
                    "course_id": "549",
                    "activity": {"module": "quiz", "cmid": 777},
                    "items": [],
                    "next_cursor": None,
                    "complete": True,
                    "warnings": [],
                },
                storage_state=_browser_state("after-empty"),
            )

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory, settings, bridge_factory=lambda *_args: EmptyBridge(), client=client,
        )

    async with session_factory() as db:
        event = await db.get(SyncOutbox, event_id)
        assert event is not None and event.state == SyncOutboxState.DELIVERED.value
        assert event.receipt["complete"] is True
        assert event.receipt["created"] == 0
        assert event.receipt["warning_count"] == 0
        assert event.receipt["next_page_queued"] is False
        assert event.receipt["full_scan_queued"] is False
        assert await db.scalar(select(func.count(Submission.id))) == 0
        assert await db.scalar(select(func.count(SyncOutbox.id))) == 1


async def test_completed_priority_history_scan_queues_exhaustive_scan(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assert course is not None
        assert (
            await enqueue_historical_submission_imports(
                db, course=course, actor_external_subject="42"
            )
            == 1
        )
        initial = await db.scalar(
            select(SyncOutbox).where(SyncOutbox.event_type == "moodle.history.import")
        )
        assert initial is not None and initial.payload["priority_only"] is True
        initial_id = initial.id

    class FakeBridge:
        async def discover_historical_submissions(
            self,
            payload: dict[str, Any],
        ) -> _BrowserDeliveryResult:
            assert payload["priority_only"] is True
            return _BrowserDeliveryResult(
                value={
                    "course_id": "549",
                    "activity": {"module": "quiz", "cmid": 777},
                    "items": [],
                    "next_cursor": None,
                    "complete": True,
                    "warnings": [],
                },
                storage_state=_browser_state("after-priority"),
            )

    async with httpx.AsyncClient() as client:
        assert await process_outbox_once(
            session_factory,
            settings,
            bridge_factory=lambda *_args: FakeBridge(),
            client=client,
        )

    async with session_factory() as db:
        events = list(
            (
                await db.scalars(
                    select(SyncOutbox)
                    .where(SyncOutbox.event_type == "moodle.history.import")
                    .order_by(SyncOutbox.created_at, SyncOutbox.id)
                )
            ).all()
        )

    assert len(events) == 2
    delivered = next(event for event in events if event.id == initial_id)
    exhaustive = next(event for event in events if event.id != initial_id)
    assert delivered.receipt["full_scan_queued"] is True
    assert delivered.receipt["priority_only"] is True
    assert exhaustive.state == SyncOutboxState.PENDING.value
    assert exhaustive.payload["cursor"] == "0:0"
    assert exhaustive.payload["priority_only"] is False


async def test_priority_history_scan_is_queued_while_legacy_full_scan_is_active(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assert course is not None
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                event_type="moodle.history.import",
                aggregate_type="Assessment",
                aggregate_id=ids["assessment_id"],
                idempotency_key="legacy-full-history-active",
                payload={
                    "course_id": "549",
                    "actor_external_subject": "42",
                    "module": "quiz",
                    "cmid": 777,
                    "cursor": "2:10",
                    "limit": 5,
                },
            )
        )
        await db.flush()
        queued = await enqueue_historical_submission_imports(
            db,
            course=course,
            actor_external_subject="42",
        )

    async with session_factory() as db:
        rows = list(
            (
                await db.scalars(
                    select(SyncOutbox).where(SyncOutbox.event_type == "moodle.history.import")
                )
            ).all()
        )

    assert queued == 1
    assert len(rows) == 2
    assert {(row.payload or {}).get("priority_only") is True for row in rows} == {False, True}


async def test_browser_session_contention_never_exhausts_history_import_retry_budget(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    settings.sync_max_attempts = 2
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        credential = await db.scalar(
            select(MoodleCredential).where(
                MoodleCredential.principal_id == ids["teacher_id"]
            )
        )
        assert course is not None and credential is not None
        assert (
            await enqueue_historical_submission_imports(
                db,
                course=course,
                actor_external_subject="42",
            )
            == 1
        )
        credential.lease_owner = "another-history-worker"
        credential.lease_expires_at = datetime.now(UTC) + timedelta(hours=1)

    # More collisions than the external-delivery retry budget previously made
    # the page terminally FAILED and severed the rest of its cursor chain.
    for _ in range(6):
        process_now = datetime.now(UTC)
        async with session_factory() as db, db.begin():
            event = await db.scalar(
                select(SyncOutbox).where(
                    SyncOutbox.event_type == "moodle.history.import"
                )
            )
            assert event is not None
            event.next_attempt_at = process_now - timedelta(seconds=1)
        assert await process_outbox_once(
            session_factory,
            settings,
            now=process_now,
        )

    async with session_factory() as db:
        event = await db.scalar(
            select(SyncOutbox).where(
                SyncOutbox.event_type == "moodle.history.import"
            )
        )

    assert event is not None
    assert event.state == SyncOutboxState.RETRY.value
    assert event.attempts == 0
    assert event.last_error.startswith("BROWSER_BUSY:")
    assert event.next_attempt_at == event.last_attempt_at + timedelta(seconds=5)


async def test_same_remote_attempt_merges_actor_provenance_without_granting_review_scope(
    app_bundle,
) -> None:
    _, session_factory, _ = app_bundle
    ids = await _seed_history_target(session_factory)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        first_teacher = await db.get(ExternalPrincipal, ids["teacher_id"])
        assert course is not None and first_teacher is not None
        second_teacher = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject="84",
            display_name="Second teacher",
        )
        db.add(second_teacher)
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=second_teacher.id,
                role=CourseRole.TEACHER.value,
            )
        )
        token = TeacherAccessToken(
            public_id=second_teacher.id.hex[:16],
            label="Second history teacher",
            secret_hash="$argon2id$test-fixture-not-used-for-login-2",
            created_by_id=first_teacher.id,
        )
        db.add(token)
        await db.flush()
        db.add(TeacherTokenGrant(token_id=token.id, principal_id=second_teacher.id))
        second_teacher_id = second_teacher.id

        assessment = await db.get(Assessment, ids["assessment_id"])
        assert assessment is not None
        first = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="42",
            items=[_finished_item()],
        )
        student_membership = await db.scalar(
            select(CourseMembership).where(
                CourseMembership.course_id == course.id,
                CourseMembership.role == CourseRole.STUDENT.value,
            )
        )
        assert student_membership is not None
        student_membership.active = False

    async with session_factory() as db, db.begin():
        first_visible = await visible_submission_ids_for_review(
            db,
            principal_id=ids["teacher_id"],
        )
        second_visible_before = await visible_submission_ids_for_review(
            db,
            principal_id=second_teacher_id,
        )
        course = await db.get(Course, ids["course_id"])
        assessment = await db.get(Assessment, ids["assessment_id"])
        assert course is not None and assessment is not None
        second = await materialize_historical_submissions(
            db,
            course=course,
            assessment=assessment,
            actor_external_subject="84",
            items=[_finished_item()],
        )

    async with session_factory() as db:
        submissions = list((await db.scalars(select(Submission))).all())
        mapping = await db.scalar(
            select(ExternalMapping).where(
                ExternalMapping.external_type == "moodle_historical_submission"
            )
        )
        second_visible_after = await visible_submission_ids_for_review(
            db,
            principal_id=second_teacher_id,
        )
        admin_visible = await visible_submission_ids_for_review(
            db,
            principal_id=second_teacher_id,
            allow_system_settings_read=True,
        )

    assert first.created == 1
    assert second.unchanged == 1
    assert len(submissions) == 1
    assert first_visible == set()
    assert second_visible_before == set()
    assert second_visible_after == set()
    assert admin_visible == {submissions[0].id}
    assert submissions[0].external_receipt["actor_external_subjects"] == ["42", "84"]
    assert mapping is not None
    assert mapping.metadata_json["actor_external_subjects"] == ["42", "84"]


async def test_enqueue_history_import_does_not_fan_out_to_other_active_teacher_sessions(
    app_bundle,
) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        first_teacher = await db.get(ExternalPrincipal, ids["teacher_id"])
        assert course is not None and first_teacher is not None
        second_teacher = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject="84",
            display_name="Second teacher",
        )
        third_teacher = ExternalPrincipal(
            connection_id=course.connection_id,
            external_subject="126",
            display_name="Teacher without Moodle session",
        )
        db.add_all([second_teacher, third_teacher])
        await db.flush()
        for teacher in (second_teacher, third_teacher):
            db.add(
                CourseMembership(
                    course_id=course.id,
                    principal_id=teacher.id,
                    role=CourseRole.TEACHER.value,
                )
            )
            token = TeacherAccessToken(
                public_id=teacher.id.hex[:16],
                label=f"History teacher {teacher.external_subject}",
                secret_hash=f"$argon2id$test-{teacher.external_subject}",
                created_by_id=first_teacher.id,
            )
            db.add(token)
            await db.flush()
            db.add(TeacherTokenGrant(token_id=token.id, principal_id=teacher.id))
        db.add(
            MoodleCredential(
                connection_id=course.connection_id,
                principal_id=second_teacher.id,
                kind=BROWSER_STATE_CREDENTIAL_KIND,
                encrypted_secret=encrypt_moodle_browser_state(
                    _browser_state("second"),
                    settings,
                    connection_id=course.connection_id,
                    principal_id=second_teacher.id,
                ),
                status="ACTIVE",
            )
        )
        await db.flush()
        first_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="42"
        )
        second_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="42"
        )
        other_actor_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="84"
        )
        missing_session_count = await enqueue_historical_submission_imports(
            db, course=course, actor_external_subject="126"
        )

    async with session_factory() as db:
        events = list(
            (
                await db.scalars(
                    select(SyncOutbox).where(SyncOutbox.event_type == "moodle.history.import")
                )
            ).all()
        )

    assert first_count == 1
    assert second_count == 0
    assert other_actor_count == 1
    assert missing_session_count == 0
    assert {event.payload["actor_external_subject"] for event in events} == {"42", "84"}


async def test_teacher_outbox_status_hides_other_history_actor_chain(app_bundle) -> None:
    _, session_factory, settings = app_bundle
    ids = await _seed_history_target(session_factory, settings=settings)
    async with session_factory() as db, db.begin():
        course = await db.get(Course, ids["course_id"])
        assert course is not None
        assert (
            await enqueue_historical_submission_imports(
                db, course=course, actor_external_subject="42"
            )
            == 1
        )
        db.add(
            SyncOutbox(
                connection_id=course.connection_id,
                course_id=course.id,
                event_type="moodle.history.import",
                aggregate_type="Assessment",
                aggregate_id=ids["assessment_id"],
                idempotency_key=f"other-history-actor:{course.id}",
                payload={
                    "course_id": course.external_id,
                    "actor_external_subject": "84",
                    "module": "quiz",
                    "cmid": 777,
                    "cursor": "0:0",
                    "limit": 5,
                },
            )
        )

    teacher_auth = AuthContext(
        principal_id=ids["teacher_id"],
        display_name="Teacher",
        session_id=uuid.uuid4(),
        session_key="teacher-session",
        roles=(CourseRole.TEACHER.value,),
        capabilities=(),
    )
    admin_auth = AuthContext(
        principal_id=ids["teacher_id"],
        display_name="Teacher",
        session_id=uuid.uuid4(),
        session_key="admin-session",
        roles=(CourseRole.TEACHER.value,),
        capabilities=("SYSTEM_SETTINGS",),
    )
    async with session_factory() as db:
        teacher_rows = await list_sync_outbox(
            auth=teacher_auth,
            db=db,
            course_id=None,
            state=None,
            limit=100,
            offset=0,
        )
        admin_rows = await list_sync_outbox(
            auth=admin_auth,
            db=db,
            course_id=None,
            state=None,
            limit=100,
            offset=0,
        )

    assert [row.payload["actor_external_subject"] for row in teacher_rows] == ["42"]
    assert {row.payload["actor_external_subject"] for row in admin_rows} == {"42", "84"}
