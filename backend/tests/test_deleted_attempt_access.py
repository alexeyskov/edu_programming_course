from __future__ import annotations

import pytest

from app.api.attempts import _attempt_read
from app.services.common import DomainError
from app.services.workspace import (
    replace_file_content,
    retry_submission_checkpoint,
    submit_attempt,
)
from tests.test_workspace_fresh_state import _start


async def test_deleted_attempt_exposes_distinct_read_only_reason(db, app_bundle):
    _, assessment, attempt, workspace, _ = await _start(db)
    attempt.state = "VOID"
    attempt.submission_source = "MOODLE_DELETED"
    await db.commit()
    read = await _attempt_read(db, attempt, assessment, workspace, app_bundle[2])
    assert read.state == "VOID"
    assert read.closure_reason == "LMS_ATTEMPT_DELETED"


@pytest.mark.parametrize("operation", ["save", "submit", "retry"])
async def test_deleted_attempt_cannot_be_changed_or_retried(db, operation):
    student, _, attempt, _, files = await _start(db)
    original_source = files[0].content
    attempt.state = "VOID"
    attempt.submission_source = "MOODLE_DELETED"
    await db.commit()
    with pytest.raises(DomainError) as error:
        if operation == "save":
            await replace_file_content(
                db,
                attempt_id=attempt.id,
                principal_id=student.id,
                file_id=files[0].id,
                content="must not be saved",
                expected_revision=0,
                client_request_id="deleted-attempt-save",
            )
        elif operation == "submit":
            await submit_attempt(
                db,
                attempt_id=attempt.id,
                principal_id=student.id,
                expected_revision=0,
            )
        else:
            await retry_submission_checkpoint(db, attempt_id=attempt.id, principal_id=student.id)
    assert error.value.code == "LMS_ATTEMPT_DELETED"
    assert error.value.status_code == 423
    assert files[0].content == original_source
