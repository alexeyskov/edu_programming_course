"""Real Monaco -> gateway schema -> native clangd completion regression.

Run Vite on port 5197, then python frontend/e2e/code_intelligence_completion_smoke.py.
Requires the backend/language-service Python dependencies, Playwright Chromium
and local clangd with C++ standard-library headers. Only the source fixture
below is used; the test never connects to a deployed backend or NUC.
"""

import asyncio
import hashlib
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path

from playwright.async_api import async_playwright
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "language_service/src")]

from app.schemas.code_intelligence import CodeIntelligenceQuery  # noqa: E402
from edu_language.config import Settings  # noqa: E402
from edu_language.models import Query  # noqa: E402
from edu_language.service import LanguageService  # noqa: E402


SOURCE = r"""#include <iostream>
#include <vector>
#include <string>
#include <map>
#include <algorithm>

int main() {
    // Входной набор слов
    std::vector<std::string> words = {"apple", "banana", "apple", "cherry", "banana", "apple"};

    // Контейнер для хранения количества вхождений (слово -> количество)
    std::map<std::string, int> word_counts;

    std::for_each(words.begin(), words.end(), [&word_counts](const std::string& word) {
        word_counts[word]++;
    });

    std::for_each(word_counts.begin(), word_counts.end(), [](const std::pair<std::string, int>& pair) {
        std::cout << pair.first << ": " << pair.second << "\n";
    });

    std::cout << 
    return 0;
}
"""


async def exercise(service):
    requests, rejected, errors, answers = [], [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1400, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def csrf(route):
                await route.fulfill(json={"csrf_token": "fixture-csrf"})

            async def query(route):
                body = route.request.post_data_json
                requests.append(body)
                assert route.request.headers.get("x-csrftoken") == "fixture-csrf"
                try:
                    validated = CodeIntelligenceQuery.model_validate(body)
                except ValidationError:
                    rejected.append(
                        {"method": body["method"], "context": body.get("context")}
                    )
                    await route.fulfill(status=422, json={"detail": "invalid query"})
                    return
                scope = validated.scope
                payload = validated.model_dump(exclude={"scope"}, exclude_none=True)
                result = await service.query(
                    Query(
                        **payload,
                        workspace_id=hashlib.sha256(
                            f"{scope.kind}:{scope.id}".encode()
                        ).hexdigest(),
                        build_profile="cpp-gcc-c++20-single",
                    )
                )
                if body["method"] == "completion":
                    answers.append(
                        [item.get("label") for item in result["result"]["items"][:10]]
                    )
                await route.fulfill(json=result)

            await page.route("**/api/v1/auth/csrf", csrf)
            await page.route("**/api/v1/code-intelligence/query", query)
            await page.goto("http://127.0.0.1:5197/e2e/code-intelligence.html")
            await page.wait_for_function(
                "window.smokeMonaco?.editor.getEditors().length === 1"
            )
            for mode in ("Студент", "Песочница"):
                await page.get_by_role("button", name=mode, exact=True).click()
                scope_kind = "attempt" if mode == "Студент" else "experiment"
                await page.wait_for_function(
                    "kind => window.smokeMonaco.editor.getEditors()[0].getModel().uri.path.includes('smoke-' + kind)",
                    arg=scope_kind,
                )
                await page.evaluate(
                    r"""source => {
                    const editor = window.smokeMonaco.editor.getEditors()[0];
                    editor.setValue(source);
                    const lines = source.split('\n');
                    const line = lines.findIndex(line => line === '    std::cout << ');
                    editor.setPosition({lineNumber: line + 1, column: lines[line].length + 1});
                    editor.focus();
                }""",
                    SOURCE,
                )
                await page.keyboard.type("word")
                widget = page.locator(".suggest-widget.visible")
                await widget.get_by_text("words", exact=True).first.wait_for(
                    timeout=8000
                )
                for _ in range(5):
                    if (
                        await widget.locator(
                            ".monaco-list-row.focused .label-name"
                        ).inner_text()
                    ).strip() == "words":
                        break
                    await page.keyboard.press("ArrowDown")
                else:
                    raise AssertionError(
                        "words could not be selected in the suggestion list"
                    )
                await page.keyboard.press("Enter")
                # No sleep/retyping after accepting the variable: the very next
                # keystroke must query members of its current unsaved value.
                await page.keyboard.type(".")
                try:
                    await widget.get_by_text(re.compile(r"^\s*begin\(")).first.wait_for(
                        timeout=8000
                    )
                except Exception:
                    print(
                        json.dumps(
                            {
                                "rejected": rejected,
                                "answers": answers,
                                "visible_suggestions": await widget.locator(
                                    ".label-name"
                                ).all_text_contents(),
                                "completion_contexts": [
                                    request.get("context")
                                    for request in requests
                                    if request["method"] == "completion"
                                ],
                            }
                        )
                    )
                    raise
                assert not rejected, rejected
                completions = [
                    request
                    for request in requests
                    if request["scope"]["kind"] == scope_kind
                    and request["method"] == "completion"
                ]
                assert completions[0]["context"]["triggerKind"] == 1
                members = next(
                    request
                    for request in completions
                    if request["context"].get("triggerCharacter") == "."
                )
                assert members["context"]["triggerKind"] == 2
                assert "std::cout << words." in members["files"][0]["content"]
                assert await page.get_by_test_id("source").inner_text() == "typing"
                await page.keyboard.press("Escape")
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "ok": True,
                        "scopes": ["attempt", "experiment"],
                        "rejected_requests": rejected,
                        "page_errors": errors,
                    }
                )
            )
        finally:
            await browser.close()


async def main():
    clangd = shutil.which("clangd")
    assert clangd, "clangd must be installed for this integration test"
    with tempfile.TemporaryDirectory(prefix="clangd-browser-completion-") as root:
        service = LanguageService(
            Settings(
                shared_secret=b"fixture-only-language-service-secret",
                work_root=Path(root),
                clangd=clangd,
                test_without_sandbox=True,
            )
        )
        await service.start()
        try:
            assert service.ready
            await exercise(service)
        finally:
            await service.close()


if __name__ == "__main__":
    asyncio.run(main())
