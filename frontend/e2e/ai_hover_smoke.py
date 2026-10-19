"""Real Monaco hover/startup regressions; no backend, clangd or model requests.

Start Vite on port 5198, then run this script with Playwright Chromium installed.
"""

import asyncio
import json

from playwright.async_api import async_playwright, expect


async def main():
    errors, requests = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page(viewport={"width": 1366, "height": 900})
            page.on("pageerror", lambda error: errors.append(str(error)))

            async def block_api(route):
                requests.append(route.request.url)
                await route.abort()

            await page.route("**/api/v1/**", block_api)
            await page.route("**/__ui-audit-api/**", block_api)
            await page.route("https://**", lambda route: route.abort())
            await page.goto("http://127.0.0.1:5198/e2e/code-intelligence.html?ai-hover")
            await page.locator(".monaco-editor").first.wait_for()
            message = await page.get_by_test_id("ai-finding").inner_text()
            await page.wait_for_function(
                "window.smokeMonaco.editor.getModelMarkers({}).some(m => m.source === 'Ответ ИИ')"
            )
            # A language hover must still coexist with the single AI description.
            await page.evaluate(
                """() => window.smokeMonaco.languages.registerHoverProvider('cpp', {
                    provideHover: () => ({
                        range: new window.smokeMonaco.Range(4, 1, 4, 27),
                        contents: [{value: '`std::vector<int>`'}],
                    }),
                })"""
            )

            async def open_hover():
                await page.evaluate(
                    """async () => {
                        const editor = window.smokeMonaco.editor.getEditors()[0];
                        editor.focus(); editor.setPosition({lineNumber: 4, column: 5});
                        await editor.getAction('editor.action.showHover').run();
                    }"""
                )
                hover = page.locator(".monaco-hover:visible").first
                await expect(hover).to_contain_text(message)
                await expect(hover).to_contain_text("std::vector<int>")
                assert (await hover.inner_text()).count(message) == 1
                assert (await hover.inner_text()).count("Ответ ИИ") == 1
                # Model content is plain text, never executable Markdown or HTML.
                assert await hover.locator("script").count() == 0
                assert await hover.locator("a[href='command:forged']").count() == 0
                return hover

            hover = await open_hover()
            await expect(page.get_by_test_id("ai-asked")).to_have_text("0")
            await hover.get_by_role("link", name="Спросить у ИИ", exact=True).click()
            await expect(page.get_by_test_id("ai-asked")).to_have_text("1")
            for mode in ("Студент", "Песочница"):
                await page.keyboard.press("Escape")
                await page.get_by_role("button", name=mode, exact=True).click()
                hover = await open_hover()
                assert (
                    await hover.get_by_role(
                        "link", name="Спросить у ИИ", exact=True
                    ).count()
                    == 0
                )

            # Receive a saved report while the real editor is still loading.
            # The wrapper retains its initial onMount callback, exactly as in production.
            await page.goto(
                "http://127.0.0.1:5198/e2e/code-intelligence.html?ai-hover&defer-editor"
            )
            await page.wait_for_function("window.smokeEditorWaiting === true")
            await expect(page.locator(".monaco-editor")).to_have_count(0)
            await page.get_by_role(
                "button", name="Получить сохранённый отчёт", exact=True
            ).click()
            await expect(page.get_by_test_id("ai-finding-count")).to_have_text("1")
            await page.get_by_role(
                "button", name="Открыть редактор", exact=True
            ).click()
            await page.wait_for_function(
                "window.smokeMonaco.editor.getModelMarkers({}).some(m => m.source === 'Ответ ИИ')"
            )
            await expect(page.locator(".ai-review-line")).to_have_count(1)
            await expect(page.get_by_test_id("ai-asked")).to_have_text("0")
            assert not requests, requests
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "ok": True,
                        "ai_descriptions": 1,
                        "saved_report_applied_after_loading": True,
                        "api_requests": requests,
                    }
                )
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
