"""Mobile grading + non-input code smoke, with in-memory demo data only.

Start Vite with VITE_DEMO_MODE=always VITE_API_BASE=/__ui-audit-api on port 5198.
No backend, Moodle, language-server or paid AI requests are permitted.
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main(output: str):
    screenshots = Path(output)
    screenshots.mkdir(parents=True, exist_ok=True)
    errors, requests = [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 390, "height": 844},
                is_mobile=True,
                has_touch=True,
            )

            async def block_api(route):
                requests.append(route.request.url)
                await route.abort()

            await context.route("**/api/v1/**", block_api)
            await context.route("**/__ui-audit-api/**", block_api)
            await context.route("https://**", lambda route: route.abort())
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.set_default_timeout(12000)
            await page.goto("http://127.0.0.1:5198/login")
            await page.get_by_role("button", name="Преподаватель", exact=True).click()
            await page.get_by_role("button", name="Открыть демо", exact=True).click()
            await page.locator(".page-heading").first.wait_for()
            await page.evaluate("""async () => {
                const {api} = await import('/src/lib/api.ts');
                const submission = await api.getSubmission('sub-1');
                const finding = {path: submission.files[0].path, line: 2,
                    severity: 'warning', message: 'Проверьте возвращаемое значение.'};
                const fixture = window.mobileReviewFixture = {
                    starts: 0, claims: 0, saves: 0, exports: [],
                    source: submission.files[0].content,
                    report: {enabled: true, state: 'NOT_STARTED', summary: '', findings: []},
                };
                api.getAiCodeReview = async () => structuredClone(fixture.report);
                api.startAiCodeReview = async () => {
                    fixture.starts++;
                    fixture.report = {id: 'mobile-check', enabled: true, state: 'COMPLETED',
                        recommendationStatus: 'PENDING', recommendedGrade: 16,
                        recommendedComment: 'Пояснение от ИИ', summary: 'Разбор кода',
                        findings: [finding]};
                    return {...fixture.report, state: 'QUEUED', findings: []};
                };
                const claim = api.claimSubmission, save = api.saveReview, finalize = api.finalizeReview;
                api.claimSubmission = async (...args) => { fixture.claims++; return claim(...args); };
                api.saveReview = async (...args) => { fixture.saves++; return save(...args); };
                api.finalizeReview = async (...args) => {
                    fixture.exports.push(args);
                    return finalize(...args);
                };
                history.pushState(null, '', '/review/sub-1');
                dispatchEvent(new PopStateEvent('popstate'));
            }""")
            await page.locator(".mobile-code-preview").wait_for()
            await expect(
                page.locator(
                    ".review-editor textarea, .review-editor input, .review-editor [contenteditable]"
                )
            ).to_have_count(0)
            await expect(page.locator(".monaco-editor")).to_have_count(0)
            await page.locator('.mobile-code-preview [data-line="1"]').tap()
            assert await page.evaluate("document.activeElement?.tagName") not in (
                "INPUT",
                "TEXTAREA",
            )
            assert await page.evaluate("mobileReviewFixture.claims") == 0
            await page.get_by_role(
                "button", name="Проверить работу через ИИ", exact=True
            ).tap()
            await expect(
                page.get_by_role("button", name="ИИ-проверка выполняется")
            ).to_be_disabled()
            await expect(
                page.get_by_role("switch", name="Подсветка замечаний ИИ")
            ).to_be_enabled()
            assert await page.evaluate("mobileReviewFixture.starts") == 1
            await page.get_by_role(
                "button", name="Замечания ИИ, строка 2", exact=True
            ).tap()
            dialog = page.get_by_role("dialog", name="Ответ ИИ: строка 2")
            await expect(dialog).to_contain_text("Проверьте возвращаемое значение.")
            await expect(
                dialog.locator("input, textarea, [contenteditable]")
            ).to_have_count(0)
            await expect(
                dialog.get_by_role("link", name="Спросить у ИИ")
            ).to_have_count(0)
            await page.screenshot(path=str(screenshots / "mobile-ai-remark.png"))
            await dialog.get_by_role("button", name="Закрыть", exact=True).tap()
            await page.locator(".review-mobile-actions .ai-highlights-toggle").tap()
            await expect(
                page.get_by_role("switch", name="Подсветка замечаний ИИ")
            ).not_to_be_checked()
            await expect(
                page.get_by_role("button", name="Замечания ИИ, строка 2")
            ).to_have_count(0)
            await page.locator(".review-mobile-actions .ai-highlights-toggle").tap()
            await expect(
                page.get_by_role("switch", name="Подсветка замечаний ИИ")
            ).to_be_checked()
            await page.get_by_role("button", name="Задание", exact=True).tap()
            apply = page.get_by_role(
                "button", name="Поставить оценку и комментарий от ИИ", exact=True
            )
            await apply.scroll_into_view_if_needed()
            await expect(apply).to_be_enabled()
            await apply.tap()
            grade = page.get_by_label("Итоговый балл", exact=True)
            comment = page.get_by_label("Комментарий студенту", exact=True)
            await expect(grade).to_have_value("16")
            await expect(comment).to_have_value("Пояснение от ИИ")
            await expect(page.locator(".toast")).to_have_count(0)
            assert await page.evaluate("mobileReviewFixture.claims") == 1
            assert await page.evaluate("mobileReviewFixture.exports.length") == 0
            await grade.fill("17")
            await comment.fill("Пояснение с телефона")
            await comment.scroll_into_view_if_needed()
            await expect(comment).to_be_in_viewport()
            await page.screenshot(path=str(screenshots / "mobile-comment.png"))
            await page.get_by_role(
                "button", name="Сохранить черновик", exact=True
            ).tap()
            await page.wait_for_function("mobileReviewFixture.saves === 1")
            await expect(page.locator(".toast")).to_have_count(0)
            await page.screenshot(path=str(screenshots / "mobile-grade.png"))
            await page.get_by_role(
                "button", name="Включить тёмную тему", exact=True
            ).tap()
            await page.screenshot(path=str(screenshots / "mobile-grade-dark.png"))
            await page.get_by_role("button", name="Код", exact=True).tap()
            await expect(page.locator(".mobile-code-preview")).to_be_visible()
            assert await page.evaluate("document.activeElement?.tagName") not in (
                "INPUT",
                "TEXTAREA",
            )
            await expect(
                page.locator(
                    ".review-editor textarea, .review-editor [contenteditable]"
                )
            ).to_have_count(0)
            await page.screenshot(path=str(screenshots / "mobile-code-dark.png"))
            await page.get_by_role("button", name="Задание", exact=True).tap()
            await expect(grade).to_have_value("17")
            await (
                page.locator(".grading-panel")
                .get_by_role("button", name="Утвердить", exact=True)
                .tap()
            )
            assert await page.evaluate("mobileReviewFixture.exports.length") == 0
            await (
                page.get_by_role("dialog")
                .get_by_role("button", name="Утвердить и отправить", exact=True)
                .tap()
            )
            await page.wait_for_url("**/submissions")
            assert await page.evaluate("mobileReviewFixture.exports") == [
                [
                    "sub-1",
                    17,
                    "Пояснение с телефона",
                    "mobile-check",
                ]
            ]
            assert await page.evaluate("mobileReviewFixture.starts") == 1
            assert await page.evaluate(
                "async () => (await (await import('/src/lib/api.ts')).api.getSubmission('sub-1')).files[0].content === mobileReviewFixture.source"
            )
            assert await page.evaluate(
                "document.documentElement.scrollWidth <= innerWidth + 1"
            )
            assert not errors, errors
            assert not requests, requests
            print(
                json.dumps(
                    {
                        "ok": True,
                        "screenshots": str(screenshots),
                        "errors": errors,
                        "api_requests": requests,
                    },
                    ensure_ascii=False,
                )
            )
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/eduprog-mobile-review")
    asyncio.run(main(parser.parse_args().output))
