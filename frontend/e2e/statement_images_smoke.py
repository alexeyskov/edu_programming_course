"""Task image rendering on student/teacher desktop and mobile, demo data only.

Start Vite with VITE_DEMO_MODE=always VITE_API_BASE=/__ui-audit-api on port 5198.
No backend, Moodle, language-server or paid model calls are permitted.
"""

import argparse
import asyncio
import json
import struct
import zlib
from pathlib import Path

from playwright.async_api import async_playwright, expect

IMAGE_URL = "https://edu.mmcs.sfedu.ru/filter/tex/pix.php/94e2a3db3e7df256861affa0e53fd454.png"
FORMULA = r"\sum_{k=1}^{N}\frac{1}{k\sqrt{k}}"
ESCAPED_FORMULA = FORMULA.replace("\\", "\\\\")
STATEMENT = (
    "Написать функцию, вычисляющую для натурального N сумму:\n"
    f"![{ESCAPED_FORMULA}](<{IMAGE_URL}>)\n"
    "В основной программе вызвать функцию для разных значений N.\n"
    "std::vector<int> остаётся обычным текстом."
)


def png_fixture():
    """Oversized local raster proves scaling rather than contacting real Moodle."""

    def chunk(kind, value):
        return (
            struct.pack("!I", len(value))
            + kind
            + value
            + struct.pack("!I", zlib.crc32(kind + value) & 0xFFFFFFFF)
        )

    rows = []
    for y in range(150):
        row = bytearray(b"\0")
        for x in range(1400):
            black = 30 < x < 1370 and (y in range(72, 76) or x % 170 in range(20, 24))
            row.extend(b"\0\0\0" if black else b"\xff\xff\xff")
        rows.append(row)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!IIBBBBB", 1400, 150, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + chunk(b"IEND", b"")
    )


async def main(output):
    screenshots = Path(output)
    screenshots.mkdir(parents=True, exist_ok=True)
    errors, external, images, checked = [], [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(viewport={"width": 1366, "height": 900})

            async def block(route):
                external.append(route.request.url)
                await route.abort()

            async def image(route):
                images.append(route.request.url)
                await route.fulfill(status=200, content_type="image/png", body=png_fixture())

            await context.route("**/api/v1/**", block)
            await context.route("**/__ui-audit-api/**", block)
            await context.route("https://**", block)
            await context.route("**/statement-images/**", image)

            async def login(role):
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.set_default_timeout(12000)
                await page.goto("http://127.0.0.1:5198/login")
                if role == "Преподаватель":
                    await page.get_by_role("button", name=role, exact=True).click()
                await page.get_by_role("button", name="Открыть демо", exact=True).click()
                await page.locator(".page-heading").first.wait_for()
                await page.evaluate(
                    """async statement => {
                        const {api} = await import('/src/lib/api.ts');
                        window.imageFixture = {historyRequests: 0};
                        const getAttempt = api.getAttempt;
                        const getAssessment = api.getAssessment;
                        const getSubmission = api.getSubmission;
                        api.getAttempt = async (...args) => ({...await getAttempt(...args), statement});
                        api.getAssessment = async (...args) => ({...await getAssessment(...args), summary: statement});
                        api.getSubmission = async (...args) => ({...await getSubmission(...args), taskStatement: statement});
                        api.getHistory = async () => { imageFixture.historyRequests++; return []; };
                    }""",
                    STATEMENT,
                )
                return page

            async def go(page, path):
                await page.evaluate(
                    "path => { history.pushState(null, '', path); dispatchEvent(new PopStateEvent('popstate')); }",
                    path,
                )

            async def capture(page, name, selector):
                image = page.locator(selector).get_by_role("img", name=FORMULA)
                await image.scroll_into_view_if_needed()
                await expect(image).to_be_visible()
                await expect(image).to_have_js_property("naturalWidth", 1400)
                assert "/statement-images/" in await image.get_attribute("src")
                assert await image.evaluate(
                    "e => e.getBoundingClientRect().width <= e.parentElement.getBoundingClientRect().width + 1"
                )
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
                assert await image.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(255, 255, 255)"
                await page.screenshot(path=str(screenshots / f"{name}.png"))
                checked.append(name)

            teacher = await login("Преподаватель")
            await go(teacher, "/review/sub-1")
            await capture(teacher, "teacher-task-desktop", ".review-task")
            await teacher.get_by_role("tab", name="История", exact=True).click()
            await expect(teacher.get_by_role("button", name="История написания", exact=False)).to_be_visible()
            await teacher.get_by_role("tab", name="Задание", exact=True).click()
            await teacher.set_viewport_size({"width": 390, "height": 844})
            await teacher.get_by_role("button", name="Задание", exact=True).click()
            await capture(teacher, "teacher-task-mobile", ".review-task")
            await teacher.get_by_role("button", name="Включить тёмную тему", exact=True).click()
            await capture(teacher, "teacher-task-mobile-dark", ".review-task")
            await teacher.get_by_role("button", name="Код", exact=True).click()
            await expect(teacher.locator(".mobile-code-preview")).to_be_visible()
            await expect(teacher.locator(".review-editor textarea, .review-editor input, .review-editor [contenteditable]")).to_have_count(0)

            student = await login("Студент")
            await go(student, "/assessments/lab-vectors")
            await capture(student, "student-task-intro", ".assessment-intro")
            await go(student, "/ide/attempt-demo")
            await capture(student, "student-task-desktop", ".condition-statement")
            await expect(student.get_by_role("tab", name="История", exact=False)).to_have_count(0)
            await student.set_viewport_size({"width": 390, "height": 844})
            await student.get_by_role("button", name="Условие", exact=True).click()
            await capture(student, "student-task-mobile", ".condition-statement")
            await student.get_by_role("button", name="Консоль", exact=True).click()
            await expect(student.get_by_role("tab", name="Консоль", exact=True)).to_be_visible()
            await expect(student.get_by_role("tab", name="История", exact=False)).to_have_count(0)
            assert await student.evaluate("imageFixture.historyRequests") == 0
            assert not external, external
            assert not errors, errors
            print(json.dumps({"ok": True, "screenshots": str(screenshots), "checked": checked, "proxy_images": len(images), "external_requests": external, "page_errors": errors}, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/eduprog-statement-images")
    asyncio.run(main(parser.parse_args().output))
