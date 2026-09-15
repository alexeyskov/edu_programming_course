from __future__ import annotations

import base64
import copy
import hashlib
import json
from typing import Any

import httpx
import pytest

from app.integrations.errors import (
    IntegrationAttemptFinalized,
    IntegrationConfigurationError,
    IntegrationProtocolError,
)
from app.integrations.moodle_browser import (
    MoodleBrowserClient,
    MoodleBrowserQuizAnswer,
    MoodleBrowserQuizEssayArtifact,
)
from tests.test_moodle_browser_client import settings, storage_state

COURSE_ID = "508"
CMID = 31529
ATTEMPT_ID = "141716"
IDEMPOTENCY_KEY = "quiz:508:31529:141716:bundle1"


def browser_client(client: httpx.AsyncClient) -> MoodleBrowserClient:
    return MoodleBrowserClient(
        settings(),
        client,
        service_url="http://moodle-browser:8082",
        shared_secret="b" * 32,
        storage_state=storage_state(),
    )


def preparation_payload(count: int = 2) -> dict[str, Any]:
    questions = [
        {
            "question_slot": slot,
            "question_text": f"Условие задачи {index + 1}",
            "answer_transport": "ESSAY_ATTACHMENT",
            "available_answer_transports": ["ESSAY_ATTACHMENT"],
            "page": page,
            "question_max_mark": mark,
        }
        for index, (slot, page, mark) in enumerate(
            [("2", 0, 1.5), ("7", 2, 2.0), ("9", 3, 3.25)][:count]
        )
    ]
    return {
        "status": "READY",
        "preparation": {
            "course_id": COURSE_ID,
            "cmid": CMID,
            "attempt_id": ATTEMPT_ID,
            "question_slot": questions[0]["question_slot"],
            "question_text": questions[0]["question_text"],
            "answer_transport": questions[0]["answer_transport"],
            "available_answer_transports": list(questions[0]["available_answer_transports"]),
            "remaining_seconds": 7050,
            "questions": questions,
        },
        "storage_state": storage_state(marker="after-quiz-prepare"),
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("answer_transport", []),
        ("available_answer_transports", [{}]),
        ("question_max_mark", 1_000_000),
    ],
)
async def test_prepared_question_invalid_shapes_raise_protocol_error(field, value):
    async with httpx.AsyncClient() as client:
        browser = browser_client(client)
        questions = preparation_payload()["preparation"]["questions"]
        questions[1][field] = value
        with pytest.raises(IntegrationProtocolError):
            browser._normalise_prepared_quiz_questions(questions)


