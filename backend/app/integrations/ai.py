from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx

from app.core.config import Settings

from ._http import canonical_json, request_json_limited, secret_value
from .errors import IntegrationConfigurationError, IntegrationProtocolError

Mode = Literal["STUDENT", "TEACHER"]

_SOLUTION_REQUEST = re.compile(
    r"(?:напиши|сгенерируй|реши|дай|пришли|выведи|реализуй|сделай|допиши|исправь)"
    r"\s+(?:мне\s+)?(?:эту\s+)?"
    r"(?:готов(?:ый|ое)|полный|весь)?\s*(?:код|решение|программ[ау]|задач[ау])"
    r"|(?:напиши|реализуй|сделай|допиши|исправь)\s+(?:эту\s+)?(?:функци[юя]|метод)"
    r"|(?:write|generate|give|provide|solve|implement|complete|fix)\s+"
    r"(?:me\s+)?(?:the\s+)?(?:complete|full|ready)?\s*"
    r"(?:code|solution|program|function|method|task)",
    re.IGNORECASE,
)
_FENCED_CODE = re.compile(r"```(?:c|cc|cpp|c\+\+)?\s*([\s\S]*?)```", re.IGNORECASE)
_URL = re.compile(r"https://[^\s<>\])}\"']+")


@dataclass(frozen=True, slots=True)
class AIAnswer:
    content: str
    citations: list[dict[str, str]]
    model: str
    safety_outcome: str = "ALLOWED"


