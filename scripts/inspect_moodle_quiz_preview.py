#!/usr/bin/env python3
"""Inspect a Moodle quiz attempt without reading credentials or submitting it.

This is a development-only helper.  It may start a teacher preview attempt when
``--start-preview`` is supplied, but it never fills answers, advances between
pages, finishes the attempt, changes a grade, or records cookies in its report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

BASE_ORIGIN = "https://edu.mmcs.sfedu.ru"
DEFAULT_QUIZ_ID = 30354


def route_shape(value: str) -> str:
    """Return an origin-checked route with names, but never query values."""

    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if origin != BASE_ORIGIN:
        return ""
    keys = sorted({key for key, _ in parse_qsl(parsed.query)})
    query = "&".join(f"{key}=:value" for key in keys)
    return f"{parsed.path}{'?' + query if query else ''}"


async def bounded_text(locator: Locator, limit: int = 2_000) -> str:
    try:
        return " ".join((await locator.inner_text(timeout=2_000)).split())[:limit]
    except Exception:
        return ""


async def controls_schema(root: Locator, limit: int = 300) -> list[dict[str, Any]]:
    controls: list[dict[str, Any]] = []
    locator = root.locator("input, textarea, select, button, [contenteditable='true']")
    for index in range(min(await locator.count(), limit)):
        item = locator.nth(index)
        controls.append(
            {
                "tag": await item.evaluate("node => node.tagName.toLowerCase()"),
                "id": (await item.get_attribute("id") or "")[:160],
                "name": (await item.get_attribute("name") or "")[:160],
                "type": (await item.get_attribute("type") or "")[:80],
                "classes": (await item.get_attribute("class") or "")[:300],
                "contenteditable": await item.get_attribute("contenteditable"),
                "visible": await item.is_visible(),
            }
        )
    return controls


async def collect_attempt_structure(page: Page) -> dict[str, Any]:
    body = page.locator("body")
    questions: list[dict[str, Any]] = []
    locator = page.locator(".que")
    for index in range(min(await locator.count(), 100)):
        question = locator.nth(index)
        qtext = question.locator(".qtext").first
        questions.append(
            {
                "id": (await question.get_attribute("id") or "")[:160],
                "classes": (await question.get_attribute("class") or "")[:500],
                "question_text": await bounded_text(qtext),
                "controls": await controls_schema(question),
                "has_essay_response": bool(
                    await question.locator(".qtype_essay_response").count()
                ),
                "has_code_runner": bool(
                    await question.locator(
                        ".qtype_coderunner, [class*='coderunner'], "
                        "button:has-text('Проверить'), button:has-text('Check')"
                    ).count()
                ),
                "attachments": [
                    route_shape(await link.get_attribute("href") or "")
                    for link in await question.locator(".attachments a[href]").all()
                ][:50],
            }
        )

    buttons = []
    for item in (await page.locator("button, input[type='submit']").all())[:100]:
        buttons.append(
            {
                "text": await bounded_text(item, 300),
                "id": (await item.get_attribute("id") or "")[:160],
                "name": (await item.get_attribute("name") or "")[:160],
                "classes": (await item.get_attribute("class") or "")[:300],
                "visible": await item.is_visible(),
            }
        )

    forms = []
    for form in (await page.locator("form").all())[:50]:
        forms.append(
            {
                "id": (await form.get_attribute("id") or "")[:160],
                "method": (await form.get_attribute("method") or "get")[:20],
                "action": route_shape(await form.get_attribute("action") or page.url),
                "controls": await controls_schema(form),
            }
        )

    return {
        "route": route_shape(page.url),
        "title": (await page.title())[:500],
        "body_id": (await body.get_attribute("id") or "")[:200],
        "body_class": (await body.get_attribute("class") or "")[:1_000],
        "authenticated": bool(
            await page.locator("a[href*='/login/logout.php']").count()
        ),
        "headings": [
            await bounded_text(item, 500)
            for item in (await page.locator("h1, h2, h3").all())[:80]
        ],
        "questions": questions,
        "forms": forms,
        "buttons": buttons,
        "selector_counts": {
            selector: await page.locator(selector).count()
            for selector in (
                ".que",
                ".que.essay",
                ".qtype_essay_response",
                ".qtype_coderunner",
                "textarea",
                "[contenteditable='true']",
                ".tox-tinymce",
                ".editor_atto_content",
                "input[name='next']",
                "input[name='finishattempt']",
                "button:has-text('Проверить')",
                "button:has-text('Check')",
            )
        },
    }


async def wait_after_click(page: Page) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except Exception:
        pass
    await page.wait_for_timeout(1_000)


async def start_teacher_preview(page: Page) -> dict[str, Any]:
    """Start only the explicit Moodle preview/attempt flow; never submit it."""

    start_form = page.locator("form[action*='/mod/quiz/startattempt.php']").first
    if not await start_form.count():
        return {"started": False, "reason": "start form not found"}
    trigger = start_form.locator("button[type='submit'], input[type='submit']").first
    trigger_text = await bounded_text(trigger, 500)
    if not await trigger.count() or not await trigger.is_visible():
        return {"started": False, "reason": "start control is not visible"}

    await trigger.click()
    await wait_after_click(page)

    # Moodle may display a Bootstrap confirmation/preflight modal.  Accept only
    # a visible primary control inside that modal; never accept arbitrary dialogs.
    modal = page.locator(".modal.show, [role='dialog']:visible").last
    if await modal.count() and await modal.is_visible():
        confirm = modal.locator(
            "button.btn-primary:visible, input.btn-primary[type='submit']:visible"
        ).last
        if await confirm.count():
            await confirm.click()
            await wait_after_click(page)

    body_id = await page.locator("body").get_attribute("id") or ""
    started = body_id in {
        "page-mod-quiz-attempt",
        "page-mod-quiz-summary",
        "page-mod-quiz-review",
    }
    return {
        "started": started,
        "trigger_text": trigger_text,
        "final_route": route_shape(page.url),
        "body_id": body_id[:200],
        "reason": "" if started else "Moodle did not open an attempt page",
    }


async def inspect(args: argparse.Namespace) -> int:
    profile_dir = Path(args.profile_dir).resolve()
    output_path = Path(args.output).resolve()
    screenshot_path = Path(args.screenshot).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)

    quiz_url = f"{BASE_ORIGIN}/mod/quiz/view.php?id={args.quiz_id}"
    async with async_playwright() as playwright:
        context: BrowserContext = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=args.headless,
            locale="ru-RU",
            viewport={"width": 1500, "height": 1000},
            service_workers="block",
            args=["--disable-sync", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            response = await page.goto(
                quiz_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            authenticated = bool(
                await page.locator("a[href*='/login/logout.php']").count()
            )
            if not authenticated and args.wait_for_login:
                print("Browser opened. Sign in directly in Moodle.")
                print("The script does not read the login or password fields.")
                deadline = asyncio.get_running_loop().time() + args.login_timeout
                while asyncio.get_running_loop().time() < deadline:
                    if await page.locator("a[href*='/login/logout.php']").count():
                        authenticated = True
                        break
                    await asyncio.sleep(1)
                if authenticated:
                    response = await page.goto(
                        quiz_url,
                        wait_until="domcontentloaded",
                        timeout=60_000,
                    )
                else:
                    raise TimeoutError("Moodle login was not completed in time")
            report: dict[str, Any] = {
                "quiz_id": args.quiz_id,
                "http_status": response.status if response else None,
                "authenticated": authenticated,
                "initial": await collect_attempt_structure(page),
            }
            if not authenticated:
                report["preview"] = {
                    "started": False,
                    "reason": "copied browser session is not authenticated",
                }
            elif args.start_preview:
                report["preview"] = await start_teacher_preview(page)
                report["attempt"] = await collect_attempt_structure(page)
            else:
                report["preview"] = {
                    "started": False,
                    "reason": "--start-preview was not supplied",
                }

            await page.screenshot(path=str(screenshot_path), full_page=True)
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(output_path, 0o600)
            os.chmod(screenshot_path, 0o600)
            if args.storage_state_output:
                storage_state_path = Path(args.storage_state_output).resolve()
                storage_state_path.parent.mkdir(parents=True, exist_ok=True)
                await context.storage_state(path=str(storage_state_path))
                os.chmod(storage_state_path, 0o600)
            print(f"authenticated={str(authenticated).lower()}")
            print(f"body_id={report.get('attempt', report['initial']).get('body_id', '')}")
            print(f"report={output_path}")
            print(f"screenshot={screenshot_path}")
        finally:
            await context.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiz-id", type=int, default=DEFAULT_QUIZ_ID)
    parser.add_argument("--profile-dir", required=True)
    parser.add_argument(
        "--output",
        default="/private/tmp/edu-course-moodle-quiz-preview.json",
    )
    parser.add_argument(
        "--screenshot",
        default="/private/tmp/edu-course-moodle-quiz-preview.png",
    )
    parser.add_argument("--start-preview", action="store_true")
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--wait-for-login", action="store_true")
    parser.add_argument("--login-timeout", type=int, default=1_800)
    parser.add_argument("--storage-state-output")
    return parser


def main() -> int:
    return asyncio.run(inspect(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