@pytest.mark.parametrize(
    ("status", "error"),
    [(423, IntegrationAttemptFinalized), (501, IntegrationConfigurationError)],
)
async def test_batch_delivery_translates_finalized_and_unsupported_endpoint(status, error):
    async def handler(_request):
        return httpx.Response(status, json={"detail": "Unavailable"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(error):
            await browser_client(client).sync_quiz_answers(
                COURSE_ID,
                CMID,
                quiz_answers(),
                expected_attempt_id=ATTEMPT_ID,
                finalize=False,
                idempotency_key=IDEMPOTENCY_KEY,
            )


@pytest.mark.parametrize("count", [2, 3])
async def test_prepare_preserves_every_question_identity_page_and_maximum(count: int) -> None:
    response = preparation_payload(count)
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await browser_client(client).prepare_quiz_essay(
            COURSE_ID,
            CMID,
            expected_attempt_id=ATTEMPT_ID,
            expected_question_slot="2",
        )

    assert captured["url"] == "http://moodle-browser:8082/internal/v1/moodle/quiz/essay/prepare"
    assert captured["body"] == {
        "schema_version": "1.0",
        "base_url": "https://moodle.example.edu",
        "course_id": COURSE_ID,
        "cmid": CMID,
        "expected_attempt_id": ATTEMPT_ID,
        "expected_question_slot": "2",
        "storage_state": storage_state(),
    }
    prepared = result.preparation
    assert (prepared.course_id, prepared.cmid, prepared.attempt_id) == (
        COURSE_ID,
        CMID,
        ATTEMPT_ID,
    )
    assert prepared.remaining_seconds == 7050
    assert len(prepared.questions) == count
    for question, expected in zip(
        prepared.questions, response["preparation"]["questions"], strict=True
    ):
        assert question.question_slot == expected["question_slot"]
        assert question.question_text == expected["question_text"]
        assert question.answer_transport == expected["answer_transport"]
        assert question.available_answer_transports == tuple(
            expected["available_answer_transports"]
        )
        assert question.page == expected["page"]
        assert question.question_max_mark == expected["question_max_mark"]
    assert prepared.question_slot == prepared.questions[0].question_slot
    assert result.storage_state == storage_state(marker="after-quiz-prepare")


@pytest.mark.parametrize(
    "corruption",
    [
        "duplicate_slot",
        "nonnumeric_slot",
        "zero_slot",
        "missing_maximum",
        "invalid_page",
        "unknown_transport",
        "first_scalar_slot_mismatch",
        "first_scalar_text_mismatch",
        "first_scalar_transport_mismatch",
        "first_scalar_available_mismatch",
    ],
)
async def test_prepare_rejects_ambiguous_or_incomplete_question_bindings(corruption: str) -> None:
    response = preparation_payload()
    prepared = response["preparation"]
    questions = prepared["questions"]
    if corruption == "duplicate_slot":
        questions[1]["question_slot"] = questions[0]["question_slot"]
    elif corruption == "nonnumeric_slot":
        questions[1]["question_slot"] = "unknown"
    elif corruption == "zero_slot":
        questions[1]["question_slot"] = "0"
    elif corruption == "missing_maximum":
        questions[1].pop("question_max_mark")
    elif corruption == "invalid_page":
        questions[1]["page"] = -1
    elif corruption == "unknown_transport":
        questions[1]["answer_transport"] = "UNKNOWN"
    elif corruption == "first_scalar_slot_mismatch":
        questions[0]["question_slot"] = "3"
    elif corruption == "first_scalar_text_mismatch":
        questions[0]["question_text"] = "Текст от другого вопроса"
    elif corruption == "first_scalar_transport_mismatch":
        questions[0]["answer_transport"] = "ESSAY_ONLINE_TEXT"
        questions[0]["available_answer_transports"] = ["ESSAY_ONLINE_TEXT"]
    else:
        questions[0]["available_answer_transports"] = ["ESSAY_ATTACHMENT", "ESSAY_ONLINE_TEXT"]

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = browser_client(client)
        with pytest.raises(IntegrationProtocolError):
            await browser.prepare_quiz_essay(COURSE_ID, CMID)
        assert browser.storage_state == storage_state()


def quiz_answers() -> tuple[MoodleBrowserQuizAnswer, ...]:
    return (
        MoodleBrowserQuizAnswer(
            question_slot="2",
            artifact=MoodleBrowserQuizEssayArtifact("main.cpp", b"int main() { return 1; }\n"),
            answer_transport="ESSAY_ATTACHMENT",
            previous_managed_filename="main.cpp",
            previous_managed_sha256="a" * 64,
        ),
        MoodleBrowserQuizAnswer(
            question_slot="7",
            artifact=MoodleBrowserQuizEssayArtifact(
                "main.cpp", b"// second\nint main() { return 2; }\n"
            ),
            answer_transport="ESSAY_ATTACHMENT",
            previous_managed_filename="main.cpp",
            previous_managed_sha256="b" * 64,
        ),
    )


def sync_response(
    answers: tuple[MoodleBrowserQuizAnswer, ...], *, finalize: bool
) -> dict[str, Any]:
    return {
        "status": "FINALIZED" if finalize else "DRAFT_SAVED",
        "receipts": [
            {
                "course_id": COURSE_ID,
                "cmid": CMID,
                "attempt_id": ATTEMPT_ID,
                "question_slot": answer.question_slot,
                "filename": answer.artifact.filename,
                "sha256": hashlib.sha256(answer.artifact.content).hexdigest(),
                "size_bytes": len(answer.artifact.content),
                "idempotency_key": IDEMPOTENCY_KEY,
            }
            for answer in answers
        ],
        "storage_state": storage_state(marker="after-bundle-sync"),
    }


@pytest.mark.parametrize("finalize", [False, True])
async def test_sync_keeps_same_filename_answers_separate_and_verifies_all_receipts(
    finalize: bool,
) -> None:
    answers = quiz_answers()
    response = sync_response(answers, finalize=finalize)
    # Receipt ordering cannot turn the two different main.cpp files into one.
    response["receipts"].reverse()
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await browser_client(client).sync_quiz_answers(
            COURSE_ID,
            CMID,
            answers,
            expected_attempt_id=ATTEMPT_ID,
            finalize=finalize,
            idempotency_key=IDEMPOTENCY_KEY,
        )

    assert captured["url"] == "http://moodle-browser:8082/internal/v1/moodle/quiz/answers/sync"
    body = captured["body"]
    assert body["course_id"] == COURSE_ID
    assert body["cmid"] == CMID
    assert body["expected_attempt_id"] == ATTEMPT_ID
    assert body["finalize"] is finalize
    assert body["idempotency_key"] == IDEMPOTENCY_KEY
    assert body["storage_state"] == storage_state()
    assert len(body["answers"]) == len(result.receipts) == 2
    for expected, sent, receipt in zip(answers, body["answers"], result.receipts, strict=True):
        digest = hashlib.sha256(expected.artifact.content).hexdigest()
        assert sent == {
            "question_slot": expected.question_slot,
            "answer_transport": expected.answer_transport,
            "artifact": {
                "filename": "main.cpp",
                "content_base64": base64.b64encode(expected.artifact.content).decode("ascii"),
                "sha256": digest,
            },
            "previous_managed_filename": expected.previous_managed_filename,
            "previous_managed_sha256": expected.previous_managed_sha256,
        }
        assert receipt.question_slot == expected.question_slot
        assert receipt.filename == "main.cpp"
        assert receipt.sha256 == digest
        assert receipt.size_bytes == len(expected.artifact.content)
        assert receipt.attempt_id == ATTEMPT_ID
        assert receipt.idempotency_key == IDEMPOTENCY_KEY
    assert result.receipts[0].sha256 != result.receipts[1].sha256
    assert result.status == ("FINALIZED" if finalize else "DRAFT_SAVED")
    assert result.storage_state == storage_state(marker="after-bundle-sync")


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_receipt",
        "duplicate_receipt",
        "unknown_slot",
        "wrong_attempt",
        "wrong_hash",
        "swapped_artifacts",
        "wrong_filename",
        "wrong_size",
        "wrong_idempotency_key",
        "not_finalized",
    ],
)
async def test_sync_rejects_incomplete_or_mismatched_per_question_confirmations(
    corruption: str,
) -> None:
    answers = quiz_answers()
    response = sync_response(answers, finalize=True)
    receipts = response["receipts"]
    if corruption == "missing_receipt":
        receipts.pop()
    elif corruption == "duplicate_receipt":
        receipts[1] = copy.deepcopy(receipts[0])
    elif corruption == "unknown_slot":
        receipts[1]["question_slot"] = "99"
    elif corruption == "wrong_attempt":
        receipts[1]["attempt_id"] = "999999"
    elif corruption == "wrong_hash":
        receipts[1]["sha256"] = "0" * 64
    elif corruption == "swapped_artifacts":
        for key in ("sha256", "size_bytes"):
            receipts[0][key], receipts[1][key] = receipts[1][key], receipts[0][key]
    elif corruption == "wrong_filename":
        receipts[1]["filename"] = "other.cpp"
    elif corruption == "wrong_size":
        receipts[1]["size_bytes"] += 1
    elif corruption == "wrong_idempotency_key":
        receipts[1]["idempotency_key"] = "quiz:another-bundle"
    else:
        response["status"] = "DRAFT_SAVED"

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        browser = browser_client(client)
        with pytest.raises(IntegrationProtocolError):
            await browser.sync_quiz_answers(
                COURSE_ID,
                CMID,
                answers,
                expected_attempt_id=ATTEMPT_ID,
                finalize=True,
                idempotency_key=IDEMPOTENCY_KEY,
            )
        assert browser.storage_state == storage_state()


@pytest.mark.parametrize("slot", ["2", "unknown", "0"])
async def test_sync_rejects_duplicate_or_invalid_slots_before_http(slot: str) -> None:
    first, second = quiz_answers()
    invalid = MoodleBrowserQuizAnswer(
        question_slot=slot,
        artifact=second.artifact,
        answer_transport=second.answer_transport,
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid answer slots must not reach the browser service")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationProtocolError):
            await browser_client(client).sync_quiz_answers(
                COURSE_ID,
                CMID,
                (first, invalid),
                expected_attempt_id=ATTEMPT_ID,
                finalize=True,
                idempotency_key=IDEMPOTENCY_KEY,
            )
