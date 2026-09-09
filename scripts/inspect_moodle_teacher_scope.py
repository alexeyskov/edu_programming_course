#!/usr/bin/env python3
"""Inspect the Moodle teacher/group projection without exporting credentials.

The script reuses an operator-created Playwright profile, reads only the
course roster and the public group/teacher labels rendered on the course
page, and writes aggregate diagnostics.  Passwords, cookies, local storage,
submission contents and group access codes are never included in the report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

from playwright.async_api import async_playwright


BASE_URL = "https://edu.mmcs.sfedu.ru"


def _compact(value: str, limit: int = 255) -> str:
    return " ".join(value.split())[:limit]


def _numeric_query_value(value: str, name: str) -> str:
    values = parse_qs(urlsplit(value).query, keep_blank_values=True).get(name, [])
    return values[0] if len(values) == 1 and values[0].isdigit() else ""


async def inspect(args: argparse.Namespace) -> int:
    profile_dir = Path(args.profile_dir).resolve()
    output = Path(args.output).resolve()
    if not profile_dir.is_dir():
        raise ValueError("Playwright profile directory does not exist")
    if not args.course_id.isdigit():
        raise ValueError("course id must be numeric")

    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=not args.headed,
            locale="ru-RU",
            service_workers="block",
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            participants_url = f"{BASE_URL}/user/index.php?" + urlencode(
                {"id": args.course_id, "perpage": 5000}
            )
            response = await page.goto(
                participants_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            if response is None or response.status != 200:
                raise RuntimeError("Moodle participants page is unavailable")
            if not await page.locator("a[href*='/login/logout.php']").count():
                if not args.headed:
                    raise RuntimeError("Moodle profile is not authenticated")
                print("Browser opened. Sign in directly in Moodle; form values are not read.")
                deadline = asyncio.get_running_loop().time() + args.login_timeout
                while asyncio.get_running_loop().time() < deadline:
                    if await page.locator("a[href*='/login/logout.php']").count():
                        break
                    await asyncio.sleep(1)
                else:
                    raise TimeoutError("Moodle login was not completed in time")
                await page.goto(
                    participants_url,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

            current_user = _compact(
                await page.locator(".usermenu .usertext").first.inner_text(),
            )
            surname = next(iter(re.findall(r"[A-Za-zА-Яа-яЁё-]+", current_user)), "")
            participant_rows = page.locator("table#participants tbody tr")
            participant_row_count = await participant_rows.count()
            rows_data = await participant_rows.evaluate_all(
                """rows => rows.map(row => {
                    const cells = Array.from(row.children).filter(node =>
                        node.tagName === 'TH' || node.tagName === 'TD');
                    const profile = row.querySelector(
                        "a[href*='/user/view.php'], a[href*='/user/profile.php']");
                    const groupCell = cells[3] || null;
                    const groups = groupCell
                        ? Array.from(groupCell.querySelectorAll('a'))
                            .map(node => node.innerText || '')
                        : [];
                    return {
                        displayName: profile ? (profile.innerText || '') : '',
                        profileHref: profile ? (profile.href || '') : '',
                        role: cells[2] ? (cells[2].innerText || '') : '',
                        groupText: groupCell ? (groupCell.innerText || '') : '',
                        groups,
                    };
                })"""
            )
            role_counts: Counter[str] = Counter()
            group_counts: Counter[str] = Counter()
            current_user_rows: list[dict[str, object]] = []
            matching_group_counts: Counter[str] = Counter()
            for row_data in rows_data:
                display_name = _compact(str(row_data.get("displayName", "")))
                role = _compact(str(row_data.get("role", "")), 500)
                groups = [
                    _compact(value)
                    for value in row_data.get("groups", [])
                    if _compact(value)
                ]
                if not groups:
                    fallback = _compact(str(row_data.get("groupText", "")))
                    if fallback and fallback.casefold() not in {"-", "нет групп", "без групп"}:
                        groups = [fallback]
                role_counts[role or "(empty)"] += 1
                for group in groups:
                    group_counts[group] += 1
                    if surname and surname.casefold() in group.casefold():
                        matching_group_counts[group] += 1
                if display_name.casefold() == current_user.casefold():
                    href = str(row_data.get("profileHref", ""))
                    current_user_rows.append(
                        {
                            "user_id": _numeric_query_value(href, "id"),
                            "display_name": display_name,
                            "role": role,
                            "groups": groups,
                        }
                    )

            await page.goto(
                f"{BASE_URL}/course/view.php?id={args.course_id}",
                wait_until="domcontentloaded",
                timeout=60_000,
            )
            teacher_labels: list[str] = []
            for table in await page.locator("table").all():
                header = _compact(await table.locator("thead").inner_text()) if await table.locator("thead").count() else ""
                if "группа/преподаватель" not in header.casefold():
                    continue
                for row in await table.locator("tbody tr").all():
                    cells = row.locator(":scope > th, :scope > td")
                    if await cells.count():
                        label = _compact(await cells.first.inner_text(), 500)
                        if label:
                            teacher_labels.append(label)

            report = {
                "course_id": args.course_id,
                "current_user": current_user,
                "participant_rows": participant_row_count,
                "current_user_rows": current_user_rows,
                "role_counts": dict(role_counts.most_common()),
                "matching_group_counts": dict(matching_group_counts.most_common()),
                "group_counts": dict(group_counts.most_common()),
                "course_teacher_labels": teacher_labels[:500],
            }
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(output, 0o600)
            print(output)
        finally:
            await context.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--course-id", default="549")
    parser.add_argument(
        "--profile-dir",
        default="/private/tmp/edu-course-moodle-teacher-scope-profile",
    )
    parser.add_argument(
        "--output",
        default="/private/tmp/edu-course-moodle-teacher-scope.json",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--login-timeout", type=int, default=1800)
    return asyncio.run(inspect(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
