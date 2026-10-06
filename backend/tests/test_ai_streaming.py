from __future__ import annotations

import asyncio
import json
from contextlib import aclosing
from types import SimpleNamespace

import httpx
import pytest

from app.api.ai import _provider_with_heartbeats
from app.core.config import Settings
from app.integrations._reasoning import ReasoningProgress
from app.integrations.ai import AIProvider, AIStreamEvent
from app.integrations.errors import (
    IntegrationProtocolError,
    IntegrationResponseTooLarge,
    IntegrationTimeout,
    IntegrationUnavailable,
)


def settings(**kwargs):
    return Settings(
        _env_file=None,
        APP_SECRET_KEY="s" * 32,
        ai_enabled=True,
        LLM_API_ADDRESS="https://openrouter.ai/api/v1",
        LLM_API_KEY="test-key",
        LLM_MODEL="test/model",
        AI_API_STYLE="chat_completions",
        **kwargs,
    )


def frame(delta=None, **kwargs):
    return (
        "data: "
        + json.dumps({"choices": [{"delta": delta or {}, **kwargs}]}, ensure_ascii=False)
        + "\r\n\r\n"
    )


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, text, *, delay=0, chunk_size=7):
        self.raw = text.encode()
        self.closed = False
        self.delay = delay
        self.chunk_size = chunk_size

    async def __aiter__(self):
        for index in range(0, len(self.raw), self.chunk_size):
            if self.delay:
                await asyncio.sleep(self.delay)
            yield self.raw[index : index + self.chunk_size]

    async def aclose(self):
        self.closed = True


async def collect(text, config=None, *, chunk_size=7):
    stream = ByteStream(text, chunk_size=chunk_size)

    async def handler(request):
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["messages"][-1]["content"].endswith("What is wrong?")
        assert "Silently account for selection boundaries" in payload["messages"][0]["content"]
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, stream=stream, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [
            event
            async for event in AIProvider(config or settings(), client).stream_answer(
                mode="TEACHER",
                question="What is wrong?",
                context={"files": [{"path": "main.cpp", "content": "int main(){}"}]},
            )
        ]
    assert stream.closed
    return events


async def test_streams_content_incrementally_without_exposing_reasoning():
    text = ": OPENROUTER PROCESSING\r\n\r\n" + frame({"reasoning": "PRIVATE_REASONING"})
    text += frame({"reasoning_details": [{"text": "PRIVATE_REASONING"}]})
    text += frame({"content": "**Проверьте** "}) + frame({"content": "границы массива."})
    text += frame(finish_reason="stop") + 'data: {"choices":[],"usage":{}}\n\ndata: [DONE]\n\n'
    events = await collect(text)
    assert [e.kind for e in events] == ["thinking", "delta", "delta", "done"]
    assert events[-1].answer.content == "**Проверьте** границы массива."
    assert "PRIVATE_REASONING" not in repr(events)


async def test_stream_exposes_only_a_short_allowlisted_reasoning_topic():
    events = await collect(
        frame({"reasoning": "PRIVATE_REASONING: inspect this pointer and memory allocation."})
        + frame({"content": "Проверьте освобождение памяти."})
        + "data: [DONE]\n\n"
    )
    assert events[0].summary == "указатели и память"
    assert "PRIVATE_REASONING" not in repr(events)
    assert "pointer" not in repr(events)
    assert events[-1].answer.content == "Проверьте освобождение памяти."


def test_reasoning_topics_handle_fragmented_aliases_and_throttle_changes(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("app.integrations._reasoning.time.monotonic", lambda: now[0])
    progress = ReasoningProgress()
    assert not progress.update({"reasoning_content": "poi"})
    assert progress.update({"reasoning_content": "nter"})
    assert progress.summary == "указатели и память"
    assert not progress.update({"reasoning": " and array"})
    now[0] = 2.1
    assert progress.update({"reasoning_details": [{"type": "reasoning.text", "text": " bounds"}]})
    assert progress.summary == "индексы и границы массива"
    now[0] = 4.2
    assert progress.update(
        {"reasoning_details": [{"type": "reasoning.summary", "summary": "Проверяю строки"}]}
    )
    assert progress.summary == "строки и символы"
    assert not progress.update({"reasoning": " секретные детали" * 1000})
    assert len(progress._tail) <= 512
    assert "секретные" not in progress.summary


@pytest.mark.parametrize(
    "details",
    [
        None,
        "not an array",
        [None, {"type": []}],
        [{"type": "reasoning.encrypted", "data": "pointer"}],
    ],
)
def test_reasoning_topics_ignore_unknown_or_encrypted_data(details):
    progress = ReasoningProgress()
    assert not progress.update({"reasoning_details": details})
    assert progress.summary == "условие и код"


async def test_ai_defaults_allow_180_seconds_and_stream_idle_timeout_is_separate():
    assert settings().ai_timeout_seconds == 180
    assert settings().ai_stream_idle_timeout_seconds == 60
    seen = []

    async def handler(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(
            200,
            stream=ByteStream(frame({"content": "OK"}) + "data: [DONE]\n\n"),
            headers={"content-type": "text/event-stream"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async for _event in AIProvider(
            settings(ai_stream_idle_timeout_seconds=25), client
        ).stream_answer(mode="TEACHER", question="Question", context={}):
            pass
    assert seen[0]["read"] == 25
    assert seen[0]["connect"] == 180


async def test_heartbeats_do_not_cancel_or_repeat_a_slow_provider_request():
    calls = []

    class Provider:
        async def stream_answer(self, **_kwargs):
            calls.append("started")
            await asyncio.sleep(0.03)
            yield AIStreamEvent("delta", text="OK")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai_provider=Provider())))
    events = [event async for event in _provider_with_heartbeats(request, heartbeat_seconds=0.005)]
    assert events[0] is None
    assert events[-1].text == "OK"
    assert calls == ["started"]


async def test_closing_heartbeat_stream_cancels_the_pending_upstream_request():
    closed = asyncio.Event()

    class Provider:
        async def stream_answer(self, **_kwargs):
            try:
                await asyncio.sleep(60)
                yield AIStreamEvent("delta", text="never delivered")
            finally:
                closed.set()

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ai_provider=Provider())))
    async with aclosing(_provider_with_heartbeats(request, heartbeat_seconds=0.005)) as events:
        assert await anext(events) is None
    assert closed.is_set()


