"""Regressions for ORM state loaded before a concurrent save/finalization.

Two committed sessions reproduce a stale identity map without relying on SQLite
to implement PostgreSQL row locks. The service must reload state at its lock
boundary, not merely run SELECT FOR UPDATE against an already loaded instance.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.attempts import Attempt, Snapshot, Workspace, WorkspaceFile
from app.services.common import DomainError
from app.services.workspace import replace_file_content, start_attempt, submit_attempt
from tests.test_attempt_review_services import _seed_course


async def _start(db):
    student, _, _, assessment = await _seed_course(db)
    assessment.attempt_limit = 2
    attempt = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    workspace = await db.scalar(select(Workspace).where(Workspace.attempt_id == attempt.id))
    files = list(
        (
            await db.scalars(
                select(WorkspaceFile)
                .where(
                    WorkspaceFile.workspace_id == workspace.id,
                )
                .order_by(WorkspaceFile.path)
            )
        ).all()
    )
    await db.commit()
    return student, assessment, attempt, workspace, files


async def test_stale_attempt_instance_cannot_edit_after_other_session_submits(db, app_bundle):
    student, _, attempt, _, files = await _start(db)
    async with app_bundle[1]() as other:
        await submit_attempt(
            other, attempt_id=attempt.id, principal_id=student.id, expected_revision=0
        )
        await other.commit()
    assert attempt.state == "ACTIVE"  # The original session has cached old state.
    with pytest.raises(DomainError, match="read-only"):
        await replace_file_content(
            db,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=files[0].id,
            content="This must never replace submitted code",
            expected_revision=0,
            client_request_id="stale-save-after-submit",
        )


async def test_stale_workspace_detects_other_session_save_before_submit(db, app_bundle):
    student, _, attempt, _, files = await _start(db)
    async with app_bundle[1]() as other:
        await replace_file_content(
            other,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=files[0].id,
            content="// fresh code from the other tab",
            expected_revision=0,
            client_request_id="other-tab-save",
        )
        await other.commit()
    with pytest.raises(DomainError) as error:
        await submit_attempt(
            db, attempt_id=attempt.id, principal_id=student.id, expected_revision=0
        )
    assert error.value.code == "REVISION_CONFLICT"
    assert error.value.details == {"current_revision": 1}


async def test_submission_snapshot_refreshes_preloaded_files(db, app_bundle):
    student, _, attempt, workspace, files = await _start(db)
    updated = "// this is the final code from another request"
    original = files[0].content
    async with app_bundle[1]() as other:
        await replace_file_content(
            other,
            attempt_id=attempt.id,
            principal_id=student.id,
            file_id=files[0].id,
            content=updated,
            expected_revision=0,
            client_request_id="newest-save",
        )
        await other.commit()
    # Simulate the API carrying the correct acknowledged revision while source
    # objects in a long-lived ORM transaction were loaded earlier.
    await db.refresh(workspace)
    await db.refresh(attempt)
    assert files[0].content == original
    submission = await submit_attempt(
        db, attempt_id=attempt.id, principal_id=student.id, expected_revision=1
    )
    snapshot = await db.get(Snapshot, submission.snapshot_id)
    assert snapshot.revision == 1
    assert (
        next(file["content"] for file in snapshot.files if file["id"] == str(files[0].id))
        == updated
    )


async def test_start_refreshes_cached_terminal_attempt(db, app_bundle):
    student, assessment, attempt, _, _ = await _start(db)
    async with app_bundle[1]() as other:
        await submit_attempt(
            other, attempt_id=attempt.id, principal_id=student.id, expected_revision=0
        )
        await other.commit()
    assert attempt.state == "ACTIVE"
    fresh = await start_attempt(db, assessment_id=assessment.id, principal_id=student.id)
    assert fresh.id != attempt.id
    assert fresh.sequence == 2
    assert (await db.get(Attempt, attempt.id)).state == "SUBMITTED"
