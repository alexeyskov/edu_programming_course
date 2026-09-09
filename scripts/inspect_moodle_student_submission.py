#!/usr/bin/env python3
"""Exercise the student-facing Moodle file submission flow in teacher preview.

The helper accepts a temporary Playwright ``storage_state`` captured after an
operator login.  It can upload an in-memory demonstration C++ file and open the
quiz summary, but deliberately never clicks Moodle's final submit control.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Locator, Page, async_playwright

BASE_ORIGIN = "https://edu.mmcs.sfedu.ru"
DEFAULT_QUIZ_ID = 30354
DEMO_SOURCE = b"// Teacher preview file for integration inspection.\nint main() { return 0; }\n"


def route_shape(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" != BASE_ORIGIN:
        return ""
    keys = sorted({key for key, _ in parse_qsl(parsed.query)})
    suffix = "?" + "&".join(f"{key}=:value" for key in keys) if keys else ""
    return f"{parsed.path}{suffix}"


async def text(locator: Locator, limit: int = 500) -> str:
    try:
        return " ".join((await locator.inner_text(timeout=2_000)).split())[:limit]
    except Exception:
        return ""


async def page_schema(page: Page) -> dict[str, Any]:
    body = page.locator("body")
    buttons = []
    for item in (await page.locator("button, input[type='submit'], a.btn").all())[:150]:
        buttons.append(
            {
                "tag": await item.evaluate("node => node.tagName.toLowerCase()"),
                "id": (await item.get_attribute("id") or "")[:160],
                "name": (await item.get_attribute("name") or "")[:160],
                "classes": (await item.get_attribute("class") or "")[:300],
                "text": await text(item),
                "visible": await item.is_visible(),
            }
        )
    return {
        "route": route_shape(page.url),
        "title": (await page.title())[:500],
        "body_id": (await body.get_attribute("id") or "")[:200],
        "body_class": (await body.get_attribute("class") or "")[:1_000],
        "headings": [
            await text(item, 500)
            for item in (await page.locator("h1, h2, h3").all())[:80]
        ],
        "selector_counts": {
            selector: await page.locator(selector).count()
            for selector in (
                ".que",
                ".que.essay",
                ".filemanager",
                ".filemanager .fp-btn-add",
                ".filemanager .fp-file",
                ".file-picker",
                "input[type='file']",
                "#mod_quiz-next-nav",
                "form[action*='/mod/quiz/processattempt.php']",
                "form[action*='/mod/quiz/processattempt.php'] input[name='finishattempt']",
                "button:has-text('Отправить всё и завершить тест')",
                "input[value*='Отправить всё']",
            )
        },
        "buttons": buttons,
    }


async def click_and_settle(page: Page, locator: Locator) -> None:
    await locator.click()
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(1_500)


async def open_preview(page: Page, quiz_url: str) -> tuple[bool, str]:
    await page.goto(quiz_url, wait_until="domcontentloaded", timeout=60_000)
    if not await page.locator("a[href*='/login/logout.php']").count():
        return False, "storage state is not authenticated"
    form = page.locator("form[action*='/mod/quiz/startattempt.php']").first
    if not await form.count():
        return False, "preview form not found"
    trigger = form.locator("button[type='submit'], input[type='submit']").first
    trigger_text = await text(trigger)
    if not any(marker in trigger_text.casefold() for marker in ("просмотр", "preview")):
        return False, "refused: start control is not an explicit teacher preview"
    await click_and_settle(page, trigger)
    modal = page.locator(".modal.show, [role='dialog']:visible").last
    if await modal.count() and await modal.is_visible():
        confirm = modal.locator(
            "button.btn-primary:visible, input.btn-primary[type='submit']:visible"
        ).last
        if await confirm.count():
            await click_and_settle(page, confirm)
    return (
        (await page.locator("body").get_attribute("id") or "")
        == "page-mod-quiz-attempt",
        trigger_text,
    )


async def upload_demo(page: Page) -> dict[str, Any]:
    add = page.locator(
        ".filemanager .fp-btn-add a, .filemanager .fp-btn-add button, "
        ".filemanager a[role='button'][title*='Добавить']"
    ).first
    if not await add.count():
        return {"uploaded": False, "reason": "file-manager add control not found"}
    await add.click()
    dialog = page.locator(".file-picker:visible, [role='dialog']:visible").last
    try:
        await dialog.wait_for(state="visible", timeout=10_000)
    except Exception:
        return {"uploaded": False, "reason": "file picker did not open"}

    file_input = dialog.locator("input[type='file']").first
    if not await file_input.count():
        upload_repository = dialog.get_by_text("Загрузить файл", exact=True).first
        if not await upload_repository.count():
            upload_repository = dialog.get_by_text("Upload a file", exact=True).first
        if await upload_repository.count():
            await upload_repository.click()
            await page.wait_for_timeout(1_000)
        file_input = dialog.locator("input[type='file']").first
    if not await file_input.count():
        file_input = page.locator("input[type='file']").last
    if not await file_input.count():
        return {"uploaded": False, "reason": "file input not found"}
    await file_input.set_input_files(
        {
            "name": "eduprog-preview.cpp",
            "mimeType": "text/x-c++src",
            "buffer": DEMO_SOURCE,
        }
    )
    submit = dialog.locator(
        ".fp-upload-btn, button:has-text('Загрузить этот файл'), "
        "input[type='submit'][value*='Загрузить']"
    ).first
    if not await submit.count():
        return {"uploaded": False, "reason": "file-picker upload control not found"}
    await submit.click()
    try:
        await dialog.wait_for(state="hidden", timeout=20_000)
    except Exception:
        pass
    await page.wait_for_timeout(2_000)
    file_entries = page.locator(".filemanager .fp-file")
    names = []
    for item in (await file_entries.all())[:20]:
        name = await text(item, 300)
        if not name:
            name = (
                await item.get_attribute("title")
                or await item.get_attribute("aria-label")
                or ""
            )[:300]
        names.append(name)
    uploaded = any("eduprog-preview.cpp" in name for name in names)
    return {
        "uploaded": uploaded,
        "reason": "" if uploaded else "uploaded file was not visible in manager",
        "visible_file_names": names,
    }


async def inspect(args: argparse.Namespace) -> int:
    storage_state = Path(args.storage_state).resolve()
    output_path = Path(args.output).resolve()
    screenshot_dir = Path(args.screenshot_dir).resolve()
    screenshot_dir.mkdir(parents=True, exist_ok=True)
    quiz_url = f"{BASE_ORIGIN}/mod/quiz/view.php?id={args.quiz_id}"

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        context = await browser.new_context(
            storage_state=str(storage_state),
            locale="ru-RU",
            viewport={"width": 1500, "height": 1000},
            service_workers="block",
        )
        try:
            page = await context.new_page()
            opened, detail = await open_preview(page, quiz_url)
            report: dict[str, Any] = {
                "quiz_id": args.quiz_id,
                "preview_opened": opened,
                "preview_trigger": detail,
            }
            if not opened:
                report["attempt"] = await page_schema(page)
            else:
                await page.wait_for_timeout(args.settle_milliseconds)
                report["attempt"] = await page_schema(page)
                attempt_shot = screenshot_dir / "moodle-student-attempt.png"
                await page.screenshot(path=str(attempt_shot), full_page=True)
                os.chmod(attempt_shot, 0o600)
                if args.upload_demo:
                    report["upload"] = await upload_demo(page)
                    upload_shot = screenshot_dir / "moodle-student-uploaded.png"
                    await page.screenshot(path=str(upload_shot), full_page=True)
                    os.chmod(upload_shot, 0o600)
                    if args.open_summary and report["upload"]["uploaded"]:
                        next_control = page.locator("#mod_quiz-next-nav").first
                        if await next_control.count():
                            await click_and_settle(page, next_control)
                            report["summary"] = await page_schema(page)
                            summary_shot = screenshot_dir / "moodle-student-summary.png"
                            await page.screenshot(path=str(summary_shot), full_page=True)
                            os.chmod(summary_shot, 0o600)
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(output_path, 0o600)
            print(f"preview_opened={str(opened).lower()}")
            print(f"uploaded={str(report.get('upload', {}).get('uploaded', False)).lower()}")
            print(f"summary_opened={str('summary' in report).lower()}")
            print(f"report={output_path}")
        finally:
            await context.close()
            await browser.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--storage-state", required=True)
    parser.add_argument("--quiz-id", type=int, default=DEFAULT_QUIZ_ID)
    parser.add_argument("--upload-demo", action="store_true")
    parser.add_argument("--open-summary", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--settle-milliseconds", type=int, default=8_000)
    parser.add_argument(
        "--output",
        default="/private/tmp/edu-course-moodle-student-submission.json",
    )
    parser.add_argument("--screenshot-dir", default="/private/tmp")
    return parser


def main() -> int:
    return asyncio.run(inspect(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