@pytest.mark.parametrize(
    "text",
    [
        frame({"content": "partial"}),
        "data: [DONE]\n\n",
        "data: not-json\n\n",
        "data: []\n\n",
        'data: {"error":{"message":"secret-upstream-body"}}\n\n',
        frame({"content": "partial"}, finish_reason="length") + "data: [DONE]\n\n",
        frame({"content": "partial"}, finish_reason="error") + "data: [DONE]\n\n",
    ],
)
async def test_rejects_broken_empty_or_failed_streams(text):
    with pytest.raises(IntegrationProtocolError) as error:
        await collect(text)
    assert "secret-upstream-body" not in str(error.value)


async def test_rejects_oversized_output_and_closes_upstream():
    with pytest.raises(IntegrationProtocolError, match="output exceeds"):
        await collect(
            frame({"content": "x" * 1025}) + "data: [DONE]\n\n",
            settings(ai_teacher_max_output_chars=1024),
        )


async def test_bounds_individual_stream_events_including_reasoning():
    with pytest.raises(IntegrationResponseTooLarge):
        await collect(frame({"reasoning": "x" * 2048}), settings(ai_max_response_bytes=1024))


async def test_wire_budget_is_separate_from_an_event_and_visible_answer():
    # Typical reasoning streams repeat metadata for every token. A tiny answer
    # must not fail just because the total stream crossed the old 1 MiB limit.
    reasoning = frame({"reasoning_details": [{"text": "PRIVATE_REASONING " * 20}]})
    text = reasoning * 6000 + frame({"content": "Короткий ответ."}) + "data: [DONE]\n\n"
    assert len(text.encode()) > 2 * 1024 * 1024
    events = await collect(text, chunk_size=8192)
    assert events[-1].answer.content == "Короткий ответ."
    assert "PRIVATE_REASONING" not in repr(events)


async def test_long_teacher_answer_is_complete_and_student_limit_is_unchanged():
    answer = "Подробное объяснение.\n" * 2500
    assert len(answer) > 12_000
    events = await collect(
        "".join(
            frame({"content": answer[index : index + 128]}) for index in range(0, len(answer), 128)
        )
        + "data: [DONE]\n\n",
        chunk_size=8192,
    )
    assert events[-1].answer.content == answer
    assert "".join(event.text for event in events if event.kind == "delta") == answer
    assert settings().ai_max_output_chars == 12_000
    assert settings().ai_teacher_max_output_chars == 131_072


async def test_stream_keeps_a_separate_total_wire_safety_limit():
    with pytest.raises(IntegrationResponseTooLarge, match="AI stream exceeds"):
        await collect(frame({"reasoning": "short"}) * 100, settings(ai_max_stream_bytes=1024))


@pytest.mark.parametrize("text", ["data: " + "x" * 2048, "data: x\n" * 300])
async def test_unterminated_or_multiline_events_cannot_grow_without_bound(text):
    with pytest.raises(IntegrationResponseTooLarge, match="event exceeds"):
        await collect(text, settings(ai_max_response_bytes=1024))


async def test_closing_consumer_closes_upstream_immediately():
    stream = ByteStream(
        frame({"content": "partial"}) + frame({"content": "more"}) + "data: [DONE]\n\n"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        )
    ) as client:
        async with aclosing(
            AIProvider(settings(), client).stream_answer(
                mode="TEACHER", question="Check", context={}
            )
        ) as events:
            first = await anext(events)
            assert first.kind == "delta"
        assert stream.closed


async def test_wall_clock_deadline_includes_dribbling_stream():
    stream = ByteStream(frame({"content": "partial"}) + "data: [DONE]\n\n", delay=0.01)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                stream=stream,
                headers={"content-type": "text/event-stream"},
            )
        )
    ) as client:
        provider = AIProvider(settings(), client)
        provider.timeout_seconds = 0.03
        with pytest.raises(IntegrationTimeout):
            _ = [
                event
                async for event in provider.stream_answer(
                    mode="TEACHER", question="Check", context={}
                )
            ]
    assert stream.closed


async def test_http_errors_do_not_disclose_upstream_body_or_key():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                401,
                text="secret-key-value",
            )
        )
    ) as client:
        with pytest.raises(IntegrationUnavailable) as caught:
            _ = [
                event
                async for event in AIProvider(settings(), client).stream_answer(
                    mode="TEACHER", question="Check", context={}
                )
            ]
    assert "401" in str(caught.value)
    assert "secret-key-value" not in str(caught.value)


async def test_student_responses_are_buffered_until_the_no_code_gate():
    async def handler(request):
        assert json.loads(request.content)["stream"] is False
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "```cpp\nint main(){}\n```"}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [
            event
            async for event in AIProvider(settings(), client).stream_answer(
                mode="STUDENT", question="Explain arrays", context={}
            )
        ]
    assert events[-1].answer.safety_outcome == "BLOCKED_SOLUTION"
    assert all("int main" not in event.text for event in events)
