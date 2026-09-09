#!/usr/bin/env python3
"""Inspect read-only Moodle authoring/review markup using an operator-owned profile.

The report is intentionally written to a private temporary file.  The script
does not submit forms and does not persist cookies or credentials in the repo.
"""

from __future__ import annotations

import asyncio
import argparse
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import Locator, Page, async_playwright


BASE_URL = "https://edu.mmcs.sfedu.ru"
COURSE_ID = "549"
PROFILE_DIR = "/private/tmp/edu-course-moodle-inspection-profile"
OUTPUT_PATH = "/private/tmp/edu-course-moodle-authoring.json"


async def _text(locator: Locator, limit: int = 2_000) -> str:
    try:
        return (await locator.inner_text(timeout=2_000))[:limit]
    except Exception:
        return ""


async def _control_schema(page: Page) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for form in (await page.locator("form").all())[:50]:
        controls: list[dict[str, object]] = []
        for control in (await form.locator("input, select, textarea, button").all())[:500]:
            tag = await control.evaluate("node => node.tagName.toLowerCase()")
            name = (await control.get_attribute("name") or "")[:200]
            item: dict[str, object] = {
                "tag": tag,
                "id": (await control.get_attribute("id") or "")[:160],
                "name": name,
                "type": (await control.get_attribute("type") or "")[:80],
                "checked": await control.is_checked() if tag == "input" and (await control.get_attribute("type")) in {"checkbox", "radio"} else False,
            }
            if tag == "select":
                item["selected"] = (await control.input_value())[:200]
                item["option_count"] = await control.locator("option").count()
            elif name in {
                "course",
                "coursemodule",
                "update",
                "instance",
                "modulename",
                "grade",
                "attempts",
                "userid",
                "timelimit[number]",
                "timelimit[timeunit]",
            } or any(name.startswith(prefix) for prefix in ("timeopen[", "timeclose[", "duedate[", "cutoffdate[", "allowsubmissionsfromdate[")):
                item["value"] = (await control.input_value())[:200]
            controls.append(item)
        result.append(
            {
                "id": (await form.get_attribute("id") or "")[:160],
                "action_path": urlsplit(await form.get_attribute("action") or page.url).path,
                "controls": controls,
            }
        )
    return result


async def _page_report(page: Page, name: str, url: str) -> dict[str, object]:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    parsed = urlsplit(page.url)
    selectors = (
        "#intro",
        ".activity-description",
        "textarea[name='introeditor[text]']",
        "textarea[name='intro[text]']",
        ".que.essay",
        ".qtext",
        ".qtype_essay_response",
        ".comment.clearfix",
        ".comment.clearfix p",
        "a[href*='/mod/quiz/comment.php']",
        "a[href*='/mod/quiz/reviewquestion.php']",
        "input[name$='-mark']",
        "div.editor_atto_content",
        "textarea[name$='[comment]']",
        "textarea[name*='comment']",
        "select[name='group']",
    )
    report: dict[str, object] = {
        "name": name,
        "status": response.status if response else None,
        "path": parsed.path,
        "query_keys": sorted(parse_qs(parsed.query)),
        "title": (await page.title())[:500],
        "body_id": await page.locator("body").get_attribute("id") or "",
        "body_class": await page.locator("body").get_attribute("class") or "",
        "selector_counts": {selector: await page.locator(selector).count() for selector in selectors},
        "forms": await _control_schema(page),
    }
    intro = page.locator("textarea[name='introeditor[text]'], textarea[name='intro[text]']").first
    if await intro.count():
        report["intro_editor"] = {
            "text": (await intro.input_value())[:20_000],
            "format": (
                await page.locator("input[name='introeditor[format]'], input[name='intro[format]']")
                .first.input_value()
                if await page.locator("input[name='introeditor[format]'], input[name='intro[format]']").count()
                else ""
            ),
        }
    name_control = page.locator("input[name='name']").first
    if await name_control.count():
        report["activity_name"] = (await name_control.input_value())[:1_000]
    qtext = page.locator(".que.essay .qtext").first
    if await qtext.count():
        report["question_text"] = {
            "inner_text": (await qtext.inner_text())[:8_000],
            "inner_html": (await qtext.inner_html())[:16_000],
        }
    group = page.locator("select[name='group']").first
    if await group.count():
        report["groups"] = [
            {
                "value": (await option.get_attribute("value") or "")[:100],
                "text": " ".join((await option.inner_text()).split())[:300],
            }
            for option in (await group.locator("option").all())[:200]
        ]
    response_node = page.locator(
        ".que.essay .qtype_essay_response, .que.essay .answer, .que.essay .formulation .answer"
    ).first
    if await response_node.count():
        report["essay_response"] = {
            "inner_text": (await response_node.inner_text())[:8_000],
            "text_content": ((await response_node.text_content()) or "")[:8_000],
            "inner_html": (await response_node.inner_html())[:16_000],
        }
    comment = page.locator(".que.essay .comment.clearfix").first
    if await comment.count():
        report["comment"] = {
            "inner_text": (await comment.inner_text())[:4_000],
            "paragraphs": [await _text(item, 2_000) for item in (await comment.locator("p").all())[:30]],
            "inner_html": (await comment.inner_html())[:8_000],
        }
    if name == "quiz_user_overrides":
        report["user_overrides"] = [
            {
                "user_path": urlsplit(
                    await row.locator("a[href*='/user/view.php']").first.get_attribute("href")
                    or ""
                ).path,
                "user_query_keys": sorted(
                    parse_qs(
                        urlsplit(
                            await row.locator("a[href*='/user/view.php']")
                            .first.get_attribute("href")
                            or ""
                        ).query
                    )
                ),
                "edit_url": (
                    await row.locator("a[href*='/mod/quiz/overrideedit.php']")
                    .first.get_attribute("href")
                    or ""
                ),
                "text": " ".join((await row.inner_text()).split())[:500],
            }
            for row in (await page.locator("tr").all())[:300]
            if await row.locator("a[href*='/user/view.php']").count()
            and await row.locator("a[href*='/mod/quiz/overrideedit.php']").count()
        ]
    return report


