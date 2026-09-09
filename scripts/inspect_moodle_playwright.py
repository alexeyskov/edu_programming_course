#!/usr/bin/env python3
"""Open Moodle for an operator-assisted, secret-free structural inspection.

The operator enters credentials directly into Moodle.  The script never reads
form values, cookies, local storage, session storage or inline script bodies.
Only bounded structural metadata is written to a temporary JSON report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from playwright.async_api import BrowserContext, Page, async_playwright

from inspect_moodle_workflows import (
    OUTPUT_PATH as WORKFLOW_OUTPUT_PATH,
    TARGETS as WORKFLOW_TARGETS,
    inspect_page as inspect_workflow_page,
)

DEFAULT_COURSE_URL = "https://edu.mmcs.sfedu.ru/course/view.php?id=549"
SAFE_QUERY_KEYS = frozenset(
    {
        "id",
        "courseid",
        "cmid",
        "mode",
        "section",
        "page",
        "perpage",
        "download",
    }
)


def safe_url(value: str, *, expected_origin: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return ""
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if origin != expected_origin:
        return ""
    query = urlencode(
        [(key, item) for key, item in parse_qsl(parsed.query) if key in SAFE_QUERY_KEYS]
    )
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


async def bounded_text(locator: Any, limit: int = 500) -> str:
    try:
        value = " ".join((await locator.inner_text(timeout=1_500)).split())
    except Exception:
        return ""
    return value[:limit]


async def first_text(locator: Any, selectors: tuple[str, ...], limit: int = 500) -> str:
    for selector in selectors:
        candidate = locator.locator(selector).first
        try:
            if await candidate.count():
                value = await bounded_text(candidate, limit)
                if value:
                    return value
        except Exception:
            continue
    return ""


async def collect_structure(page: Page, course_url: str) -> dict[str, Any]:
    parsed_course = urlsplit(course_url)
    expected_origin = f"{parsed_course.scheme.lower()}://{parsed_course.netloc.lower()}"
    body = page.locator("body")
    body_class = (await body.get_attribute("class") or "")[:1_000]
    generator = ""
    generator_meta = page.locator('meta[name="generator"]').first
    if await generator_meta.count():
        generator = (await generator_meta.get_attribute("content") or "")[:200]

    section_locator = page.locator(
        "li.section, section.course-section, [data-region='course-section-list'] "
        "[data-sectionid], [data-for='section']"
    )
    sections: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    for index in range(min(await section_locator.count(), 256)):
        section = section_locator.nth(index)
        section_id = (
            await section.get_attribute("data-sectionid")
            or await section.get_attribute("data-id")
            or await section.get_attribute("id")
            or str(index)
        )[:120]
        if section_id in seen_sections:
            continue
        seen_sections.add(section_id)
        title = await first_text(
            section,
            (
                ".sectionname",
                ".section-title",
                "[data-for='section_title']",
                "h2",
                "h3",
            ),
            300,
        )
        activity_locator = section.locator(
            ".activity.activity-wrapper[data-id], li.activity[data-id]"
        )
        activities: list[dict[str, Any]] = []
        seen_activities: set[str] = set()
        for activity_index in range(min(await activity_locator.count(), 512)):
            activity = activity_locator.nth(activity_index)
            activity_id = (
                await activity.get_attribute("data-id")
                or await activity.get_attribute("data-cmid")
                or await activity.get_attribute("id")
                or f"{section_id}:{activity_index}"
            )[:120]
            if activity_id in seen_activities:
                continue
            seen_activities.add(activity_id)
            link = activity.locator("a[href]").first
            href = ""
            if await link.count():
                raw_href = await link.get_attribute("href") or ""
                href = safe_url(raw_href, expected_origin=expected_origin)
            classes = (await activity.get_attribute("class") or "")[:500]
            module_match = re.search(r"\bmodtype_([a-z0-9_]+)\b", classes)
            module = (
                await activity.get_attribute("data-modname")
                or (module_match.group(1) if module_match else "")
            )[:64]
            activities.append(
                {
                    "id": activity_id,
                    "module": module,
                    "name": await first_text(
                        activity,
                        (
                            ".activityname",
                            ".instancename",
                            "[data-activityname]",
                            "a[href]",
                        ),
                        300,
                    ),
                    "url": href,
                    "classes": classes,
                }
            )
        sections.append(
            {
                "id": section_id,
                "title": title,
                "classes": (await section.get_attribute("class") or "")[:500],
                "activities": activities,
            }
        )

    links: list[dict[str, str]] = []
    seen_links: set[str] = set()
    link_locator = page.locator("a[href]")
    for index in range(min(await link_locator.count(), 1_000)):
        link = link_locator.nth(index)
        raw_href = await link.get_attribute("href") or ""
        href = safe_url(raw_href, expected_origin=expected_origin)
        if not href or href in seen_links:
            continue
        seen_links.add(href)
        links.append({"url": href, "text": await bounded_text(link, 240)})
        if len(links) >= 300:
            break

    forms: list[dict[str, Any]] = []
    form_locator = page.locator("form")
    for index in range(min(await form_locator.count(), 100)):
        form = form_locator.nth(index)
        action = safe_url(
            await form.get_attribute("action") or page.url,
            expected_origin=expected_origin,
        )
        fields: list[dict[str, str]] = []
        inputs = form.locator("input, select, textarea, button")
        for field_index in range(min(await inputs.count(), 100)):
            field = inputs.nth(field_index)
            fields.append(
                {
                    "tag": await field.evaluate("element => element.tagName.toLowerCase()"),
                    "name": (await field.get_attribute("name") or "")[:120],
                    "type": (await field.get_attribute("type") or "")[:60],
                }
            )
        forms.append(
            {
                "action": action,
                "method": (await form.get_attribute("method") or "get").lower()[:10],
                "fields": fields,
            }
        )

    return {
        "url": safe_url(page.url, expected_origin=expected_origin),
        "title": (await page.title())[:500],
        "generator": generator,
        "body_id": (await body.get_attribute("id") or "")[:200],
        "body_class": body_class,
        "sections": sections,
        "links": links,
        "forms": forms,
    }


async def is_authenticated(page: Page) -> bool:
    body_class = (await page.locator("body").get_attribute("class") or "").split()
    if "loggedin" in body_class and "notloggedin" not in body_class:
        return True
    return bool(await page.locator("a[href*='/login/logout.php']").count())


async def wait_for_course(page: Page, course_id: str, timeout_seconds: int) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        parsed = urlsplit(page.url)
        query = dict(parse_qsl(parsed.query))
        if (
            parsed.path.rstrip("/") == "/course/view.php"
            and query.get("id") == course_id
            and await is_authenticated(page)
        ):
            try:
                await page.locator("body").wait_for(state="visible", timeout=5_000)
                return
            except Exception:
                pass
        await asyncio.sleep(1)
    raise TimeoutError("course page was not opened before the inspection timeout")


async def inspect(args: argparse.Namespace) -> int:
    profile_dir = Path(args.profile_dir).resolve()
    output_path = Path(args.output).resolve()
    screenshot_path = Path(args.screenshot).resolve()
    profile_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    screenshot_path.parent.mkdir(parents=True, exist_ok=True)
    course_id = dict(parse_qsl(urlsplit(args.course_url).query)).get("id", "")
    if not course_id.isdigit():
        raise ValueError("course URL must contain a numeric id")

    async with async_playwright() as playwright:
        context: BrowserContext = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=False,
            viewport={"width": 1500, "height": 1000},
            locale="ru-RU",
            args=["--disable-sync", "--no-default-browser-check"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(args.course_url, wait_until="domcontentloaded", timeout=60_000)
            print("Browser opened. Sign in directly in Moodle; credentials are not read by the script.")
            print(f"After login open: {args.course_url}")
            await wait_for_course(page, course_id, args.timeout)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:
                pass
            report = await collect_structure(page, args.course_url)
            await page.screenshot(path=str(screenshot_path), full_page=True)
            os.chmod(screenshot_path, 0o600)
            section_urls = [
                entry["url"]
                for entry in report["links"]
                if urlsplit(entry["url"]).path.rstrip("/") == "/course/view.php"
                and dict(parse_qsl(urlsplit(entry["url"]).query)).get("id") == course_id
                and dict(parse_qsl(urlsplit(entry["url"]).query)).get("section", "").isdigit()
            ][:128]
            section_pages: list[dict[str, Any]] = []
            for section_url in dict.fromkeys(section_urls):
                await page.goto(section_url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    await page.wait_for_load_state("networkidle", timeout=10_000)
                except Exception:
                    pass
                section_pages.append(await collect_structure(page, args.course_url))
            report["section_pages"] = section_pages
            output_path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(output_path, 0o600)
            workflow_reports = [
                await inspect_workflow_page(page, name, url)
                for name, url in WORKFLOW_TARGETS
            ]
            workflow_output_path = Path(WORKFLOW_OUTPUT_PATH)
            workflow_output_path.write_text(
                json.dumps({"pages": workflow_reports}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.chmod(workflow_output_path, 0o600)
            print(f"Structure saved to {output_path}")
            print(f"Screenshot saved to {screenshot_path}")
            print(f"Workflow structure saved to {workflow_output_path}")
            print("You may close the browser window now.")
            while context.pages:
                await asyncio.sleep(1)
        finally:
            await context.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--course-url", default=DEFAULT_COURSE_URL)
    parser.add_argument(
        "--profile-dir",
        default="/private/tmp/edu-course-moodle-inspection-profile",
    )
    parser.add_argument(
        "--output",
        default="/private/tmp/edu-course-moodle-549-structure.json",
    )
    parser.add_argument(
        "--screenshot",
        default="/private/tmp/edu-course-moodle-549.png",
    )
    parser.add_argument("--timeout", type=int, default=1_800)
    return parser


def main() -> int:
    return asyncio.run(inspect(build_parser().parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
