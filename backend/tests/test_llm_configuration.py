from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.api.ai import _effective_ai_flags
from app.api.attempts import _effective_flags
from app.api.system import _settings_read
from app.core.config import Settings
from app.integrations.ai import AIProvider
from app.integrations.errors import IntegrationConfigurationError, IntegrationProtocolError


def settings(**values) -> Settings:
    return Settings(
        _env_file=None,
        APP_SECRET_KEY="s" * 32,
        APP_DEBUG=False,
        ai_enabled=True,
        **values,
    )


def test_new_environment_overrides_legacy_including_empty_key(monkeypatch):
    for name, value in {
        "LLM_API_ADDRESS": "http://host.docker.internal:11434/api",
        "LLM_API_KEY": "",
        "LLM_MODEL": "local/model",
        "LLM_THINKING": "0",
        "AI_BASE_URL": "https://old.example/v1",
        "AI_API_KEY": "old-secret",
        "AI_MODEL": "old-model",
    }.items():
        monkeypatch.setenv(name, value)
    config = settings()
    assert config.ai_base_url == "http://host.docker.internal:11434/api"
    assert config.ai_api_key.get_secret_value() == ""
    assert config.ai_model == "local/model"
    assert config.llm_thinking is False
    assert config.ai_api_style == "auto"
    assert config.ai_provider_configured


