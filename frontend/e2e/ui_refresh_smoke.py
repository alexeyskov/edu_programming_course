"""Visual and workflow smoke against local demo data only.

Start Vite with VITE_DEMO_MODE=always VITE_API_BASE=/__ui-audit-api on port 5198.
Run: python frontend/e2e/ui_refresh_smoke.py --output /tmp/your-screenshot-folder
Requires Playwright Chromium. No Moodle, model, or deployed application is used.
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def main(output):
    screenshots = Path(output)
    screenshots.mkdir(parents=True, exist_ok=True)
    errors, layouts = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1366, "height": 768})
        context.set_default_timeout(8000)
        await context.route(
            "**/__ui-audit-api/**",
            lambda route: route.fulfill(status=503, json={"detail": "offline UI test"}),
        )
        await context.route("https://**", lambda route: route.abort())

        async def new_page(role="Студент", admin=False, mobile=False):
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(str(error)))
            if mobile:
                await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto("http://127.0.0.1:5198/login")
            await page.get_by_role("button", name="Открыть демо", exact=True).wait_for()
            await page.get_by_label("Логин Moodle", exact=True).wait_for()
            if admin:
                await page.locator(".login-additional > summary").click()
                await page.get_by_label("Токен администратора", exact=True).fill(
                    "ui-demo-only-token"
                )
            if role == "Преподаватель":
                await page.get_by_role("button", name=role, exact=True).click()
            await page.get_by_role("button", name="Открыть демо", exact=True).click()
            await page.locator(".page-heading").first.wait_for()
            return page

        async def go(page, path, selector):
            await page.evaluate(
                "path => { history.pushState(null, '', path); dispatchEvent(new PopStateEvent('popstate')); window.scrollTo(0, 0); }",
                path,
            )
            await page.locator(selector).first.wait_for()
            await page.wait_for_timeout(300)

        async def capture(page, name, full=False):
            if await page.locator(
                ".sidebar:not(.is-open)"
            ).count() and await page.evaluate("innerWidth <= 900"):
                await page.wait_for_function(
                    "document.querySelector('.sidebar').getBoundingClientRect().right <= 1"
                )
            dimensions = await page.evaluate(
                "({viewport:innerWidth, width:document.documentElement.scrollWidth, height:innerHeight})"
            )
            layouts.append({"screen": name, **dimensions})
            await page.screenshot(
                path=str(screenshots / f"{name}.png"),
                full_page=full,
                animations="disabled",
            )
            print(f"checked {name}: {dimensions}", flush=True)
            assert dimensions["width"] <= dimensions["viewport"] + 1, (name, dimensions)

        teacher = await new_page("Преподаватель")
        await capture(teacher, "teacher-dashboard")
        await go(teacher, "/courses", ".course-detail")
        await teacher.get_by_role(
            "button",
            name="Развернуть список работ курса Основы программирования C/C++",
            exact=True,
        ).click()
        await capture(teacher, "courses")
        await go(teacher, "/submissions", ".submission-row")
        await teacher.get_by_label("Группа", exact=True).select_option("1.1")
        assert await teacher.locator(".submission-row").count() == 2
        await teacher.get_by_label("Сортировка работ", exact=True).select_option(
            "oldest"
        )
        assert "Софья" in await teacher.locator(".submission-row").first.inner_text()
        await capture(teacher, "submissions")
        active_tab = teacher.locator(".submission-view-tabs button.is-active")
        assert await active_tab.evaluate(
            "e => { const s = getComputedStyle(e); return s.boxShadow === 'none' && s.backgroundImage === 'none' && s.borderTopColor === s.borderBottomColor && s.borderTopWidth === s.borderBottomWidth; }"
        )
        await teacher.get_by_role("tab", name="Ожидают проверки", exact=False).hover()
        await capture(teacher, "submissions-hover")
        await teacher.get_by_role(
            "button", name="Включить тёмную тему", exact=True
        ).click()
        await capture(teacher, "submissions-dark")
        assert await active_tab.evaluate("e => getComputedStyle(e).boxShadow") == "none"
        assert await active_tab.evaluate("e => getComputedStyle(e).backgroundImage") == "none"
        await teacher.set_viewport_size({"width": 390, "height": 844})
        await capture(teacher, "submissions-mobile-dark")
        await teacher.set_viewport_size({"width": 1366, "height": 768})
        await teacher.get_by_role(
            "button", name="Включить светлую тему", exact=True
        ).click()
        await go(teacher, "/review/sub-1", ".review-layout")
        await teacher.locator(".monaco-editor").first.wait_for()
        await capture(teacher, "review")
        splitter = teacher.get_by_role(
            "separator", name="Ширина панели проверки", exact=True
        )
        before = int(await splitter.get_attribute("aria-valuenow"))
        await splitter.focus()
        await teacher.keyboard.press("ArrowLeft")
        assert int(await splitter.get_attribute("aria-valuenow")) == before + 10
        await teacher.wait_for_function(
            "value => localStorage.getItem('eduprog.layout.review-width') === String(value)",
            arg=before + 10,
        )
        await teacher.get_by_label("Итоговый балл", exact=False).fill("8")
        await teacher.get_by_label("Комментарий студенту", exact=True).fill(
            "Комментарий для проверки интерфейса"
        )
        await teacher.get_by_role("tab", name="ИИ", exact=True).click()
        await teacher.wait_for_function(
            "document.querySelector('.grading-panel').hidden"
        )
        assert not await teacher.get_by_role(
            "button", name="Оценка и комментарий", exact=True
        ).is_visible()
        await capture(teacher, "review-ai")
        await teacher.get_by_role("button", name="Развернуть чат", exact=True).click()
        await capture(teacher, "review-ai-expanded")
        assert (
            await teacher.locator(".teacher-chat form textarea").evaluate(
                "e => getComputedStyle(e).fontSize"
            )
            == "16px"
        )
        await teacher.get_by_role("button", name="Свернуть чат", exact=True).click()
        await teacher.get_by_role("tab", name="Задание", exact=True).click()
        assert (
            await teacher.get_by_label("Итоговый балл", exact=False).input_value()
            == "8"
        )
        await teacher.get_by_role("button", name="Утвердить", exact=True).first.click()
        modal = teacher.get_by_role("dialog", name="Утвердить решение", exact=True)
        await modal.wait_for()
        assert await teacher.evaluate(
            "Boolean(document.activeElement.closest('[role=dialog]'))"
        )
        await teacher.keyboard.press("Tab")
        assert await teacher.evaluate(
            "Boolean(document.activeElement.closest('[role=dialog]'))"
        )
        await teacher.keyboard.press("Escape")
        assert await teacher.get_by_role("dialog").count() == 0
        await capture(teacher, "review-after-dialog")
        assert (
            await teacher.get_by_label("Комментарий студенту", exact=True).input_value()
            == "Комментарий для проверки интерфейса"
        )
        await teacher.get_by_role(
            "button", name="Включить тёмную тему", exact=True
        ).click()
        await capture(teacher, "review-dark")
        await teacher.get_by_role(
            "button", name="Включить светлую тему", exact=True
        ).click()
        for width, height in ((1920, 1080), (1024, 768), (768, 1024)):
            await teacher.set_viewport_size({"width": width, "height": height})
            await capture(teacher, f"review-{width}")

        student = await new_page()
        await capture(student, "student-dashboard")
        assert (
            await student.locator(".continue-card h2").evaluate(
                "e => getComputedStyle(e).color"
            )
            != "rgb(243, 243, 243)"
        )
        await go(student, "/ide/attempt-demo", ".monaco-editor")
        await capture(student, "student-ide")
        await student.evaluate(
            "async () => { window.testMonaco = (await import('/src/lib/monacoSetup.ts')).monaco; }"
        )
        original = await student.evaluate(
            "window.testMonaco.editor.getEditors()[0].getValue()"
        )
        original += "\n// Проверка сохранности черновика при смене размера\n"
        await student.evaluate(
            "value => { const editor = window.testMonaco.editor.getEditors()[0]; editor.executeEdits('ui-smoke', [{range: editor.getModel().getFullModelRange(), text: value}]); }",
            original,
        )
        await student.set_viewport_size({"width": 390, "height": 844})
        await student.locator(".mobile-code-preview").wait_for()
        assert await student.locator(".ide-editor textarea, .ide-editor [contenteditable]").count() == 0
        await capture(student, "student-mobile-code")
        await student.get_by_role("button", name="Условие", exact=True).click()
        await student.locator(".condition-statement").wait_for(state="visible")
        await capture(student, "student-mobile-task")
        await student.get_by_role(
            "button", name="Запуски и история", exact=True
        ).click()
        await student.get_by_role("tab", name="История", exact=False).click()
        await capture(student, "student-mobile-history")
        await student.get_by_role("button", name="Код", exact=True).click()
        await student.get_by_label("Файл для просмотра", exact=True).select_option(
            "file-header"
        )
        await capture(student, "student-mobile-files")
        await student.get_by_label("Файл для просмотра", exact=True).select_option(
            "file-main"
        )
        await student.set_viewport_size({"width": 1366, "height": 768})
        await student.wait_for_function(
            "window.testMonaco.editor.getEditors()[0] && !window.testMonaco.editor.getEditors()[0].getOption(window.testMonaco.editor.EditorOption.readOnly)"
        )
        assert (
            await student.evaluate(
                "window.testMonaco.editor.getEditors()[0].getValue()"
            )
            == original
        )

        mobile_teacher = await new_page("Преподаватель", mobile=True)
        await capture(mobile_teacher, "teacher-mobile-dashboard")
        await go(mobile_teacher, "/submissions", ".submission-row")
        await capture(mobile_teacher, "teacher-mobile-submissions")
        await go(mobile_teacher, "/review/sub-1", ".review-layout")
        await capture(mobile_teacher, "teacher-mobile-code")
        assert (
            await mobile_teacher.evaluate(
                "async () => (await (await import('/src/lib/api.ts')).api.getSubmission('sub-1')).status"
            )
            == "UNGRADED"
        )
        await mobile_teacher.get_by_role("button", name="Задание", exact=True).click()
        await capture(mobile_teacher, "teacher-mobile-task")
        assert await mobile_teacher.get_by_role(
            "button", name="Оценка", exact=True
        ).count() == 0
        await mobile_teacher.locator(".grading-panel").scroll_into_view_if_needed()
        assert await mobile_teacher.get_by_label(
            "Итоговый балл", exact=False
        ).is_disabled()
        assert (
            await mobile_teacher.get_by_role(
                "button", name="Утвердить", exact=True
            ).is_visible()
            is False
        )
        await capture(mobile_teacher, "teacher-mobile-grade")
        await mobile_teacher.get_by_role(
            "button", name="Вернуться к работам", exact=True
        ).click()
        await mobile_teacher.get_by_role(
            "button", name="Открыть меню", exact=True
        ).click()
        await mobile_teacher.get_by_role(
            "link", name="Курсы и работы", exact=True
        ).click()
        await mobile_teacher.locator(".course-detail").first.wait_for()
        await capture(mobile_teacher, "teacher-mobile-courses")

        admin = await new_page("Преподаватель", admin=True)
        await capture(admin, "settings")
        await admin.set_viewport_size({"width": 390, "height": 844})
        await capture(admin, "settings-mobile")
        await admin.get_by_role("link", name="ИИ", exact=True).click()
        await capture(admin, "settings-mobile-ai")

        login = await browser.new_page(viewport={"width": 360, "height": 800})
        login.set_default_timeout(8000)
        await login.route(
            "**/__ui-audit-api/**",
            lambda route: route.fulfill(status=503, json={"detail": "offline UI test"}),
        )
        await login.route("https://**", lambda route: route.abort())
        await login.goto("http://127.0.0.1:5198/login")
        await login.get_by_label("Логин Moodle", exact=True).wait_for()
        await capture(login, "login-mobile")
        assert not await login.locator(".login-additional").evaluate("e => e.open")
        await login.get_by_role("button", name="Показать пароль", exact=True).click()
        assert (
            await login.get_by_label("Пароль Moodle", exact=True).get_attribute("type")
            == "text"
        )
        await login.locator(".login-additional > summary").click()
        await login.get_by_label("Токен преподавателя", exact=True).wait_for(
            state="visible"
        )
        await capture(login, "login-mobile-additional")
        assert not errors, errors
        print(
            json.dumps(
                {"ok": True, "screens": layouts, "page_errors": errors},
                ensure_ascii=False,
            )
        )
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    asyncio.run(main(parser.parse_args().output))
