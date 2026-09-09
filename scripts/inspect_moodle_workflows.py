#!/usr/bin/env python3
"""Capture redacted structural metadata for selected Moodle workflows."""

from __future__ import annotations

import asyncio
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import Page, async_playwright

BASE_URL = "https://edu.mmcs.sfedu.ru"
COURSE_ID = "549"
PROFILE_DIR = "/private/tmp/edu-course-moodle-inspection-profile"
OUTPUT_PATH = "/private/tmp/edu-course-moodle-workflows.json"
TARGETS = (
    ("profile", f"{BASE_URL}/user/profile.php"),
    ("course", f"{BASE_URL}/course/view.php?id={COURSE_ID}"),
    ("participants", f"{BASE_URL}/user/index.php?id={COURSE_ID}&perpage=5000"),
    ("assign", f"{BASE_URL}/mod/assign/view.php?id=23457"),
    ("assign_grading", f"{BASE_URL}/mod/assign/view.php?id=23457&action=grading"),
    ("quiz", f"{BASE_URL}/mod/quiz/view.php?id=30354"),
    ("quiz_report", f"{BASE_URL}/mod/quiz/report.php?id=30354&mode=overview"),
)
KNOWN_SELECTORS = (
    "#login",
    "#username",
    "#password",
    "#loginbtn",
    "a[href*='/login/logout.php']",
    ".usermenu .usertext",
    "[data-region='user-menu']",
    "[data-userid]",
    "[data-user-id]",
    ".activity.activity-wrapper[data-id]",
    "table#participants",
    "table#participants tbody tr",
    "table#attempts",
    "table#attempts tbody tr",
    "a.reviewlink",
    "a[href*='/mod/quiz/review.php']",
    "a[href*='/mod/quiz/comment.php']",
    "[data-region='grading-navigation']",
    "table.generaltable",
    "form#mform1",
    "input#id_submitbutton",
    "input[name$='-mark']",
    "div.editor_atto_content",
    "textarea",
    ".tox-tinymce",
    ".que.essay",
    ".qtype_essay_response",
    ".attachments a",
)
SAFE_QUERY_KEYS = frozenset(
    {
        "id",
        "courseid",
        "cmid",
        "mode",
        "section",
        "action",
        "attempt",
        "slot",
        "userid",
        "rownum",
    }
)


def route_shape(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    if f"{parsed.scheme.lower()}://{parsed.netloc.lower()}" != BASE_URL:
        return ""
    keys = sorted({key for key, _ in parse_qsl(parsed.query) if key in SAFE_QUERY_KEYS})
    suffix = "?" + "&".join(f"{key}=:{key}" for key in keys) if keys else ""
    return f"{parsed.path}{suffix}"


async def input_schema(page: Page) -> list[dict[str, Any]]:
    forms: list[dict[str, Any]] = []
    for form_index in range(min(await page.locator("form").count(), 100)):
        form = page.locator("form").nth(form_index)
        controls: list[dict[str, str]] = []
        locator = form.locator("input, select, textarea, button")
        for index in range(min(await locator.count(), 300)):
            field = locator.nth(index)
            controls.append(
                {
                    "tag": await field.evaluate("node => node.tagName.toLowerCase()"),
                    "id": (await field.get_attribute("id") or "")[:120],
                    "name": (await field.get_attribute("name") or "")[:160],
                    "type": (await field.get_attribute("type") or "")[:60],
                }
            )
        forms.append(
            {
                "id": (await form.get_attribute("id") or "")[:120],
                "method": (await form.get_attribute("method") or "get").lower()[:10],
                "action_shape": route_shape(await form.get_attribute("action") or page.url),
                "controls": controls,
            }
        )
    return forms


async def table_schema(page: Page) -> list[dict[str, Any]]:
    tables: list[dict[str, Any]] = []
    for table_index in range(min(await page.locator("table").count(), 50)):
        table = page.locator("table").nth(table_index)
        headers = [
            " ".join((await item.inner_text()).split())[:200]
            for item in await table.locator("thead th").all()
        ][:100]
        first_row = table.locator("tbody tr").first
        cell_classes: list[str] = []
        row_link_shapes: Counter[str] = Counter()
        if await first_row.count():
            cell_classes = [
                (await item.get_attribute("class") or "")[:200]
                for item in await first_row.locator("th, td").all()
            ][:100]
            for link in await first_row.locator("a[href]").all():
                shape = route_shape(await link.get_attribute("href") or "")
                if shape:
                    row_link_shapes[shape] += 1
        tables.append(
            {
                "id": (await table.get_attribute("id") or "")[:120],
                "classes": (await table.get_attribute("class") or "")[:300],
                "rows": min(await table.locator("tbody tr").count(), 100_000),
                "headers": headers,
                "first_row_cell_classes": cell_classes,
                "first_row_link_shapes": dict(row_link_shapes),
            }
        )
    return tables


async def inspect_page(page: Page, name: str, url: str) -> dict[str, Any]:
    response = await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    body = page.locator("body")
    selector_counts = {
        selector: await page.locator(selector).count() for selector in KNOWN_SELECTORS
    }
    route_counts: Counter[str] = Counter()
    for link in await page.locator("a[href]").all():
        shape = route_shape(await link.get_attribute("href") or "")
        if shape:
            route_counts[shape] += 1
    headings = [
        " ".join((await heading.inner_text()).split())[:300]
        for heading in (await page.locator("h1, h2, h3").all())[:80]
    ]
    documentation_url = ""
    documentation = page.locator("a[href*='docs.moodle.org']").first
    if await documentation.count():
        raw = await documentation.get_attribute("href") or ""
        parsed = urlsplit(raw)
        documentation_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
    return {
        "name": name,
        "requested_route": route_shape(url),
        "final_route": route_shape(page.url),
        "http_status": response.status if response else None,
        "title": (await page.title())[:500],
        "body_id": (await body.get_attribute("id") or "")[:200],
        "body_class": (await body.get_attribute("class") or "")[:1_000],
        "headings": headings,
        "selector_counts": selector_counts,
        "route_counts": dict(route_counts.most_common(300)),
        "forms": await input_schema(page),
        "tables": await table_schema(page),
        "documentation_url": documentation_url,
    }


async def main_async() -> int:
    output = Path(OUTPUT_PATH)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            PROFILE_DIR,
            headless=True,
            locale="ru-RU",
            service_workers="block",
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            reports = [await inspect_page(page, name, url) for name, url in TARGETS]
            output.write_text(
                json.dumps({"pages": reports}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(output, 0o600)
            print(output)
        finally:
            await context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