def test_old_ai_environment_remains_compatible(monkeypatch):
    for name in ("LLM_API_ADDRESS", "LLM_API_KEY", "LLM_MODEL", "LLM_THINKING"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AI_BASE_URL", "https://old.example/v1")
    monkeypatch.setenv("AI_API_KEY", "legacy-secret")
    monkeypatch.setenv("AI_MODEL", "legacy-model")
    config = settings()
    assert config.ai_base_url == "https://old.example/v1"
    assert config.ai_api_key.get_secret_value() == "legacy-secret"
    assert config.ai_model == "legacy-model"
    assert config.ai_api_style == "responses"
    assert config.llm_thinking is None


@pytest.mark.parametrize(
    "raw, expected", [("0", False), ("1", True), ("false", False), ("true", True), ("", None)]
)
def test_thinking_boolean(raw, expected, monkeypatch):
    monkeypatch.setenv("LLM_THINKING", raw)
    assert settings().llm_thinking is expected


@pytest.mark.parametrize("raw", ["2", "maybe", "-1"])
def test_thinking_rejects_invalid_setting(raw, monkeypatch):
    monkeypatch.setenv("LLM_THINKING", raw)
    with pytest.raises(ValidationError):
        settings()


@pytest.mark.parametrize("thinking", [False, True, None])
@pytest.mark.parametrize(
    "address",
    [
        "http://localhost:11434",
        "http://localhost:11434/api/",
        "http://localhost:11434/api/chat",
        "http://host.docker.internal:11434/api",
        "http://192.168.1.7:11434/api",
        "http://[::1]:11434/api",
    ],
)
async def test_native_ollama_request_without_key_in_production(address, thinking):
    config = settings(
        LLM_API_ADDRESS=address, LLM_API_KEY="", LLM_MODEL="local/model", llm_thinking=thinking
    )
    assert not config.debug

    async def handler(request):
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers
        payload = json.loads(request.content)
        assert payload["model"] == "local/model"
        assert payload["stream"] is False
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1] == {"role": "user", "content": "Earlier question"}
        assert "revision" in payload["messages"][-1]["content"]
        if thinking is None:
            assert "think" not in payload
        else:
            assert payload["think"] is thinking
        return httpx.Response(
            200,
            json={
                "model": "local/model",
                "done": True,
                "message": {
                    "role": "assistant",
                    "thinking": "private reasoning",
                    "content": "Проверьте границы массива.",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        answer = await AIProvider(config, client).answer(
            mode="TEACHER",
            question="Что проверить?",
            context={"revision": 3},
            history=[{"role": "user", "content": "Earlier question"}],
        )
    assert answer.content == "Проверьте границы массива."
    assert answer.model == "local/model"


@pytest.mark.parametrize("thinking", [False, True, None])
@pytest.mark.parametrize(
    "address, kind, endpoint",
    [
        ("http://localhost:11434/v1", "chat", "/v1/chat/completions"),
        ("https://llm.example/v1/chat/completions", "chat", "/v1/chat/completions"),
        ("https://openrouter.ai/api/v1", "router", "/api/v1/chat/completions"),
        ("https://llm.example/v1/responses", "responses", "/v1/responses"),
    ],
)
async def test_compatible_endpoints_and_thinking_contract(thinking, address, kind, endpoint):
    config = settings(
        LLM_API_ADDRESS=address,
        LLM_API_KEY="provider-key",
        LLM_MODEL="test-model",
        llm_thinking=thinking,
    )

    async def handler(request):
        assert request.url.path == endpoint
        assert request.headers["authorization"] == "Bearer provider-key"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        if thinking is None:
            assert "reasoning_effort" not in payload and "reasoning" not in payload
        elif kind == "chat":
            assert payload["reasoning_effort"] == ("medium" if thinking else "none")
        elif kind == "router":
            assert payload["reasoning"] == {"enabled": thinking}
        else:
            assert payload["reasoning"] == {"effort": "medium" if thinking else "none"}
        return httpx.Response(
            200,
            json={
                "output_text": "Answer",
                "choices": [
                    {"message": {"content": "Answer", "reasoning": "Not a final answer"}},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AIProvider(config, client).answer(
            mode="TEACHER", question="Why?", context={}
        )
    assert result.content == "Answer"


@pytest.mark.parametrize("mode", ["STUDENT", "TEACHER"])
@pytest.mark.parametrize("key", ["", "provider-key"])
@pytest.mark.parametrize("style", ["auto", "chat_completions"])
@pytest.mark.parametrize(
    "address, endpoint",
    [
        ("http://localhost:11434/v1", "http://localhost:11434/v1/chat/completions"),
        ("http://192.168.1.7:8000/v1", "http://192.168.1.7:8000/v1/chat/completions"),
        ("http://192.168.1.8:30000/v1/", "http://192.168.1.8:30000/v1/chat/completions"),
        ("https://llm.example/proxy/v1/", "https://llm.example/proxy/v1/chat/completions"),
        ("https://llm.example/v1/chat/completions/", "https://llm.example/v1/chat/completions"),
    ],
)
async def test_portable_chat_completions_contract(address, endpoint, style, key, mode):
    """All self-hosted servers/tunnels use the same optional-auth JSON contract."""
    config = settings(
        LLM_API_ADDRESS=address,
        LLM_API_KEY=key,
        LLM_MODEL="served-model-id",
        LLM_THINKING="",
        AI_API_STYLE=style,
    )
    content = "Проверьте границы массива: https://en.cppreference.com/w/cpp/container/array"
    history = [
        {"role": "user", "content": "Предыдущий вопрос"},
        {"role": "assistant", "content": "Предыдущая подсказка"},
    ]

    async def handler(request):
        assert request.method == "POST"
        assert str(request.url) == endpoint
        assert request.headers["content-type"] == "application/json"
        assert request.headers["accept"] == "application/json"
        if key:
            assert request.headers["authorization"] == f"Bearer {key}"
        else:
            assert "authorization" not in request.headers
        payload = json.loads(request.content)
        # No provider-specific thinking fields when LLM_THINKING is empty.
        assert set(payload) == {"model", "messages", "stream"}
        assert payload["model"] == "served-model-id"
        assert payload["stream"] is False
        messages = payload["messages"]
        assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
        assert all(isinstance(message["content"], str) for message in messages)
        assert messages[1:3] == history
        assert "revision" in messages[-1]["content"]
        assert "Что проверить?" in messages[-1]["content"]
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "model": "served-model-id",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": content,
                            "reasoning_content": "Not the final answer",
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await AIProvider(config, client).answer(
            mode=mode,
            question="Что проверить?",
            context={"revision": 3},
            history=history,
        )
    assert result.content == content
    assert result.model == "served-model-id"
    assert result.safety_outcome == "ALLOWED"


@pytest.mark.parametrize(
    "address",
    [
        "http://public.example/v1",
        "http://169.254.169.254/api",
        "http://0.0.0.0:11434/api",
        "http://user:password@localhost:11434/api",
        "http://localhost:11434/api?key=secret",
        "http://localhost:bad/api",
    ],
)
async def test_invalid_or_public_plaintext_addresses_do_not_send_requests(address):
    async def handler(_request):
        pytest.fail("Invalid endpoint must not receive requests")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationConfigurationError):
            await AIProvider(settings(LLM_API_ADDRESS=address), client).answer(
                mode="TEACHER",
                question="Why?",
                context={},
            )


@pytest.mark.parametrize("style", ["ollama", "chat_completions"])
async def test_thinking_only_response_is_not_returned_as_an_answer(style):
    async def handler(_request):
        message = {"content": None, "thinking": "Not the answer", "reasoning": "Not the answer"}
        return httpx.Response(200, json={"message": message, "choices": [{"message": message}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(IntegrationProtocolError, match="empty response"):
            await AIProvider(
                settings(LLM_API_ADDRESS="http://localhost:11434/api", ai_api_style=style), client
            ).answer(
                mode="TEACHER",
                question="Why?",
                context={},
            )


async def test_local_keyless_provider_is_enabled_in_ai_system_and_workspace_settings(db):
    config = settings(
        LLM_API_ADDRESS="http://host.docker.internal:11434/api",
        LLM_MODEL="local/model",
        LLM_API_KEY="",
    )
    assert await _effective_ai_flags(db, config) == (True, True)
    workspace_flags = await _effective_flags(db, config)
    assert workspace_flags["ai_enabled"] and workspace_flags["student_ai_enabled"]
    result = await _settings_read(db, config)
    assert result.ai_enabled and result.student_ai_enabled
    config.ai_enabled = False
    assert await _effective_ai_flags(db, config) == (False, False)
    workspace_flags = await _effective_flags(db, config)
    assert not workspace_flags["ai_enabled"] and not workspace_flags["student_ai_enabled"]


@pytest.mark.parametrize("address", ["https://openrouter.ai/api/v1", "https://api.openai.com/v1"])
def test_hosted_provider_still_requires_api_key(address):
    assert not settings(LLM_API_ADDRESS=address, LLM_API_KEY="").ai_provider_configured
