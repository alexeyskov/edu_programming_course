"""Local-only course comment and publication UI checks; no LMS/model calls.

Start Vite with VITE_DEMO_MODE=always VITE_API_BASE=/__ui-audit-api on port 5198.
"""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main(output):
    folder = Path(output)
    folder.mkdir(parents=True, exist_ok=True)
    errors, external, checked = [], [], []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            for role in ("Преподаватель", "Студент"):
                context = await browser.new_context(viewport={"width": 1366, "height": 900})

                async def block(route):
                    external.append(route.request.url)
                    await route.abort()

                await context.route("https://**", block)
                await context.route("**/__ui-audit-api/**", block)
                await context.route("**/api/v1/**", block)
                page = await context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.set_default_timeout(15000)
                await page.goto("http://127.0.0.1:5198/login")
                if role == "Преподаватель":
                    await page.locator(".login-additional > summary").click()
                    await page.get_by_label("Токен администратора", exact=True).fill("local-demo-only-token")
                    await page.get_by_role("button", name=role, exact=True).click()
                await page.get_by_role("button", name="Открыть демо", exact=True).click()
                await page.locator(".page-heading").first.wait_for()

                async def go(path, selector):
                    await page.evaluate("""path => {
                        history.pushState(null, '', path);
                        dispatchEvent(new PopStateEvent('popstate'));
                    }""", path)
                    await page.locator(selector).first.wait_for()

                async def capture(name, selector):
                    await page.locator(selector).first.scroll_into_view_if_needed()
                    await page.evaluate("document.fonts.ready")
                    await page.screenshot(path=str(folder / f"{name}.png"), animations="disabled")
                    assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), name
                    checked.append(name)

                if role == "Преподаватель":
                    await go("/settings", ".system-course-row")
                    await page.get_by_role("button", name="Изменить комментарий к курсу Основы программирования C/C++ (549)", exact=True).click()
                    modal = page.get_by_role("dialog", name="Комментарий к курсу", exact=True)
                    await modal.get_by_label("Комментарий к курсу", exact=True).fill("Осень 2026")
                    await capture("admin-comment-editor", "#course-comment-editor")
                    await modal.get_by_role("button", name="Сохранить", exact=True).click()
                    await expect(modal).to_have_count(0)
                    await expect(page.locator(".system-course-title .course-title__comment")).to_have_text(" Осень 2026")
                    await capture("admin-comment-saved", ".system-course-row")

                await page.evaluate("""async role => {
                    const {api} = await import('/src/lib/api.ts');
                    const getCourses = api.getCourses;
                    api.getCourses = async (...args) => (await getCourses(...args)).map((course, index, all) => ({
                        ...course, title: all[0].title,
                        comment: index ? 'Весна 2026' : (course.comment || 'Осень 2026'),
                    }));
                    const getAssessment = api.getAssessment;
                    let rules = [];
                    api.getAssessment = async (...args) => ({...await getAssessment(...args),
                        publicationStatus:'PUBLISHED', requiresLiveLmsPreparation:true,
                        policy:{moodle_metadata_read_only:true}, availabilityRules:rules,
                    });
                    api.getAssessmentPublicationTargets = async () => ({
                        groups:[{id:'test-group', externalId:'test-group-42', name:'2 — Тестовая группа'}],
                        principals:[], overridesConfirmed:true,
                    });
                    api.publishAssessment = async (id, groupIds) => {
                        rules = groupIds.map(groupId => ({id:groupId, targetType:'GROUP', targetExternalId:'test-group-42', allowed:true}));
                        return api.getAssessment(id);
                    };
                }""", role)
                await go("/courses", ".course-detail")
                await expect(page.locator(".course-detail h2 .course-title__comment").first).to_have_text(" Осень 2026")
                if role == "Преподаватель":
                    await expect(page.locator(".course-detail h2 .course-title__comment").nth(1)).to_have_text(" Весна 2026")
                    for season in ("Осень 2026", "Весна 2026"):
                        await page.get_by_role("button", name=f"Развернуть список работ курса Основы программирования C/C++ {season}", exact=True).click()
                    await expect(page.locator(".course-detail:not(.course-detail--collapsed)")).to_have_count(2)

                for name, width, dark in (("desktop", 1366, False), ("mobile", 390, False), ("mobile-dark", 390, True)):
                    await page.set_viewport_size({"width": width, "height": 900 if width > 767 else 844})
                    if dark:
                        await page.get_by_role("button", name="Включить тёмную тему", exact=True).click()
                    note = page.locator(".course-detail h2 .course-title__comment").first
                    assert await note.evaluate("""e => {
                        const a = getComputedStyle(e), b = getComputedStyle(e.parentElement);
                        return a.fontSize === b.fontSize && a.fontWeight === b.fontWeight && a.color !== b.color;
                    }""")
                    await capture(f"{'teacher' if role == 'Преподаватель' else 'student'}-courses-{name}", ".course-detail")

                if role == "Преподаватель":
                    await page.set_viewport_size({"width": 1366, "height": 900})
                    await page.get_by_role("button", name="Включить светлую тему", exact=True).click()
                    await go("/assessments/control-pointers", ".assessment-intro")
                    badge = page.locator(".intro-badges")
                    await expect(badge).to_contain_text("Не включена")
                    await capture("work-no-audience", ".assessment-intro")
                    for selected in (True, False):
                        await page.get_by_role("button", name="Настроить доступ" if selected else "Изменить доступ", exact=True).click()
                        modal = page.get_by_role("dialog", name="Доступ к работе", exact=True)
                        await modal.get_by_role("checkbox", name="2 — Тестовая группа", exact=False).set_checked(selected)
                        await modal.get_by_role("button", name="Сохранить доступ", exact=True).click()
                        await expect(modal).to_have_count(0)
                        await expect(badge).to_contain_text("Опубликована" if selected else "Не включена")
                        await capture("work-open" if selected else "work-access-revoked", ".assessment-intro")
                await context.close()
            assert not errors, errors
            assert not external, external
            print(json.dumps({"ok": True, "checked": checked, "external_requests": external,
                              "page_errors": errors, "screenshots": str(folder)}, ensure_ascii=False))
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/tmp/eduprog-course-catalog-publication")
    asyncio.run(main(parser.parse_args().output))
