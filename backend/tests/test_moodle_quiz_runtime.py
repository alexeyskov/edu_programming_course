from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.api.attempts import _prepare_deferred_moodle_quiz_attempt
from app.core.credential_crypto import (
    BROWSER_STATE_CREDENTIAL_KIND,
    decrypt_moodle_browser_state,
    encrypt_moodle_browser_state,
)
from app.integrations.moodle_browser import (
    MoodleBrowserClient,
    MoodleBrowserQuizEssayPreparation,
    MoodleBrowserQuizEssayPrepareResult,
)
from app.models.courses import Course, CourseMembership
from app.models.identity import ExternalPrincipal, LMSConnection, MoodleCredential
from app.models.integration import ExternalMapping
from app.models.tasks import Assessment, AssessmentItem, TaskBankItem, TaskVersion
from app.services.workspace import start_attempt


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


async def test_deferred_quiz_preparation_uses_student_session_and_persists_binding(
    app_bundle,
    monkeypatch,
) -> None:
    _, session_factory, settings = app_bundle
    settings.moodle_browser_service_url = "http://moodle-browser:8083"
    settings.moodle_browser_shared_secret = "browser-test-secret-" + "x" * 32
    async with session_factory() as db:
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
        student = ExternalPrincipal(
            connection_id=connection.id,
            external_subject="42",
            display_name="Student",
        )
        course = Course(
            connection_id=connection.id,
            external_id="549",
            title="C++",
            catalog_enabled=True,
        )
        db.add_all([student, course])
        await db.flush()
        db.add(
            CourseMembership(
                course_id=course.id,
                principal_id=student.id,
                role="STUDENT",
            )
        )
        item = TaskBankItem(
            course_id=course.id,
            slug="moodle-quiz-30354",
            created_by_id=student.id,
        )
        db.add(item)
        await db.flush()
        version = TaskVersion(
            item_id=item.id,
            number=1,
            title="Самостоятельная работа №1",
            statement="",
            language="CPP",
            language_standard="C++20",
            multi_file=False,
            starter_files=[{"path": "main.cpp", "content": ""}],
            build_profile="cpp-gcc-c++20-single",
            max_score=Decimal("5.00"),
            content_hash="a" * 64,
            status="PUBLISHED",
            authored_by_id=student.id,
        )
        db.add(version)
        await db.flush()
        assessment = Assessment(
            course_id=course.id,
            title="Самостоятельная работа №1",
            status="PUBLISHED",
            attempt_limit=2,
            max_score=Decimal("5.00"),
            created_by_id=student.id,
        )
        db.add(assessment)
        await db.flush()
        db.add(
            AssessmentItem(
                assessment_id=assessment.id,
                task_version_id=version.id,
                points=Decimal("5.00"),
            )
        )
        source_confirmation = {
            "title": True,
            "settings": True,
            "statement": False,
            "schedule": True,
            "duration": True,
            "grade": True,
            "attempt_policy": True,
            "statement_deferred": True,
        }
        db.add(
            ExternalMapping(
                connection_id=connection.id,
                local_type="Assessment",
                local_id=assessment.id,
                external_type="mod_quiz",
                external_id="30354",
                metadata_json={
                    "module": "quiz",
                    "cmid": 30354,
                    "submission_mode": "REQUIRES_CONFIGURATION",
                    "sync_state": "ANSWER_TRANSPORT_UNSUPPORTED",
                    "moodle_source_confirmation": source_confirmation,
                    "activity": {
                        "module": "quiz",
                        "cmid": 30354,
                        "question_count": 1,
                        "essay_question_count": 0,
                        "random_question_count": 1,
                        "random_essay_confirmed": True,
                        "statement_deferred": True,
                        "import_supported": True,
                        "statement_confirmed": False,
                    },
                },
            )
        )
        credential = MoodleCredential(
            connection_id=connection.id,
            principal_id=student.id,
            kind=BROWSER_STATE_CREDENTIAL_KIND,
            encrypted_secret=encrypt_moodle_browser_state(
                _browser_state("before"),
                settings,
                connection_id=connection.id,
                principal_id=student.id,
            ),
            status="ACTIVE",
        )
        db.add(credential)
        await db.commit()

        captured: dict[str, Any] = {}

        async def prepare(
            _browser: MoodleBrowserClient,
            course_id: str,
            cmid: int,
            **kwargs: Any,
        ) -> MoodleBrowserQuizEssayPrepareResult:
            captured.update({"course_id": course_id, "cmid": cmid, **kwargs})
            return MoodleBrowserQuizEssayPrepareResult(
                preparation=MoodleBrowserQuizEssayPreparation(
                    course_id="549",
                    cmid=30354,
                    attempt_id="141716",
                    question_slot="1",
                    question_text="Создайте класс трёхмерного вектора.",
                    answer_transport="ESSAY_ATTACHMENT",
                    available_answer_transports=(
                        "ESSAY_ATTACHMENT",
                        "ESSAY_ONLINE_TEXT",
                    ),
                    remaining_seconds=6_332,
                ),
                storage_state=_browser_state("after"),
            )

        monkeypatch.setattr(MoodleBrowserClient, "prepare_quiz_essay", prepare)
        prepared = await _prepare_deferred_moodle_quiz_attempt(
            db,
            settings,
            assessment_id=assessment.id,
            principal_id=student.id,
        )
        assert prepared is not None
        assert prepared.remaining_seconds == 6_332
        attempt = await start_attempt(
            db,
            assessment_id=assessment.id,
            principal_id=student.id,
            prepared_moodle_quiz=prepared,
        )
        await db.commit()

        refreshed = await db.scalar(
            select(MoodleCredential).where(MoodleCredential.id == credential.id)
        )
        assert refreshed is not None
        await db.refresh(refreshed)
        assigned = await db.get(TaskVersion, attempt.assigned_task_version_id)

    assert captured == {"course_id": "549", "cmid": 30354}
    assert refreshed.revision == 2
    assert refreshed.lease_owner is None
    state = decrypt_moodle_browser_state(
        refreshed.encrypted_secret,
        settings,
        connection_id=connection.id,
        principal_id=student.id,
    )
    assert state["cookies"][0]["value"] == "after"
    assert assigned is not None
    assert assigned.statement == "Создайте класс трёхмерного вектора."
    assert attempt.integrity_policy["moodle_attempt_id"] == "141716"
    assert attempt.integrity_policy["moodle_question_slot"] == "1"
    assert attempt.integrity_policy["moodle_answer_transport"] == "ESSAY_ATTACHMENT"
    assert attempt.expected_end_at is not None
    assert 6_330 <= (attempt.expected_end_at - attempt.started_at).total_seconds() <= 6_333
    assert attempt.deadline_at is None
