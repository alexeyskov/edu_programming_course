"""Actual Monaco/browser smoke with a deterministic, CSRF-protected gateway fixture.

Run Vite on port 5197, then python frontend/e2e/code_intelligence_smoke.py.
This tests the browser bridge, not the separate real-clangd service integration.
"""
import asyncio
import json

from playwright.async_api import async_playwright


async def main():
    requests = []
    errors = []
    external = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1400, "height": 900})
        page.on("pageerror", lambda error: errors.append({"message": str(error), "stack": error.stack, "after_method": requests[-1]["method"] if requests else None}))
        page.on("request", lambda request: external.append(request.url) if request.url.startswith("https://") else None)

        async def csrf(route):
            await route.fulfill(json={"csrf_token": "fixture-csrf"})

        async def query(route):
            body = route.request.post_data_json
            assert route.request.headers.get("x-csrftoken") == "fixture-csrf"
            requests.append(body)
            method = body["method"]
            result = None
            if method == "completion":
                # Match the gateway's LSP contract, not Monaco's zero-based enum.
                assert body["context"]["triggerKind"] in (1, 2, 3)
                if body["context"].get("triggerCharacter"):
                    assert body["context"]["triggerKind"] == 2
                result = {"items": [{"label": "push_back", "kind": 2, "insertTextFormat": 2, "detail": "void push_back(int value)", "textEdit": {
                    "range": {"start": {"line": 4, "character": 9}, "end": {"line": 4, "character": 9}}, "newText": "push_back(${1:1})$0"}}]}
            elif method == "definition":
                result = [{"uri": "thing.h", "range": {"start": {"line": 0, "character": 11}, "end": {"line": 0, "character": 17}}}]
            elif method == "formatting":
                result = [{"range": {"start": {"line": 0, "character": 0}, "end": {"line": 1, "character": 0}}, "newText": "inline int helper() {\n  return 42;\n}\n"}]
            elif method == "hover":
                result = {"contents": {"kind": "markdown", "value": "**function helper**\n\nReturns an integer."}}
            await route.fulfill(json={"result": result, "diagnostics": []})

        await page.route("**/api/v1/auth/csrf", csrf)
        await page.route("**/api/v1/code-intelligence/query", query)
        await page.goto("http://127.0.0.1:5197/e2e/code-intelligence.html")
        await page.wait_for_function("window.smokeMonaco?.editor.getEditors().length === 1")
        await page.evaluate("""() => { const editor = window.smokeMonaco.editor.getEditors()[0]; editor.setPosition({lineNumber: 5, column: 10}); editor.focus(); editor.trigger('smoke', 'editor.action.triggerSuggest', {}); }""")
        await page.locator(".suggest-widget.visible").wait_for()
        await page.get_by_text("push_back", exact=True).first.wait_for()
        await page.keyboard.press("Enter")
        await page.wait_for_function("window.smokeMonaco.editor.getEditors()[0].getValue().includes('values.push_back(1)')")
        assert await page.get_by_test_id("source").inner_text() == "typing"
        changes_after_completion = int(await page.get_by_test_id("changes").inner_text())
        assert changes_after_completion > 0

        await page.evaluate("""() => { const editor = window.smokeMonaco.editor.getEditors()[0]; editor.setPosition({lineNumber: 6, column: 13}); editor.trigger('smoke', 'editor.action.revealDefinition', {}); }""")
        await page.wait_for_function("window.smokeMonaco.editor.getEditors()[0].getModel().uri.path.endsWith('/thing.h')")
        await page.get_by_role("button", name="Форматировать код", exact=True).click()
        await page.wait_for_function("window.smokeMonaco.editor.getEditors()[0].getValue().includes('\\n  return 42;')")
        assert int(await page.get_by_test_id("changes").inner_text()) > changes_after_completion
        assert await page.get_by_test_id("source").inner_text() == "typing"

        await page.get_by_role("button", name="Проверка", exact=True).click()
        await page.wait_for_function("window.smokeMonaco.editor.getEditors()[0].getOption(window.smokeMonaco.editor.EditorOption.readOnly) === true")
        assert await page.get_by_role("button", name="Форматировать код", exact=True).count() == 0
        await page.evaluate("""() => { const editor = window.smokeMonaco.editor.getEditors()[0]; editor.setPosition({lineNumber: 1, column: 14}); editor.focus(); editor.trigger('smoke', 'editor.action.showHover', {}); }""")
        await page.get_by_text("Returns an integer.").first.wait_for()
        submission_requests = [request for request in requests if request["scope"]["kind"] == "submission"]
        assert submission_requests and all("files" not in request for request in submission_requests)
        assert all(request["method"] != "formatting" for request in submission_requests)
        assert not external, external
        assert not errors, errors
        print(json.dumps({"ok": True, "methods": sorted(set(request["method"] for request in requests)), "editor_changes": int(await page.get_by_test_id("changes").inner_text()), "external_requests": external, "page_errors": errors}))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
