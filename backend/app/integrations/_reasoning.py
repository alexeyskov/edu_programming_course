"""Coarse progress topics, never raw reasoning, code excerpts or extra model calls."""

from __future__ import annotations

import re
import time
from typing import Any

_TOPICS = [
    (r"\b(?:requirement|constraint|task statement)|услови|ограничени", "условие и ограничения"),
    (
        r"\b(?:syntax|compil|type|initializ)|синтакси|компил|типов|инициализац",
        "типы и синтаксис C/C++",
    ),
    (r"\b(?:pointer|memory|allocat|deallocat|delete|leak)|указател|памят", "указатели и память"),
    (r"\b(?:string|character|null.terminat)|строк|символ|терминатор", "строки и символы"),
    (r"\b(?:index|indices|array|bound)|индекс|массив|границ", "индексы и границы массива"),
    (r"\b(?:loop|iteration|branch)|цикл|итераци|ветвлен", "циклы и ветвления"),
    (
        r"\b(?:edge case|corner case|empty input|overflow)|крайн|граничн|пустой ввод|переполн",
        "граничные случаи",
    ),
]
_PATTERNS = [(re.compile(pattern, re.IGNORECASE), label) for pattern, label in _TOPICS]


class ReasoningProgress:
    """Infer only an allowlisted topic from the latest small window of provider data.

    This is an approximate UI hint, not a verbatim explanation of model reasoning.
    Unknown/encrypted data is ignored. Streaming aliases are not concatenated twice.
    """

    def __init__(self) -> None:
        self.summary = "условие и код"
        self._tail = ""
        self._last_update = float("-inf")

    def update(self, delta: dict[str, Any]) -> bool:
        fragment = next(
            (
                delta[key]
                for key in ("reasoning", "reasoning_content")
                if isinstance(delta.get(key), str) and delta[key]
            ),
            "",
        )
        if not fragment:
            details = delta.get("reasoning_details")
            if isinstance(details, list):
                for detail in details:
                    if not isinstance(detail, dict):
                        continue
                    key = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(
                        detail.get("type") if isinstance(detail.get("type"), str) else ""
                    )
                    value = detail.get(key) if key else None
                    if isinstance(value, str):
                        fragment = (fragment + value[-512:])[-512:]
        if not fragment:
            return False
        self._tail = (self._tail + fragment[-512:])[-512:]
        matches = [
            (match.start(), label)
            for pattern, label in _PATTERNS
            for match in pattern.finditer(self._tail)
        ]
        if not matches:
            return False
        topic = max(matches, key=lambda item: item[0])[1]
        now = time.monotonic()
        if topic == self.summary or now - self._last_update < 2:
            return False
        self.summary = topic
        self._last_update = now
        return True
