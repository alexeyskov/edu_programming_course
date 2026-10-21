"""Real Moodle formula, rendered locally for both roles; no LMS/model calls.

Start Vite: VITE_DEMO_MODE=always VITE_API_BASE=/__ui-audit-api npm run dev -- --port 5198
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect
from statement_images_smoke import png_fixture

TEX = r"\sum_{k=1}^{N}{ \frac{1}{k!} }"
FALLBACK_TEX = r"\sum_{k=1}^{N}{ \frac{1}{k!} }"
FORMULA_IMAGE = "https://edu.mmcs.sfedu.ru/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png"
PICTURE = "https://edu.mmcs.sfedu.ru/pluginfile.php/21/question/questiontext/55/1/diagram.png"
STATEMENT = (
    "Написать функцию, вычисляющую для заданного натурального числа N, "
    "cумму первых N слагаемых по следующей формуле: "
    rf"\({TEX}\) "
    "В основной программе вызвать функцию для разных значений N"
    f"\nФормула-картинка: ![Формула-картинка](<{FORMULA_IMAGE}>)"
    "\nФормула при недоступной картинке: !["
    + FALLBACK_TEX.replace("\\", "\\\\")
    + f"](<{FORMULA_IMAGE}?variant=unavailable>)"
    f"\nРисунок: ![Рисунок к заданию](<{PICTURE}>)"
)


async def main(output):
    screenshots = Path(output)
    screenshots.mkdir(parents=True, exist_ok=True)
    failures, requests, checked = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for role, path, panel in (
                ("Студент", "/ide/attempt-demo", ".condition-statement"),
                ("Преподаватель", "/review/sub-1", ".review-task"),
            ):
                context = await browser.new_context(viewport={"width": 1366, "height": 900})

                async def block(route):
                    requests.append(route.request.url)
                    await route.abort()

                await context.route("https://**", block)
                await context.route("**/__ui-audit-api/**", block)
                await context.route("**/api/v1/**", block)

                async def serve_image(route):
                    if "unavailable" in route.request.url:
                        await route.fulfill(status=503, content_type="application/json", body='{"detail":"Image unavailable"}')
                    else:
                        await route.fulfill(status=200, content_type="image/png", body=png_fixture())

                await context.route("**/statement-images/**", serve_image)
                page = await context.new_page()
                page.on("pageerror", lambda error: failures.append(str(error)))
                page.set_default_timeout(20000)
                await page.goto("http://127.0.0.1:5198/login")
                if role == "Преподаватель":
                    await page.get_by_role("button", name=role, exact=True).click()
                await page.get_by_role("button", name="Открыть демо", exact=True).click()
                await page.locator(".page-heading").first.wait_for()
                await page.evaluate("""async statement => {
                    const {api} = await import('/src/lib/api.ts');
                    // Formula layout does not need the optional clangd service.
                    api.queryCodeIntelligence = async () => ({result:null, diagnostics:[]});
                    const getAttempt = api.getAttempt, getSubmission = api.getSubmission;
                    api.getAttempt = async (...args) => ({...await getAttempt(...args), statement});
                    api.getSubmission = async (...args) => ({...await getSubmission(...args), taskStatement: statement});
                }""", STATEMENT)
                await page.evaluate("""path => {
                    history.pushState(null, '', path);
                    dispatchEvent(new PopStateEvent('popstate'));
                }""", path)
                for name, width, dark in (
                    ("desktop", 1366, False), ("mobile", 390, False),
                    ("narrow-mobile", 320, False), ("mobile-dark", 390, True),
                ):
                    await page.set_viewport_size({"width": width, "height": 900 if width > 767 else 844})
                    if width <= 767:
                        await page.get_by_role("button", name="Условие" if role == "Студент" else "Задание", exact=True).click()
                    if dark:
                        await page.get_by_role("button", name="Включить тёмную тему", exact=True).click()
                    condition = page.locator(panel)
                    formula = condition.locator(".task-statement__math--display").first
                    await expect(formula).to_be_visible()
                    await expect(formula).to_have_attribute("data-tex", TEX)
                    await expect(formula.locator("math mfrac")).to_have_count(1)
                    await expect(formula.locator("math munderover")).to_have_count(1)
                    await expect(condition).to_contain_text("В основной программе вызвать функцию")
                    for label in ("Формула-картинка", "Рисунок к заданию"):
                        image = condition.get_by_role("img", name=label, exact=True)
                        await image.scroll_into_view_if_needed()
                        await expect(image).to_have_js_property("naturalWidth", 1400)
                    fallback = condition.locator(".task-statement__math--display").nth(1)
                    await fallback.scroll_into_view_if_needed()
                    await expect(fallback).to_have_attribute("data-tex", FALLBACK_TEX)
                    await expect(fallback.locator("math mfrac")).to_have_count(1)
                    await expect(fallback.locator("math munderover")).to_have_count(1)
                    await expect(fallback.locator(".frac-line")).to_be_visible()
                    await expect(condition.locator(".task-statement__math-source, .task-statement__image-fallback")).to_have_count(0)
                    await formula.scroll_into_view_if_needed()
                    await page.evaluate("document.fonts.ready")
                    assert await formula.evaluate("e => e.clientHeight > 35 && e.clientWidth <= e.parentElement.clientWidth + 1")
                    assert await formula.locator(".frac-line").evaluate("e => e.getBoundingClientRect().width > 5")
                    assert await fallback.locator(".frac-line").evaluate("e => e.getBoundingClientRect().width > 5")
                    # Check actual line breaks around the equation, including
                    # old Moodle inline tokens, formula images and fallbacks.
                    for equation in (formula, condition.get_by_role("img", name="Формула-картинка", exact=True), fallback):
                        assert await equation.evaluate("""e => {
                            const edge = (node, last) => {
                                const text = node.firstChild;
                                const value = text.textContent;
                                const start = last ? value.trimEnd().length - 1 : value.search(/\\S/);
                                const range = document.createRange();
                                range.setStart(text, start);
                                range.setEnd(text, start + 1);
                                return range.getBoundingClientRect();
                            };
                            const before = edge(e.previousElementSibling, true);
                            const after = edge(e.nextElementSibling, false);
                            const box = e.getBoundingClientRect();
                            return getComputedStyle(e).display === 'block'
                                && before.bottom <= box.top + 1 && after.top >= box.bottom - 1;
                        }"""), f"{role}: {name}: formula must have text above and below, not beside it"
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                    filename = f"{'student' if role == 'Студент' else 'teacher'}-{name}"
                    await page.screenshot(path=str(screenshots / f"{filename}.png"))
                    checked.append(filename)
                await context.close()
            assert not failures, failures
            assert not requests, requests
            print(json.dumps({"ok": True, "checked": checked, "external_requests": requests,
                              "page_errors": failures, "screenshots": str(screenshots)}, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/eduprog-statement-math")
    asyncio.run(main(parser.parse_args().output))