class AIProvider:
    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient,
        *,
        api_style: str | None = None,
    ) -> None:
        self.settings = settings
        self.client = client
        configured_style = (api_style or getattr(settings, "ai_api_style", "responses")).lower()
        self.api_style = (
            "chat" if configured_style in {"chat", "chat_completions"} else configured_style
        )
        self.timeout_seconds = float(getattr(settings, "ai_timeout_seconds", 45))
        self.response_limit = int(getattr(settings, "ai_max_response_bytes", 1_000_000))
        self.context_limit = int(
            getattr(
                settings,
                "ai_max_context_bytes",
                getattr(settings, "ai_max_context_chars", 100_000),
            )
        )
        self.output_limit = int(getattr(settings, "ai_max_output_chars", 12_000))
        self.history_limit = int(
            getattr(
                settings,
                "ai_history_messages",
                getattr(settings, "ai_max_history_messages", 12),
            )
        )
        self.history_chars = int(
            getattr(settings, "ai_max_history_chars", max(4_000, self.context_limit // 2))
        )

    async def answer(
        self,
        *,
        mode: Mode | str,
        question: str,
        context: dict[str, Any],
        history: Sequence[dict[str, Any]] = (),
    ) -> AIAnswer:
        normalized_mode = str(mode).upper()
        if normalized_mode not in {"STUDENT", "TEACHER"}:
            raise IntegrationProtocolError("Unsupported AI assistant mode")
        if not isinstance(question, str) or not question.strip():
            raise IntegrationProtocolError("AI question must not be empty")
        question = question.strip()[:20_000]
        if normalized_mode == "STUDENT" and self._blocked_input(question):
            return AIAnswer(
                content=(
                    "Я не могу написать готовое решение за вас. Могу помочь разбить задачу "
                    "на шаги, "
                    "объяснить нужную конструкцию C/C++ или разобрать конкретную диагностику."
                ),
                citations=[],
                model="student-policy-v1",
                safety_outcome="BLOCKED_INPUT",
            )
        if self.settings.ai_mock_enabled:
            content = (
                "Разберите сообщение компилятора сверху вниз, проверьте типы выражений и "
                "сведите проблему к минимальному примеру."
                if normalized_mode == "STUDENT"
                else (
                    "Сопоставьте диагностики, граничные случаи и условие; решение "
                    "об оценке остаётся за преподавателем."
                )
            )
            return AIAnswer(
                content=content,
                citations=[{"title": "cppreference", "url": "https://en.cppreference.com/w/"}],
                model="mock-policy-v1",
            )
        key = secret_value(self.settings.ai_api_key)
        if not self.settings.ai_enabled or not key:
            raise IntegrationConfigurationError("AI provider is not configured")
        if self.api_style not in {"responses", "chat"}:
            raise IntegrationConfigurationError("Unsupported AI API style")

        messages = self._bounded_history(history)
        context_bytes = canonical_json(context)
        context_text = context_bytes[: self.context_limit].decode("utf-8", errors="ignore")
        if len(context_bytes) > self.context_limit:
            context_text += "\n[context truncated by configured byte limit]"
        user_content = f"Context: {context_text}\n\nQuestion: {question}"
        system = self._system(normalized_mode)
        base = self._validated_base_url()
        if self.api_style == "chat":
            endpoint = f"{base}/chat/completions"
            payload: dict[str, Any] = {
                "model": self.settings.ai_model,
                "messages": [
                    {"role": "system", "content": system},
                    *messages,
                    {"role": "user", "content": user_content},
                ],
            }
        else:
            endpoint = f"{base}/responses"
            payload = {
                "model": self.settings.ai_model,
                "instructions": system,
                "input": [*messages, {"role": "user", "content": user_content}],
            }
        result = await request_json_limited(
            self.client,
            "POST",
            endpoint,
            timeout_seconds=self.timeout_seconds,
            response_limit=self.response_limit,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            content=canonical_json(payload),
        )
        if not isinstance(result, dict):
            raise IntegrationProtocolError("AI provider returned an invalid response")
        content = self._content(result)
        if not content:
            raise IntegrationProtocolError("AI provider returned an empty response")
        if len(content) > self.output_limit:
            raise IntegrationProtocolError("AI provider output exceeds the configured limit")
        cumulative_assistant_output = "\n".join(
            item["content"] for item in messages if item["role"] == "assistant"
        )
        output_for_gate = (
            f"{cumulative_assistant_output}\n{content}" if cumulative_assistant_output else content
        )
        if normalized_mode == "STUDENT" and self._blocked_output(output_for_gate):
            return AIAnswer(
                content=(
                    "Ответ был остановлен учебной политикой, потому что выглядел как готовое "
                    "решение. Сформулируйте вопрос о конкретной концепции или "
                    "сообщении компилятора."
                ),
                citations=[],
                model=self.settings.ai_model,
                safety_outcome="BLOCKED_SOLUTION",
            )
        citations = self._citations(result, content)
        if normalized_mode == "STUDENT" and not citations:
            raise IntegrationProtocolError(
                "Student AI response has no allowlisted cppreference citation"
            )
        return AIAnswer(
            content=content,
            citations=citations,
            model=str(result.get("model") or self.settings.ai_model),
        )

    def _bounded_history(self, history: Sequence[dict[str, Any]]) -> list[dict[str, str]]:
        if self.history_limit <= 0 or self.history_chars <= 0:
            return []
        accepted: list[dict[str, str]] = []
        used = 0
        for item in reversed(list(history)[-self.history_limit :]):
            if not isinstance(item, dict):
                continue
            role = str(item.get("role", "")).lower()
            content = item.get("content")
            if role not in {"user", "assistant"} or not isinstance(content, str):
                continue
            remaining = self.history_chars - used
            if remaining <= 0:
                break
            bounded = content[-remaining:]
            accepted.append({"role": role, "content": bounded})
            used += len(bounded)
        accepted.reverse()
        return accepted

    def _content(self, result: dict[str, Any]) -> str:
        if self.api_style == "chat":
            choices = result.get("choices", [])
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                return ""
            message = choices[0].get("message", {})
            return str(message.get("content", "")) if isinstance(message, dict) else ""
        direct = result.get("output_text")
        if isinstance(direct, str) and direct:
            return direct
        output = result.get("output", [])
        if not isinstance(output, list):
            return ""
        parts: list[str] = []
        for item in output:
            if not isinstance(item, dict) or not isinstance(item.get("content", []), list):
                continue
            for part in item["content"]:
                if isinstance(part, dict) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "".join(parts)

    def _citations(self, result: dict[str, Any], content: str) -> list[dict[str, str]]:
        candidates: list[tuple[str, str]] = []
        explicit = result.get("citations", [])
        if isinstance(explicit, list):
            for item in explicit:
                if isinstance(item, dict):
                    candidates.append(
                        (
                            str(item.get("title", "cppreference")),
                            str(item.get("url", "")),
                        )
                    )
        for item in result.get("output", []) if isinstance(result.get("output"), list) else []:
            if not isinstance(item, dict):
                continue
            for part in item.get("content", []) if isinstance(item.get("content"), list) else []:
                if not isinstance(part, dict):
                    continue
                annotations = (
                    part.get("annotations", []) if isinstance(part.get("annotations"), list) else []
                )
                for annotation in annotations:
                    if isinstance(annotation, dict):
                        candidates.append(
                            (
                                str(annotation.get("title", "cppreference")),
                                str(annotation.get("url", annotation.get("uri", ""))),
                            )
                        )
        candidates.extend(("cppreference", url.rstrip(".,;:")) for url in _URL.findall(content))
        citations: list[dict[str, str]] = []
        seen: set[str] = set()
        for title, url in candidates:
            if url in seen or not self._allowed_citation(url):
                continue
            seen.add(url)
            citations.append({"title": title[:200] or "cppreference", "url": url})
            if len(citations) == 10:
                break
        return citations

    @staticmethod
    def _allowed_citation(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            return (
                parsed.scheme == "https"
                and parsed.hostname
                in {"en.cppreference.com", "www.cppreference.com", "cppreference.com"}
                and not parsed.username
                and not parsed.password
                and parsed.port in {None, 443}
                and (parsed.path == "/w" or parsed.path.startswith("/w/"))
            )
        except ValueError:
            return False

    def _validated_base_url(self) -> str:
        base = self.settings.ai_base_url.rstrip("/")
        try:
            parsed = urlsplit(base)
        except ValueError as exc:
            raise IntegrationConfigurationError("AI provider URL is invalid") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not self.settings.debug)
        ):
            raise IntegrationConfigurationError("AI provider URL is invalid")
        return base

    @staticmethod
    def _blocked_input(question: str) -> bool:
        return bool(_SOLUTION_REQUEST.search(question))

    @staticmethod
    def _blocked_output(content: str) -> bool:
        if any(len(block.strip()) >= 80 for block in _FENCED_CODE.findall(content)):
            return True
        if "#include" in content and re.search(r"\b(?:int|auto)\s+main\s*\(", content):
            return True
        function = re.search(
            r"\b(?:void|bool|char|int|long|float|double|auto|std::\w+)\s+\w+\s*\([^)]*\)\s*\{",
            content,
        )
        if function:
            return True
        code_like_control_flow = re.search(r"\b(?:if|for|while|switch)\s*\([^)]*\)\s*\{", content)
        return bool(code_like_control_flow and content.count(";") >= 3)

    @staticmethod
    def _system(mode: str) -> str:
        if mode == "STUDENT":
            return (
                "You are a C/C++ tutor. Explain concepts, compiler diagnostics, and debugging "
                "steps. Never provide a complete solution, complete function, or directly "
                "compilable answer for the student's task. Refuse requests to write the "
                "solution and offer guiding "
                "questions instead. Cite only relevant pages under https://en.cppreference.com/w/."
            )
        return (
            "You assist a C/C++ teacher reviewing a fixed submission snapshot. Explain "
            "diagnostics, "
            "correctness, edge cases, complexity, and standard-library behavior. Separate evidence "
            "from inference; never make or apply a grading decision. Cite only relevant "
            "pages under "
            "https://en.cppreference.com/w/."
        )
