"""Student code-selection/Markdown chat and stdin echo for both roles, demo only.

VITE_DEMO_MODE=always VITE_API_BASE=/__ui-audit-api npm run dev -- --host 127.0.0.1 --port 5199
No requests to deployed APIs, Moodle or models are permitted.
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main(base_url, output):
    screenshots = Path(output)
    screenshots.mkdir(parents=True, exist_ok=True)
    failures, blocked_requests, checked = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for role, path in (
                ("Студент", "/ide/attempt-demo"),
                ("Преподаватель", "/review/sub-1"),
            ):
                context = await browser.new_context(viewport={"width": 1440, "height": 1000})

                async def block(route):
                    blocked_requests.append(route.request.url)
                    await route.abort()

                await context.route("https://**", block)
                await context.route("**/__ui-audit-api/**", block)
                await context.route("**/api/v1/**", block)
                page = await context.new_page()
                page.set_default_timeout(15000)
                page.on("pageerror", lambda error: failures.append(str(error)))
                await page.goto(f"{base_url}/login")
                if role == "Преподаватель":
                    await page.get_by_role("button", name=role, exact=True).click()
                await page.get_by_role("button", name="Открыть демо", exact=True).click()
                await page.locator(".page-heading").first.wait_for()
                await page.evaluate("""async () => {
                    const {api} = await import('/src/lib/api.ts');
                    window.testMonaco = (await import('/src/lib/monacoSetup.ts')).monaco;
                    api.queryCodeIntelligence = async () => ({result:null, diagnostics:[]});
                    const getAttempt = api.getAttempt;
                    api.getAttempt = async (...args) => ({...await getAttempt(...args), aiEnabled:true});
                    api.getAttemptStatus = async id => ({id,status:'ACTIVE',checkpointStatus:'SYNCED',aiEnabled:true});
                    window.testChatRequests = [];
                    api.createStudentAiThread = async (attempt,course,revision) => {
                        window.testChatRequests.push({kind:'student-thread',attempt,course,revision});
                        return {id:'student-smoke-thread'};
                    };
                    api.sendAiMessageStream = async (id,content,context,options) => {
                        if(id!=='student-smoke-thread' || !Number.isInteger(context.revision) || context.teacherComment !== undefined) throw Error('Wrong student context');
                        window.testChatRequests.push({kind:'student-question',content,context});
                        options.onEvent({type:'thinking'});
                        await new Promise(resolve => setTimeout(resolve,25));
                        const answer = '**Проверьте границы**\\n\\n- Размер массива\\n- Последний индекс\\n\\n`size()` — число элементов.\\n\\n![tracking](https://tracker.invalid/pixel)';
                        options.onEvent({type:'delta',text:answer});
                        return {id:'student-smoke-reply',content:answer,citations:[]};
                    };
                    let run;
                    const start = async () => (run={sessionId:'stdin-smoke',status:'RUNNING',terminal:false,durationMs:1,stdout:'Число: ',stderr:'',outputTruncated:false,diagnostics:[],inputClosed:false});
                    const input = async (_id,_session,text) => (run={...run,status:'SUCCESS',terminal:true,stdout:run.stdout+'Результат: '+text+'\\n'});
                    api.startInteractiveAttempt = api.startInteractiveExperiment = start;
                    api.getInteractiveAttempt = api.getInteractiveExperiment = async () => run;
                    api.sendInteractiveAttemptInput = api.sendInteractiveInput = input;
                }""")
                await page.evaluate("""path => {
                    history.pushState(null,'',path); dispatchEvent(new PopStateEvent('popstate'));
                }""", path)
                await page.locator(".monaco-editor").first.wait_for()

                if role == "Студент":
                    await page.evaluate("""() => {
                        const editor=window.testMonaco.editor.getEditors()[0];
                        editor.setValue('#include <iostream>\\nint main() {\\n  int n = 0;\\n  std::cin >> n;\\n  std::cout << n;\\n}');
                        editor.focus();
                        editor.setSelection(new window.testMonaco.Selection(3,3,4,16));
                    }""")
                    await page.get_by_role("button", name="Спросить у ИИ", exact=True).click()
                    await expect(page.get_by_role("complementary", name="Прикреплённый код", exact=True)).to_be_visible()
                    assert await page.evaluate("window.testChatRequests.length") == 0
                    assert await page.evaluate("!window.testMonaco.editor.getEditors()[0].getOption(window.testMonaco.editor.EditorOption.readOnly)")
                    await page.get_by_role("textbox", name="Вопрос ИИ", exact=True).fill("Почему индекс выходит за границы?")
                    await page.get_by_role("button", name="Отправить", exact=True).click()
                    await expect(page.locator(".ai-markdown strong")).to_have_text("Проверьте границы")
                    await expect(page.locator(".ai-markdown li")).to_have_count(2)
                    await expect(page.locator(".ai-markdown code")).to_have_text("size()")
                    assert await page.locator(".ai-markdown img").count() == 0
                    assert await page.get_by_role("complementary", name="Прикреплённый код", exact=True).count() == 0
                    requests = await page.evaluate("window.testChatRequests")
                    assert len(requests) == 2 and "Выделенный фрагмент кода" in requests[1]["content"]
                    await page.screenshot(path=str(screenshots / "student-chat.png"))
                    await page.get_by_role("button", name="Развернуть чат", exact=True).click()
                    await expect(page.get_by_role("button", name="Свернуть чат", exact=True)).to_be_visible()
                    await page.screenshot(path=str(screenshots / "student-chat-expanded.png"))
                    await page.get_by_role("button", name="Свернуть чат", exact=True).click()
                    await page.set_viewport_size({"width": 390, "height": 844})
                    await expect(page.locator(".mobile-code-preview")).to_be_attached()
                    assert await page.locator(".ide-editor textarea, .ide-editor [contenteditable]").count() == 0
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    await page.screenshot(path=str(screenshots / "student-chat-mobile.png"))
                    await page.set_viewport_size({"width": 1440, "height": 1000})
                    await page.get_by_role("button", name="Закрыть помощник", exact=True).click()
                    await page.get_by_role("button", name="Запустить", exact=True).click()
                    output_selector = ".student-console__output"
                else:
                    assert await page.evaluate("window.testMonaco.editor.getEditors()[0].getOption(window.testMonaco.editor.EditorOption.readOnly)")
                    await page.get_by_role("button", name="Компилировать и запустить код", exact=True).click()
                    output_selector = ".experiment-console__output"

                await expect(page.locator(output_selector)).to_contain_text("Число:")
                await page.get_by_role("textbox", name="Ввод программы", exact=True).fill("42")
                await page.get_by_role("button", name="Передать строку программе", exact=True).click()
                output = page.locator(output_selector)
                await expect(output.locator(".is-input")).to_have_text("› 42")
                await expect(output).to_contain_text("Результат: 42")
                styles = await output.evaluate("""element => {
                    const input=getComputedStyle(element.querySelector('.is-input'));
                    const stdout=getComputedStyle(element.querySelector('.is-stdout'));
                    return {input:input.fontSize, stdout:stdout.fontSize, style:input.fontStyle,color:input.color};
                }""")
                assert styles["style"] == "italic", styles
                assert float(styles["input"][:-2]) < float(styles["stdout"][:-2]), styles
                assert styles["color"] == "rgb(130, 184, 146)", styles
                assert await output.locator("pre").all_text_contents() == ["Число: ", "› 42", "Результат: 42"]
                for dark in (False, True):
                    if dark:
                        await page.get_by_role("button", name="Включить тёмную тему", exact=True).click()
                    await page.screenshot(path=str(screenshots / f"{'student' if role == 'Студент' else 'teacher'}-console{'-dark' if dark else ''}.png"))
                checked.append({"role": role, "stdin_styles": styles})
                await context.close()
        finally:
            await browser.close()
    assert not failures, failures
    assert not blocked_requests, blocked_requests
    print(json.dumps({"ok": True, "checked": checked, "page_errors": failures, "external_requests": blocked_requests}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5199")
    parser.add_argument("--output", default="/tmp/eduprog-student-chat-console")
    args = parser.parse_args()
    asyncio.run(main(args.base_url, args.output))
