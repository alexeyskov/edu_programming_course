"""Real Chromium clipboard regression against the development-only Vite fixture.

Start Vite, then run this script with --url http://127.0.0.1:5187.
No Moodle, credentials, or production APIs are used. Requires Playwright/Chromium.
--cdn additionally checks the Monaco version loaded by the production loader.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from playwright.async_api import async_playwright


async def check(base_url: str, cdn: bool) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                permissions=["clipboard-read", "clipboard-write"]
            )
            page = await context.new_page()
            errors: list[str] = []
            cancellations: list[str] = []

            def page_error(error):
                stack = error.stack or str(error)
                # Monaco cancels model-bound background work when switching files.
                # Only this named upstream cancellation is accepted; all other
                # page errors, and every document/history assertion, fail the test.
                if "Canceled: Canceled" in stack and "monaco-editor" in stack:
                    cancellations.append(stack)
                else:
                    errors.append(stack)

            page.on("pageerror", page_error)
            await page.goto(
                f"{base_url}/e2e/clipboard.html" + ("?cdn=1" if cdn else "")
            )
            await page.wait_for_function(
                "window.monaco?.editor.getEditors().length === 1", timeout=45_000
            )
            await page.wait_for_load_state("networkidle")
            modifier = "Meta" if sys.platform == "darwin" else "Control"
            editor = "window.monaco.editor.getEditors()[0]"

            async def files():
                return json.loads(await page.get_by_test_id("files").inner_text())

            async def focus_end():
                # @monaco-editor/react creates the model before revealing its
                # container and running onMount. Do not focus a hidden editor.
                await page.locator(".monaco-editor").wait_for(state="visible")
                await page.wait_for_function(
                    f"{editor}.trigger.name === 'guardedTrigger'"
                )
                await page.evaluate(f"""() => {{
                  const editor = {editor}; const model = editor.getModel();
                  editor.focus(); editor.setPosition(model.getPositionAt(model.getValueLength()));
                }}""")
                await page.wait_for_function(f"{editor}.hasTextFocus()")

            async def external_copy(text):
                field = page.get_by_label("External text")
                await field.fill(text)
                await field.press(f"{modifier}+A")
                await field.press(f"{modifier}+C")

            async def wait_content(file_id, content):
                try:
                    await page.wait_for_function(
                        """([id, text]) => JSON.parse(
                        document.querySelector('[data-testid=files]').textContent
                    ).find(file => file.id === id).content === text""",
                        arg=[file_id, content],
                    )
                except Exception:
                    print(
                        await page.evaluate("""() => ({
                        files: document.querySelector('[data-testid=files]').textContent,
                        edits: document.querySelector('[data-testid=edits]').textContent,
                        blocked: document.querySelector('[data-testid=blocked]').textContent,
                        receipts: document.querySelector('[data-testid=receipts]').textContent,
                        editors: window.monaco.editor.getEditors().map(editor => ({
                            uri: editor.getModel()?.uri.toString(),
                            content: editor.getValue(), focus: editor.hasTextFocus(),
                            trigger: editor.trigger.name, selection: editor.getSelection()
                        }))
                    })""")
                    )
                    raise

            initial = await files()
            await external_copy("AI_EXTERNAL_SOLUTION")
            await focus_end()
            await page.keyboard.press(f"{modifier}+V")
            await page.wait_for_function(
                "document.querySelector('[data-testid=blocked]').textContent === '1'"
            )
            assert await files() == initial

            await page.evaluate(f"""() => {{
              const editor = {editor}; editor.focus();
              editor.setSelection({{startLineNumber: 1, startColumn: 1, endLineNumber: 1, endColumn: 4}});
            }}""")
            await page.keyboard.press(f"{modifier}+C")
            await focus_end()
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("main", "int value = 42;\nint")
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("main", "int value = 42;\nintint")

            await page.locator(".file-entry__open").filter(has_text="other.cpp").click()
            await focus_end()
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("other", "// other\nint")
            state = await files()
            await external_copy("int")  # matching existing code is NOT sufficient
            await focus_end()
            await page.keyboard.press(f"{modifier}+V")
            await page.wait_for_function(
                "document.querySelector('[data-testid=blocked]').textContent === '2'"
            )
            assert await files() == state

            await page.evaluate(
                f"{editor}.trigger('keyboard', 'paste', {{text: 'EXTERNAL_COMMAND'}})"
            )
            assert await files() == state
            await page.keyboard.press(f"{modifier}+Z")
            await wait_content("other", "// other\n")

            # Cut waits for copy acknowledgement; paste must work with no source left.
            await page.evaluate(f"""() => {{
              const editor = {editor}; editor.focus(); editor.setSelection(editor.getModel().getFullModelRange());
            }}""")
            await page.keyboard.press(f"{modifier}+X")
            await wait_content("other", "")
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("other", "// other\n")
            await page.keyboard.type("x")
            await wait_content("other", "// other\nx")
            assert (
                json.loads(await page.get_by_test_id("edits").inner_text())[-1][
                    "source"
                ]
                == "typing"
            )

            async def select_last_character():
                await page.evaluate(f"""() => {{
                  const editor = {editor}; const model = editor.getModel();
                  const line = model.getLineCount(); const end = model.getLineMaxColumn(line);
                  editor.focus(); editor.setSelection({{
                    startLineNumber: line, startColumn: end - 1, endLineNumber: line, endColumn: end
                  }});
                }}""")

            async def switch_question(label, model_scope):
                await page.get_by_role("button", name=label, exact=True).click()
                await page.wait_for_function(
                    """scope => window.monaco.editor.getEditors().some(editor =>
                        editor.getModel()?.uri.path.startsWith('/' + scope + '/'))""",
                    arg=model_scope,
                )
                await focus_end()

            # Copy proof survives the actual Monaco/React remount. The original
            # cut fragment no longer exists in task 1, so returning there must
            # renew from the saved paste in task 2, not infer external provenance.
            await select_last_character()
            await page.keyboard.press(f"{modifier}+X")
            await wait_content("other", "// other\n")
            await switch_question("Задача 2", "question-2")
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("second", "// task two\nx")
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("second", "// task two\nxx")
            await switch_question("Задача 1", "question-1")
            await page.keyboard.press(f"{modifier}+V")
            await wait_content("other", "// other\nx")

            state = await files()
            await external_copy("x")
            await focus_end()
            await page.keyboard.press(f"{modifier}+V")
            await page.wait_for_function(
                "document.querySelector('[data-testid=blocked]').textContent === '4'"
            )
            assert await files() == state
            await select_last_character()
            await page.keyboard.press(f"{modifier}+C")
            await switch_question("Другая работа", "another-work")
            await page.keyboard.press(f"{modifier}+V")
            await page.wait_for_function(
                "document.querySelector('[data-testid=blocked]').textContent === '5'"
            )
            await wait_content("foreign", "// unrelated\n")
            edits = json.loads(await page.get_by_test_id("edits").inner_text())
            pasted = [edit for edit in edits if edit["source"] == "internal_paste"]
            assert len(pasted) == 7
            assert all(edit.get("receiptId") and edit.get("range") for edit in pasted)
            assert edits[-1]["source"] == "internal_paste"
            assert not errors, errors
            print(
                json.dumps(
                    {
                        "monaco": "loader CDN" if cdn else "installed package",
                        "status": "PASS",
                        "internal_pastes": len(pasted),
                        "blocked": await page.get_by_test_id("blocked").inner_text(),
                        "monaco_background_cancellations": len(cancellations),
                    }
                )
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:5187")
    parser.add_argument("--cdn", action="store_true")
    arguments = parser.parse_args()
    asyncio.run(check(arguments.url.rstrip("/"), arguments.cdn))