async def _wait_for_login(page: Page, timeout_seconds: int) -> None:
    await page.goto(f"{BASE_URL}/course/view.php?id={COURSE_ID}", wait_until="domcontentloaded", timeout=60_000)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    announced = False
    while not await page.locator("a[href*='/login/logout.php']").count():
        if not announced:
            print("Sign in directly in the opened Moodle window; credentials are not read by the script.")
            announced = True
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Moodle login was not completed before the timeout")
        await asyncio.sleep(1)


async def main_async(args: argparse.Namespace) -> int:
    reports: list[dict[str, object]] = []
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=args.headless,
            locale="ru-RU",
            service_workers="block",
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await _wait_for_login(page, args.timeout)
            targets = (
                ("assign_settings", f"{BASE_URL}/course/modedit.php?update=23457&return=1"),
                ("quiz_settings", f"{BASE_URL}/course/modedit.php?update=30354&return=1"),
                ("quiz_user_overrides", f"{BASE_URL}/mod/quiz/overrides.php?cmid=30354&mode=user"),
                ("quiz_questions", f"{BASE_URL}/mod/quiz/edit.php?cmid=30354"),
                ("quiz_report", f"{BASE_URL}/mod/quiz/report.php?id=30354&mode=overview&attempts=enrolled_with&onlyregraded=0&slotmarks=1&group=0"),
                ("assign_grading", f"{BASE_URL}/mod/assign/view.php?id=23457&action=grading&page=0&perpage=100"),
            )
            for name, url in targets:
                reports.append(await _page_report(page, name, url))
                if name == "quiz_user_overrides":
                    edit = page.locator("a[href*='/mod/quiz/overrideedit.php']").first
                    if await edit.count():
                        reports.append(
                            await _page_report(
                                page,
                                "quiz_user_override_edit",
                                await edit.get_attribute("href") or "",
                            )
                        )
                if name == "quiz_report":
                    review = page.locator("a.reviewlink[href*='/mod/quiz/review.php']").first
                    if await review.count():
                        review_url = await review.get_attribute("href") or ""
                        reports.append(await _page_report(page, "quiz_review", review_url))
                        comment_link = page.locator("a[href*='/mod/quiz/comment.php'], a[href*='/mod/quiz/reviewquestion.php']").first
                        if await comment_link.count():
                            reports.append(
                                await _page_report(
                                    page,
                                    "quiz_grade_question",
                                    await comment_link.get_attribute("href") or "",
                                )
                            )
            output = Path(OUTPUT_PATH)
            output.write_text(json.dumps({"pages": reports}, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(output, 0o600)
            print(output)
        finally:
            await context.close()
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--timeout", type=int, default=1_800)
    raise SystemExit(asyncio.run(main_async(parser.parse_args())))
