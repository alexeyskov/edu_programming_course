from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, Tag

from .config import exact_https_origin

_WHITESPACE = re.compile(r"\s+")
_MODULE = re.compile(r"^modtype_([a-z0-9_]{1,32})$")
_POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_LEGACY_MODULE_ID = re.compile(r"^module-([1-9][0-9]{0,19})$")
_TEACHER_WORDS = {
    "editingteacher",
    "instructor",
    "manager",
    "teacher",
    "teaching assistant",
    "ассистент",
    "менеджер",
    "преподаватель",
    "учитель",
}
_STUDENT_WORDS = {"learner", "student", "обучающийся", "студент"}


def _without_avatar_initials(value: str) -> str:
    """Remove duplicated avatar initials and canonicalise MMCS' visible name.

    The MMCS theme renders ``АК Алексей Коваленко``: avatar initials first,
    followed by Moodle's given-name/surname order.  Internally the application
    keeps the surname first so greetings and reviewer signatures are stable
    (``Коваленко А.``) instead of leaking the avatar marker into the UI.
    """

    words = value.split()
    if len(words) < 3 or not (1 <= len(words[0]) <= 3) or not words[0].isalpha():
        return value
    marker = words[0].casefold().replace("ё", "е")
    initials = "".join(word[0] for word in words[1:] if word).casefold().replace("ё", "е")
    if marker != initials[: len(marker)]:
        return value
    visible_name = words[1:]
    if len(visible_name) >= 2:
        return " ".join([visible_name[-1], *visible_name[:-1]])
    return " ".join(visible_name)


class MoodleMarkupError(ValueError):
    """The page was reachable but did not satisfy the bounded Moodle contract."""


@dataclass(frozen=True, slots=True)
class ParsedParticipants:
    members: list[dict[str, Any]]
    has_next: bool
    table_present: bool
    all_rows_classified: bool


@dataclass(frozen=True, slots=True)
class CourseSectionLink:
    section: int
    url: str
    # Moodle 5.2 top-format pages link to the section database record rather
    # than to ``course/view.php?section=<number>``.  Keeping that record id lets
    # the crawler validate both the response URL and the returned section DOM.
    section_record_id: int | None = None


def canonical_hash(value: object) -> str:
    body = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(body).hexdigest()


def _text(node: Tag | None, limit: int = 255) -> str:
    if node is None:
        return ""
    return _WHITESPACE.sub(" ", node.get_text(" ", strip=True)).strip()[:limit]


def _visible_text(node: Tag | None, limit: int = 255) -> str:
    if node is None:
        return ""
    parts: list[str] = []
    for text in node.find_all(string=True):
        if any(
            isinstance(parent, Tag) and "accesshide" in (parent.get("class") or [])
            for parent in text.parents
            if parent is not node
        ):
            continue
        clean = _WHITESPACE.sub(" ", str(text)).strip()
        if clean:
            parts.append(clean)
    return _WHITESPACE.sub(" ", " ".join(parts)).strip()[:limit]


def _same_origin_url(href: str | None, base_url: str) -> str | None:
    if not href or len(href) > 4_096:
        return None
    try:
        absolute = urljoin(f"{base_url}/", href)
        parsed = urlsplit(absolute)
        candidate_origin = exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
    except (TypeError, ValueError):
        return None
    if candidate_origin != base_url or parsed.username or parsed.password or parsed.fragment:
        return None
    return absolute


def _query_id(url: str, *, path: str, name: str = "id") -> str | None:
    parsed = urlsplit(url)
    if parsed.path.rstrip("/") != path:
        return None
    values = parse_qs(parsed.query, keep_blank_values=True).get(name, [])
    if len(values) != 1 or not _POSITIVE_ID.fullmatch(values[0]):
        return None
    return values[0]


def has_authenticated_markup(html: str, base_url: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href]"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if url and urlsplit(url).path.rstrip("/") == "/login/logout.php":
            return True
    return False


def parse_identity(html: str, base_url: str) -> dict[str, str]:
    soup = BeautifulSoup(html, "html.parser")
    user_id = ""
    profile_anchor: Tag | None = None
    for anchor in soup.select(
        ".usermenu a[href], [data-region='user-menu'] a[href], "
        "a[href*='/user/profile.php'], a[href*='/user/view.php']"
    ):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        for path in ("/user/profile.php", "/user/view.php"):
            candidate = _query_id(url, path=path)
            if candidate:
                user_id = candidate
                profile_anchor = anchor
                break
        if user_id:
            break
    if not user_id:
        for element in soup.select("[data-userid], [data-user-id]"):
            for attribute in ("data-userid", "data-user-id"):
                candidate = str(element.get(attribute, ""))
                if _POSITIVE_ID.fullmatch(candidate):
                    user_id = candidate
                    break
            if user_id:
                break
    if not user_id:
        # Moodle deliberately omits ``?id=...`` from the current user's menu
        # link on some themes.  The authenticated profile page still emits the
        # stable user id as a hidden control of its edit/reset button.  Accept
        # that value only when the enclosing form posts back to the same-origin
        # profile endpoint; arbitrary hidden inputs elsewhere must not become
        # an identity source.
        for form in soup.select("form[action]"):
            action = _same_origin_url(form.get("action"), base_url)
            if not action or urlsplit(action).path.rstrip("/") != "/user/profile.php":
                continue
            control = form.select_one("input[name='id'][value]")
            candidate = str(control.get("value", "")) if isinstance(control, Tag) else ""
            if _POSITIVE_ID.fullmatch(candidate):
                user_id = candidate
                break
    if not user_id:
        raise MoodleMarkupError("Moodle profile has no stable numeric user id")

    display_name = ""
    for selector in (
        ".usermenu .usertext",
        "#action-menu-toggle-0 .usertext",
        "[data-region='user-menu'] .usertext",
        ".page-header-headings h1",
        "#page-header h1",
    ):
        display_name = _visible_text(soup.select_one(selector))
        if display_name:
            break
    if not display_name and profile_anchor is not None:
        display_name = str(profile_anchor.get("title", "")).strip() or _visible_text(
            profile_anchor
        )
    if not display_name:
        raise MoodleMarkupError("Moodle profile has no display name")
    display_name = _without_avatar_initials(display_name)[:255]

    email = ""
    email_link = soup.select_one("a[href^='mailto:']")
    if email_link is not None:
        raw = str(email_link.get("href", ""))[7:].split("?", 1)[0].strip()
        if 3 <= len(raw) <= 254 and "@" in raw and "\r" not in raw and "\n" not in raw:
            email = raw
    html_tag = soup.find("html")
    locale = str(html_tag.get("lang", "")).strip()[:35] if isinstance(html_tag, Tag) else ""
    return {
        "external_subject": user_id,
        "display_name": display_name,
        "email": email,
        "locale": locale,
    }


def parse_course_links(html: str, base_url: str, *, maximum: int = 512) -> list[dict[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in soup.select("a[href*='/course/view.php']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        course_id = _query_id(url, path="/course/view.php")
        if not course_id or course_id in seen:
            continue
        title = _visible_text(anchor) or str(anchor.get("title", "")).strip()[:255]
        if not title:
            continue
        seen.add(course_id)
        result.append(
            {
                "external_id": course_id,
                "title": title,
                "short_name": str(anchor.get("data-course-name", "")).strip()[:120],
                "role": "UNKNOWN",
            }
        )
        if len(result) >= maximum:
            break
    return result


def parse_course_section_links(
    html: str,
    base_url: str,
    course_id: str,
    *,
    maximum: int = 128,
) -> list[CourseSectionLink]:
    """Find top-format section links and rebuild every navigated URL locally.

    Moodle <= 5.1 used ``/course/view.php?id=<course>&section=<number>``.  Moodle
    5.2 summary pages use ``/course/section.php?id=<section-record>`` and keep
    activities only on those section pages.  The record id is accepted only
    when the enclosing course-section node exposes the same ``data-id``.
    """

    soup = BeautifulSoup(html, "html.parser")
    modern: dict[int, CourseSectionLink] = {}
    for section_node in soup.select(
        "li.course-section[data-sectionid][data-id], "
        "div.course-section[data-sectionid][data-id], "
        "section.course-section[data-sectionid][data-id]"
    ):
        raw_number = str(section_node.get("data-sectionid", "")).strip()
        raw_record_id = str(section_node.get("data-id", "")).strip()
        if (
            not re.fullmatch(r"[0-9]{1,6}", raw_number)
            or int(raw_number) > 100_000
            or not _POSITIVE_ID.fullmatch(raw_record_id)
        ):
            continue
        section_number = int(raw_number)
        section_record_id = int(raw_record_id)
        matching_urls: set[str] = set()
        for anchor in section_node.select("a[href*='/course/section.php']"):
            url = _same_origin_url(anchor.get("href"), base_url)
            if not url:
                continue
            parsed = urlsplit(url)
            if parsed.path.rstrip("/") != "/course/section.php":
                continue
            query = parse_qs(parsed.query, keep_blank_values=True)
            if query.get("id") == [raw_record_id]:
                matching_urls.add(url)
        if not matching_urls:
            continue
        existing = modern.get(section_record_id)
        if existing is not None and existing.section != section_number:
            raise MoodleMarkupError("Moodle course section id is ambiguous")
        modern[section_record_id] = CourseSectionLink(
            section=section_number,
            url=f"{base_url}/course/section.php?id={section_record_id}",
            section_record_id=section_record_id,
        )
        if len(modern) > maximum:
            raise MoodleMarkupError("Moodle course exposes too many section pages")
    if modern:
        return sorted(
            modern.values(),
            key=lambda item: (item.section, item.section_record_id or 0),
        )

    section_numbers: set[int] = set()
    for anchor in soup.select("a[href*='/course/view.php'][href*='section=']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        parsed = urlsplit(url)
        if parsed.path.rstrip("/") != "/course/view.php":
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("id") != [course_id]:
            continue
        sections = query.get("section", [])
        if len(sections) != 1 or not re.fullmatch(r"[0-9]{1,6}", sections[0]):
            continue
        section = int(sections[0])
        if section > 100_000:
            continue
        section_numbers.add(section)
        if len(section_numbers) > maximum:
            raise MoodleMarkupError("Moodle course exposes too many section pages")
    return [
        CourseSectionLink(
            section=section,
            url=f"{base_url}/course/view.php?id={course_id}&section={section}",
        )
        for section in sorted(section_numbers)
    ]


def merge_course_sections(
    course: dict[str, Any],
    section_pages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Merge top-format pages and reject cross-section activity ambiguity."""

    result = dict(course)
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    cmid_owner: dict[int, str] = {}

    def merge_one(raw: object) -> None:
        if not isinstance(raw, dict):
            raise MoodleMarkupError("Moodle course section has an invalid shape")
        section_id = str(raw.get("external_id", ""))
        if not section_id or len(section_id) > 255:
            raise MoodleMarkupError("Moodle course section has no stable id")
        if section_id not in merged:
            if len(merged) >= 512:
                raise MoodleMarkupError("Moodle course has too many sections")
            merged[section_id] = {
                **raw,
                "activities": [],
                "position": len(order),
            }
            order.append(section_id)
        target = merged[section_id]
        if raw.get("title"):
            target["title"] = raw["title"]
        target["visible"] = bool(target.get("visible", True)) and bool(raw.get("visible", True))
        known = {int(item["cmid"]): item for item in target["activities"]}
        activities = raw.get("activities", [])
        if not isinstance(activities, list):
            raise MoodleMarkupError("Moodle course activities have an invalid shape")
        for activity in activities:
            if not isinstance(activity, dict) or not isinstance(activity.get("cmid"), int):
                raise MoodleMarkupError("Moodle course activity has an invalid shape")
            cmid = int(activity["cmid"])
            owner = cmid_owner.get(cmid)
            if owner is not None and owner != section_id:
                raise MoodleMarkupError("Moodle activity appears in multiple sections")
            cmid_owner[cmid] = section_id
            known[cmid] = activity
        target["activities"] = list(known.values())
        if sum(len(item["activities"]) for item in merged.values()) > 4_096:
            raise MoodleMarkupError("Moodle course has too many activities")

    for raw in course.get("sections", []):
        merge_one(raw)
    for page in section_pages:
        for raw in page.get("sections", []):
            merge_one(raw)
        result["teacher_controls"] = bool(result.get("teacher_controls")) or bool(
            page.get("teacher_controls")
        )
        result["grade_controls"] = bool(result.get("grade_controls")) or bool(
            page.get("grade_controls")
        )
    result["sections"] = [merged[section_id] for section_id in order]
    return result


def _section_id(section: Tag, position: int) -> str:
    candidate = str(section.get("data-sectionid", "")).strip()
    if candidate and len(candidate) <= 255:
        return candidate
    html_id = str(section.get("id", ""))
    match = re.fullmatch(r"section-([0-9]{1,19})", html_id)
    return match.group(1) if match else f"position-{position}"


def _activity(activity: Tag, base_url: str) -> dict[str, Any] | None:
    """Project modern and legacy Moodle activity markup onto one stable cmid.

    Moodle 4 emits ``data-id`` on ``.activity-wrapper`` while the MMCS theme
    has also used the older ``id=module-<cmid>`` list-item contract.  In both
    cases the canonical same-origin ``/mod/<module>/view.php?id=<cmid>`` link
    must agree with the container, so an unrelated numeric DOM id cannot be
    promoted into an activity.
    """

    cmid = str(activity.get("data-id", "")).strip()
    if not _POSITIVE_ID.fullmatch(cmid):
        match = _LEGACY_MODULE_ID.fullmatch(str(activity.get("id", "")).strip())
        cmid = match.group(1) if match else ""
    if not _POSITIVE_ID.fullmatch(cmid):
        return None
    module = ""
    for class_name in activity.get("class") or []:
        match = _MODULE.fullmatch(str(class_name))
        if match:
            module = match.group(1)
            break
    if not module:
        return None
    anchor = activity.select_one(
        f"a[href*='/mod/{module}/view.php'], .activityname a[href], a.aalink[href]"
    )
    url = _same_origin_url(anchor.get("href"), base_url) if isinstance(anchor, Tag) else None
    if not url or _query_id(url, path=f"/mod/{module}/view.php") != cmid:
        return None
    name = str(activity.get("data-activityname", "")).strip()[:255]
    if not name:
        name = _visible_text(activity.select_one(".instancename, .activityname"))
    if not name:
        name = _visible_text(anchor)
    if not name:
        return None
    classes = {str(value).lower() for value in activity.get("class") or []}
    hidden = bool(classes.intersection({"hiddenactivity", "dimmed", "stealth"}))
    return {
        "cmid": int(cmid),
        "instance_id": 0,
        "module": module,
        "name": name,
        "visible": not hidden,
        "uservisible": not hidden,
        "url": url,
        "opens_at": 0,
        "due_at": 0,
        "cutoff_at": 0,
    }


def teacher_controls_present(html: str, base_url: str, course_id: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one("[data-key='editmode'], .editing_switch, input[name='setmode']"):
        return True
    allowed_paths = {
        "/course/edit.php",
        "/enrol/users.php",
        "/grade/edit/tree/index.php",
        "/grade/report/grader/index.php",
    }
    for anchor in soup.select("a[href]"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        parsed = urlsplit(url)
        if parsed.path.rstrip("/") not in allowed_paths:
            continue
        values = parse_qs(parsed.query, keep_blank_values=True).get("id", [])
        if values == [course_id]:
            return True
    return False


def grade_controls_present(html: str, base_url: str, course_id: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for anchor in soup.select("a[href*='/grade/report/grader/index.php']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if url and _query_id(url, path="/grade/report/grader/index.php") == course_id:
            return True
    return False


def parse_course_page(
    html: str,
    base_url: str,
    course_id: str,
    *,
    expected_section_record_id: int | None = None,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    if expected_section_record_id is not None:
        body = soup.body
        body_classes = (
            {str(value).lower() for value in body.get("class") or []}
            if isinstance(body, Tag)
            else set()
        )
        body_course_id = (
            str(body.get("data-courseid", "")).strip() if isinstance(body, Tag) else ""
        )
        explicit_body_courses = {
            match.group(1)
            for value in body_classes
            if (match := re.fullmatch(r"course-([1-9][0-9]{0,19})", value))
        }
        if (
            (body_course_id and body_course_id != course_id)
            or (explicit_body_courses and explicit_body_courses != {course_id})
            or (body_course_id != course_id and course_id not in explicit_body_courses)
        ):
            raise MoodleMarkupError("Moodle course section identifies another course")

    title = ""
    for selector in (
        ".page-header-headings h1",
        "#page-header h1",
        "header h1",
        "h1",
    ):
        title = _visible_text(soup.select_one(selector))
        if title:
            break
    if not title:
        raise MoodleMarkupError("Moodle course page has no title")

    section_nodes = soup.select(
        "li.course-section[data-sectionid], div.course-section[data-sectionid], "
        "section.course-section[data-sectionid], "
        "li.section.main[data-sectionid], li.section.main[id^='section-']"
    )
    if expected_section_record_id is not None:
        expected_record = str(expected_section_record_id)
        matching_sections = [
            node
            for node in section_nodes
            if str(node.get("data-id", "")).strip() == expected_record
        ]
        if len(matching_sections) != 1:
            raise MoodleMarkupError("Moodle course section response identifies another section")
        section_nodes = matching_sections
    sections: list[dict[str, Any]] = []
    seen_sections: set[str] = set()
    activity_count = 0
    for position, section in enumerate(section_nodes):
        section_id = _section_id(section, position)
        if section_id in seen_sections:
            continue
        seen_sections.add(section_id)
        section_title = ""
        for selector in (
            ".course-section-header .sectionname",
            ".sectionname",
            "h3",
        ):
            section_title = _visible_text(section.select_one(selector))
            if section_title:
                break
        if not section_title:
            section_title = str(section.get("data-sectionname", "")).strip()[:255]
        raw_number = str(section.get("data-number", section.get("data-sectionid", ""))).strip()
        section_title = section_title or (
            "Общее" if raw_number == "0" else f"Раздел {raw_number or position}"
        )
        activities: list[dict[str, Any]] = []
        for node in section.select(
            ".activity.activity-wrapper[data-id], .activity[id^='module-']"
        ):
            parsed = _activity(node, base_url)
            if parsed is None:
                continue
            activities.append(parsed)
            activity_count += 1
            if activity_count > 4_096:
                raise MoodleMarkupError("Moodle course has too many activities")
        classes = {str(value).lower() for value in section.get("class") or []}
        sections.append(
            {
                "external_id": section_id,
                "title": section_title[:255],
                "position": len(sections),
                "visible": not bool(classes.intersection({"hidden", "orphaned"})),
                "activities": activities,
            }
        )
        if len(sections) > 512:
            raise MoodleMarkupError("Moodle course has too many sections")

    short_name = ""
    for selector in (
        "[data-course-shortname]",
        "meta[name='course-shortname']",
    ):
        node = soup.select_one(selector)
        if node is not None:
            short_name = str(node.get("data-course-shortname", node.get("content", ""))).strip()[
                :120
            ]
            if short_name:
                break
    return {
        "external_id": course_id,
        "title": title,
        "short_name": short_name,
        "starts_at_epoch": 0,
        "ends_at_epoch": 0,
        "sections": sections,
        "teacher_controls": teacher_controls_present(html, base_url, course_id),
        "grade_controls": grade_controls_present(html, base_url, course_id),
    }


def _selected_value(form: Tag, name: str) -> str:
    control = form.select_one(f"[name='{name}']")
    if not isinstance(control, Tag):
        return ""
    if control.name == "select":
        selected = control.select_one("option[selected]") or control.select_one("option")
        return str(selected.get("value", "")).strip() if isinstance(selected, Tag) else ""
    return str(control.get("value", "")).strip()


def _enabled(form: Tag, prefix: str) -> bool:
    control = form.select_one(f"input[name='{prefix}[enabled]']")
    if not isinstance(control, Tag):
        return True
    return control.has_attr("checked")


def _checkbox_setting(form: Tag, name: str) -> bool | None:
    """Read one canonical Moodle checkbox without trusting its hidden fallback."""

    controls = form.select(f"[name='{name}']")
    if not controls:
        return None
    selects = [control for control in controls if control.name == "select"]
    if selects:
        if len(selects) != 1:
            return None
        selected_option = selects[0].select_one("option[selected]") or selects[0].select_one(
            "option"
        )
        selected_value = (
            str(selected_option.get("value", "")).strip().casefold()
            if isinstance(selected_option, Tag)
            else ""
        )
        if selected_value in {"1", "true", "on", "yes"}:
            return True
        if selected_value in {"", "0", "false", "off", "no"}:
            return False
        return None
    checkboxes = [
        control
        for control in controls
        if str(control.get("type", "")).casefold() in {"checkbox", "radio"}
    ]
    if checkboxes:
        return any(
            control.has_attr("checked") and str(control.get("value", "1")) != "0"
            for control in checkboxes
        )
    values = {str(control.get("value", "")).strip().casefold() for control in controls}
    if values <= {"", "0", "false", "off", "no"}:
        return False
    if values <= {"1", "true", "on", "yes"}:
        return True
    return None


def _positive_setting(form: Tag, name: str, *, maximum: int) -> int | None:
    raw = _selected_value(form, name)
    if raw.isdigit() and 0 < int(raw) <= maximum:
        return int(raw)
    return None


def _date_time_epoch(form: Tag, prefix: str) -> int:
    """Read Moodle's bounded date-time selector in the MMCS server timezone."""

    if not _enabled(form, prefix):
        return 0
    direct = _selected_value(form, prefix)
    if direct.isdigit() and 946_684_800 <= int(direct) <= 4_102_444_800:
        return int(direct)
    parts: list[int] = []
    for component in ("year", "month", "day", "hour", "minute"):
        raw = _selected_value(form, f"{prefix}[{component}]")
        if not raw or not re.fullmatch(r"-?[0-9]{1,4}", raw):
            return 0
        parts.append(int(raw))
    try:
        value = datetime(*parts, tzinfo=ZoneInfo("Europe/Moscow"))
    except (ValueError, OverflowError):
        return 0
    return int(value.timestamp())


def _date_time_evidence(form: Tag, prefix: str) -> tuple[int, bool]:
    """Return the date and whether Moodle explicitly proved its policy.

    A zero value is trustworthy only when the canonical enable control exists
    and is unchecked.  Missing or malformed controls are parser failure, not
    evidence that Moodle configured an unlimited window.
    """

    enabled = form.select_one(f"input[name='{prefix}[enabled]']")
    if isinstance(enabled, Tag) and not enabled.has_attr("checked"):
        return 0, True
    direct = _selected_value(form, prefix)
    if direct.isdigit() and 946_684_800 <= int(direct) <= 4_102_444_800:
        return int(direct), True
    value = _date_time_epoch(form, prefix)
    has_all_components = all(
        isinstance(form.select_one(f"[name='{prefix}[{component}]']"), Tag)
        for component in ("year", "month", "day", "hour", "minute")
    )
    return value, has_all_components and value > 0


def _override_date(form: Tag, prefix: str) -> tuple[int, bool]:
    """Read an optional Moodle override date without confusing inherit/zero."""

    enabled = form.select_one(f"input[name='{prefix}[enabled]']")
    has_direct = isinstance(form.select_one(f"[name='{prefix}']"), Tag)
    has_parts = all(
        isinstance(form.select_one(f"[name='{prefix}[{component}]']"), Tag)
        for component in ("year", "month", "day", "hour", "minute")
    )
    if not isinstance(enabled, Tag) and not has_direct and not has_parts:
        raise MoodleMarkupError(f"Moodle override field {prefix} is missing")
    overridden = enabled.has_attr("checked") if isinstance(enabled, Tag) else True
    if not overridden:
        return 0, False
    value, confirmed = _date_time_evidence(form, prefix)
    if not confirmed or value <= 0:
        raise MoodleMarkupError(f"Moodle override field {prefix} is invalid")
    return value, True


def parse_activity_user_override_index(
    html: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    module: str,
) -> list[dict[str, Any]]:
    """Return canonical user/override edit references from an override table."""

    if module not in {"assign", "quiz"} or cmid <= 0 or not _POSITIVE_ID.fullmatch(course_id):
        raise MoodleMarkupError("Moodle override request is invalid")
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    body_classes = {str(value).lower() for value in (body.get("class") or [])} if body else set()
    body_id = str(body.get("id", "")) if isinstance(body, Tag) else ""
    if body_id != f"page-mod-{module}-overrides":
        raise MoodleMarkupError("Moodle override page identifies another module")
    if f"cmid-{cmid}" not in body_classes:
        raise MoodleMarkupError("Moodle override page identifies another activity")
    # ``course-N`` is emitted by Moodle's page requirements independently of
    # the active theme.  Recent Moodle themes may omit the course breadcrumb
    # link from this admin-layout page, so requiring that presentation link
    # made a valid, empty override table look unconfirmed after an upgrade.
    course_proved = f"course-{course_id}" in body_classes or any(
        _query_id(url, path="/course/view.php") == course_id
        for anchor in soup.select("a[href]")
        if (url := _same_origin_url(anchor.get("href"), base_url)) is not None
    )
    if not course_proved:
        raise MoodleMarkupError("Moodle override page has another course context")

    result: list[dict[str, Any]] = []
    seen_users: set[str] = set()
    seen_overrides: set[int] = set()
    for user_anchor in soup.select("a[href*='/user/view.php']"):
        user_url = _same_origin_url(user_anchor.get("href"), base_url)
        user_id = _query_id(user_url, path="/user/view.php") if user_url else None
        row = user_anchor.find_parent("tr")
        if not user_id or not isinstance(row, Tag):
            continue
        edit_anchor = row.select_one(f"a[href*='/mod/{module}/overrideedit.php']")
        edit_url = (
            _same_origin_url(edit_anchor.get("href"), base_url)
            if isinstance(edit_anchor, Tag)
            else None
        )
        override_id = (
            _query_id(edit_url, path=f"/mod/{module}/overrideedit.php") if edit_url else None
        )
        if not override_id or not _POSITIVE_ID.fullmatch(override_id):
            continue
        numeric_override_id = int(override_id)
        if user_id in seen_users or numeric_override_id in seen_overrides:
            raise MoodleMarkupError("Moodle override table contains duplicate targets")
        display_name = _text(user_anchor)
        if not display_name:
            raise MoodleMarkupError("Moodle override user name is missing")
        seen_users.add(user_id)
        seen_overrides.add(numeric_override_id)
        result.append(
            {
                "override_id": numeric_override_id,
                "user_id": user_id,
                "display_name": display_name,
                "edit_url": edit_url,
            }
        )
        if len(result) > 256:
            raise MoodleMarkupError("Moodle activity has too many user overrides")
    return result


def parse_activity_user_override_edit(
    html: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    module: str,
    override_id: int,
    user_id: str,
    display_name: str,
) -> dict[str, Any]:
    """Parse exact machine values from one canonical Moodle override form."""

    if (
        module not in {"assign", "quiz"}
        or cmid <= 0
        or override_id <= 0
        or not _POSITIVE_ID.fullmatch(course_id)
        or not _POSITIVE_ID.fullmatch(user_id)
    ):
        raise MoodleMarkupError("Moodle override edit request is invalid")
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form.mform, form[id^='mform']")
    if not isinstance(form, Tag):
        raise MoodleMarkupError("Moodle override edit form is missing")
    body = soup.body
    body_classes = {str(value).lower() for value in (body.get("class") or [])} if body else set()
    if (
        f"path-mod-{module}" not in body_classes
        or f"cmid-{cmid}" not in body_classes
        or f"course-{course_id}" not in body_classes
    ):
        raise MoodleMarkupError("Moodle override edit form identifies another activity")
    form_cmid = _selected_value(form, "cmid") or _selected_value(form, "coursemodule")
    form_override = _selected_value(form, "id")
    action_url = _same_origin_url(form.get("action"), base_url)
    action_override = (
        _query_id(action_url, path=f"/mod/{module}/overrideedit.php") if action_url else None
    )
    form_user = _selected_value(form, "userid") or _selected_value(form, "userid[value]")
    override_evidence = [value for value in (form_override, action_override) if value]
    if (
        (form_cmid and form_cmid != str(cmid))
        or not override_evidence
        or any(value != str(override_id) for value in override_evidence)
    ):
        raise MoodleMarkupError("Moodle override edit form identifies another override")
    if form_user and form_user != user_id:
        raise MoodleMarkupError("Moodle override edit form identifies another user")

    open_prefix = "allowsubmissionsfromdate" if module == "assign" else "timeopen"
    due_prefix = "duedate" if module == "assign" else "timeclose"
    opens_at, opens_overridden = _override_date(form, open_prefix)
    due_at, due_overridden = _override_date(form, due_prefix)
    cutoff_at, cutoff_overridden = (
        _override_date(form, "cutoffdate") if module == "assign" else (0, False)
    )
    result: dict[str, Any] = {
        "override_id": override_id,
        "user_id": user_id,
        "display_name": display_name[:255],
        "opens_at": opens_at,
        "due_at": due_at,
        "cutoff_at": cutoff_at,
        "opens_at_overridden": opens_overridden,
        "due_at_overridden": due_overridden,
        "cutoff_at_overridden": cutoff_overridden,
        "duration_overridden": False,
        "attempt_limit_overridden": False,
        "attempt_limit_unlimited": False,
        "confirmed": True,
    }
    if module == "quiz":
        duration_enabled = form.select_one("input[name='timelimit[enabled]']")
        duration_number = form.select_one("[name='timelimit[number]']")
        duration_unit = form.select_one("[name='timelimit[timeunit]']")
        if not all(
            isinstance(control, Tag)
            for control in (duration_enabled, duration_number, duration_unit)
        ):
            raise MoodleMarkupError("Moodle override duration field is missing")
        duration_overridden = duration_enabled.has_attr("checked")
        result["duration_overridden"] = duration_overridden
        if duration_overridden:
            amount = _positive_float(_selected_value(form, "timelimit[number]"))
            unit = _selected_value(form, "timelimit[timeunit]")
            factors = {"1": 1, "60": 60, "3600": 3600, "86400": 86400}
            if amount is None or unit not in factors:
                raise MoodleMarkupError("Moodle override duration value is invalid")
            result["duration_seconds"] = min(int(amount * factors[unit]), 31_536_000)
        attempts_enabled = form.select_one("input[name='attempts[enabled]']")
        attempts_control = form.select_one("[name='attempts']")
        if not isinstance(attempts_enabled, Tag) and not isinstance(attempts_control, Tag):
            raise MoodleMarkupError("Moodle override attempts field is missing")
        attempts_overridden = (
            attempts_enabled.has_attr("checked") if isinstance(attempts_enabled, Tag) else True
        )
        result["attempt_limit_overridden"] = attempts_overridden
        if attempts_overridden:
            attempts = _selected_value(form, "attempts")
            if attempts == "0":
                result["attempt_limit_unlimited"] = True
            elif attempts.isdigit() and 0 < int(attempts) <= 100:
                result["attempt_limit"] = int(attempts)
            else:
                raise MoodleMarkupError("Moodle override attempts value is invalid")
    return result


def _plain_rich_text(raw: str, *, maximum: int) -> str:
    """Render a bounded Moodle HTML editor value as readable plain text.

    Moodle stores editor content as HTML inside a textarea.  A whitespace-wide
    regex loses paragraph boundaries and list items, which made imported task
    statements unreadable.  Converting structural separators to newlines first
    keeps those boundaries without exposing HTML in the connector contract.
    """

    fragment = BeautifulSoup(raw.replace("\xa0", " "), "html.parser")
    for line_break in fragment.find_all("br"):
        line_break.replace_with("\n")
    for block in fragment.find_all(
        (
            "address",
            "article",
            "blockquote",
            "div",
            "figcaption",
            "figure",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "li",
            "main",
            "p",
            "pre",
            "section",
            "tr",
        )
    ):
        block.insert_before("\n")
        block.insert_after("\n")
    text = fragment.get_text("", strip=False).replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WHITESPACE.sub(" ", line).strip() for line in text.split("\n")]
    result: list[str] = []
    for line in lines:
        if line:
            result.append(line)
        elif result and result[-1] != "":
            result.append("")
    while result and result[-1] == "":
        result.pop()
    return "\n".join(result).strip()[:maximum]


def _plain_editor_text(
    form: Tag,
    *names: str,
    maximum: int = 50_000,
) -> str:
    selectors: list[str] = []
    for name in names:
        selectors.extend((f"textarea[name='{name}[text]']", f"textarea[name='{name}']"))
    control = form.select_one(", ".join(selectors)) if selectors else None
    if not isinstance(control, Tag):
        return ""
    return _plain_rich_text(control.get_text("", strip=False), maximum=maximum)


def _positive_float(value: str) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _grade_max(form: Tag, module: str) -> float | None:
    """Read Moodle's module-specific positive numeric maximum.

    Modern assignments use a compound ``grade[...]`` control.  Its scale value
    is a database id, not a numeric maximum, so it must never be treated as a
    score.  Quizzes and older forms still expose one numeric ``grade`` control.
    """

    grade_type = _selected_value(form, "grade[modgrade_type]").lower()
    if module == "assign" and grade_type:
        if grade_type in {"point", "1"}:
            return _positive_float(_selected_value(form, "grade[modgrade_point]"))
        return None
    compound = _positive_float(_selected_value(form, "grade[modgrade_point]"))
    return compound if compound is not None else _positive_float(_selected_value(form, "grade"))


def parse_activity_settings(
    html: str,
    *,
    course_id: str,
    cmid: int,
    module: str,
) -> dict[str, Any]:
    """Parse the read-only part of a canonical Moodle activity edit form.

    Discovery performs only GET requests.  Stable hidden ids must bind the
    response to the requested course, course-module and module type before any
    text or schedule is accepted.
    """

    if module not in {"assign", "quiz"} or cmid <= 0 or not _POSITIVE_ID.fullmatch(course_id):
        raise MoodleMarkupError("Moodle activity settings request is invalid")
    soup = BeautifulSoup(html, "html.parser")
    form = soup.select_one("form.mform, form[id^='mform']")
    if not isinstance(form, Tag):
        raise MoodleMarkupError("Moodle activity settings form is missing")
    form_cmid = _selected_value(form, "coursemodule") or _selected_value(form, "update")
    form_course = _selected_value(form, "course") or str(
        (soup.body or {}).get("data-courseid", "")
    )
    form_module = _selected_value(form, "modulename").lower()
    body_classes = (
        {str(value).lower() for value in (soup.body.get("class") or [])}
        if isinstance(soup.body, Tag)
        else set()
    )
    if form_cmid != str(cmid) or form_course != course_id:
        raise MoodleMarkupError("Moodle activity settings identify another activity")
    if form_module and form_module != module:
        raise MoodleMarkupError("Moodle activity settings identify another module")
    if not form_module and f"path-mod-{module}" not in body_classes:
        raise MoodleMarkupError("Moodle activity settings have no module evidence")

    instance = _selected_value(form, "instance")
    grade = _grade_max(form, module)
    open_prefix = "allowsubmissionsfromdate" if module == "assign" else "timeopen"
    due_prefix = "duedate" if module == "assign" else "timeclose"
    opens_at, opens_confirmed = _date_time_evidence(form, open_prefix)
    due_at, due_confirmed = _date_time_evidence(form, due_prefix)
    cutoff_at, cutoff_confirmed = (
        _date_time_evidence(form, "cutoffdate") if module == "assign" else (0, True)
    )
    description = _plain_editor_text(form, "introeditor", "intro")
    result: dict[str, Any] = {
        "description": description,
        "instance_id": int(instance) if _POSITIVE_ID.fullmatch(instance) else 0,
        "opens_at": opens_at,
        "due_at": due_at,
        "cutoff_at": cutoff_at,
        "settings_confirmed": True,
        "statement_confirmed": module == "assign" and bool(description),
        "schedule_confirmed": opens_confirmed and due_confirmed and cutoff_confirmed,
        "duration_confirmed": module == "assign",
        "grade_confirmed": grade is not None,
        "attempt_policy_confirmed": False,
    }
    if grade is not None:
        result["grade_max"] = grade
    if module == "assign":
        reopen_method = _selected_value(form, "attemptreopenmethod").lower()
        maximum_attempts = _selected_value(form, "maxattempts")
        if reopen_method == "none":
            result["attempt_limit"] = 1
            result["attempt_policy_confirmed"] = True
        elif reopen_method in {"manual", "untilpass"}:
            if maximum_attempts == "-1":
                result["attempt_limit_unlimited"] = True
                result["attempt_policy_confirmed"] = True
            elif maximum_attempts.isdigit() and 0 < int(maximum_attempts) <= 100:
                result["attempt_limit"] = int(maximum_attempts)
                result["attempt_policy_confirmed"] = True

        online_text = _checkbox_setting(form, "assignsubmission_onlinetext_enabled")
        file_submission = _checkbox_setting(form, "assignsubmission_file_enabled")
        transports: list[str] = []
        if online_text is True:
            transports.append("ASSIGN_ONLINE_TEXT")
        if file_submission is True:
            transports.append("ASSIGN_FILE")
        if online_text is not None and file_submission is not None:
            result["available_answer_transports"] = transports
            # File submission preserves multi-file work. Prefer it when Moodle
            # explicitly enables both standard Assignment plugins.
            if transports:
                result["answer_transport"] = (
                    "ASSIGN_FILE" if "ASSIGN_FILE" in transports else "ASSIGN_ONLINE_TEXT"
                )

        drafts = _checkbox_setting(form, "submissiondrafts")
        if drafts is not None:
            result["submission_drafts"] = drafts
        statement = _checkbox_setting(form, "requiresubmissionstatement")
        if statement is not None:
            result["requires_submission_statement"] = statement
        team_submission = _checkbox_setting(form, "teamsubmission")
        if team_submission is not None:
            result["team_submission"] = team_submission
        max_files = _positive_setting(form, "assignsubmission_file_maxfiles", maximum=128)
        if max_files is not None:
            result["max_submission_files"] = max_files
        max_bytes = _positive_setting(
            form,
            "assignsubmission_file_maxsizebytes",
            maximum=4 * 1024 * 1024 * 1024,
        )
        if max_bytes is not None:
            result["max_submission_bytes"] = max_bytes
        elif _selected_value(form, "assignsubmission_file_maxsizebytes") == "0":
            result["max_submission_bytes_inherited"] = True
        file_types_name = "assignsubmission_file_filetypes[filetypes]"
        file_types_control = form.select_one(f"[name='{file_types_name}']")
        if not isinstance(file_types_control, Tag):
            file_types_name = "assignsubmission_file_filetypes"
            file_types_control = form.select_one(f"[name='{file_types_name}']")
        file_types = _selected_value(form, file_types_name)[:2_000]
        if isinstance(file_types_control, Tag):
            result["accepted_file_types"] = file_types
            result["file_types_confirmed"] = True

        # The settings form proves configuration; the browser service proves
        # the actual student's writable form again immediately before upload.
        if online_text is not None and file_submission is not None:
            file_contract_confirmed = "ASSIGN_FILE" not in transports or (
                max_files is not None and isinstance(file_types_control, Tag)
            )
            result["import_supported"] = bool(
                transports
                and drafts is not None
                and statement is not None
                and team_submission is False
                and file_contract_confirmed
            )
    else:
        attempt_policy_confirmed = False
        attempts = _selected_value(form, "attempts")
        if attempts == "0":
            result["attempt_limit_unlimited"] = True
            attempt_policy_confirmed = True
        elif attempts.isdigit() and 0 < int(attempts) <= 100:
            result["attempt_limit"] = int(attempts)
            attempt_policy_confirmed = True
    if module == "quiz":
        grading_methods = {
            "1": "HIGHEST",
            "2": "AVERAGE",
            "3": "FIRST",
            "4": "LAST",
        }
        grading_method = grading_methods.get(_selected_value(form, "grademethod"))
        result["quiz_grading_method_confirmed"] = grading_method is not None
        if grading_method is not None:
            result["quiz_grading_method"] = grading_method
        # The number of attempts and the method Moodle uses to aggregate them
        # form one policy.  Treating only the limit as authoritative could let
        # the application grade the latest attempt while Moodle's gradebook
        # keeps the highest, average or first attempt instead.
        result["attempt_policy_confirmed"] = bool(
            attempt_policy_confirmed and grading_method is not None
        )
        amount = _positive_float(_selected_value(form, "timelimit[number]"))
        unit = _selected_value(form, "timelimit[timeunit]")
        factors = {"1": 1, "60": 60, "3600": 3600, "86400": 86400}
        enabled_control = form.select_one("input[name='timelimit[enabled]']")
        if isinstance(enabled_control, Tag) and not enabled_control.has_attr("checked"):
            result["duration_confirmed"] = True
        elif _enabled(form, "timelimit") and amount is not None and unit in factors:
            result["duration_seconds"] = min(int(amount * factors[unit]), 31_536_000)
            result["duration_confirmed"] = True
    return result


_QUIZ_QUESTION_EDIT_PATHS = {
    "/question/bank/editquestion/question.php",
    "/question/question.php",
}
_QUIZ_QUESTION_PREVIEW_PATHS = {
    "/question/bank/previewquestion/preview.php",
    "/question/preview.php",
}


def _quiz_essay_edit_reference(
    slot: Tag,
    *,
    base_url: str,
    cmid: int,
) -> tuple[str, str, str] | None:
    """Return one origin-bound Essay editor GET target from a quiz slot.

    Moodle 5 renders the question name as an edit link only when the current
    teacher can *edit* the question. A teacher with the normal ``use``/view
    capability still receives a preview link, even though the same question
    form is available read-only. Deriving the canonical read-only editor URL
    from that origin-bound preview reference keeps discovery GET-only and
    avoids treating a perfectly usable Essay question as statement-less.
    """

    candidates: list[tuple[str, str, str]] = []
    preview_question_ids: set[str] = set()
    for anchor in slot.select("a[href]"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        parsed = urlsplit(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("cmid") != [str(cmid)]:
            continue
        query_name = "id" if query.get("id") else "questionid"
        question_ids = query.get(query_name, [])
        if len(question_ids) != 1 or not _POSITIVE_ID.fullmatch(question_ids[0]):
            continue
        path = parsed.path.rstrip("/")
        if path in _QUIZ_QUESTION_EDIT_PATHS:
            candidates.append((url, question_ids[0], query_name))
        elif path in _QUIZ_QUESTION_PREVIEW_PATHS:
            preview_question_ids.add(question_ids[0])
    unique = list(dict.fromkeys(candidates))
    if len(unique) == 1:
        return unique[0]
    if unique or len(preview_question_ids) != 1:
        return None
    question_id = next(iter(preview_question_ids))
    editor_url = f"{base_url}/question/bank/editquestion/question.php?" + urlencode(
        {"cmid": cmid, "id": question_id}
    )
    return editor_url, question_id, "id"


def parse_quiz_question_summary(
    html: str,
    *,
    course_id: str,
    cmid: int,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Confirm that a quiz edit page contains exactly one Essay slot."""

    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    body_classes = (
        {str(value).lower() for value in body.get("class") or []}
        if isinstance(body, Tag)
        else set()
    )
    course_evidence = str(body.get("data-courseid", "")) if isinstance(body, Tag) else ""
    if not course_evidence:
        course_evidence = course_id if f"course-{course_id}" in body_classes else ""
    cmid_controls = {
        str(node.get("value", "")).strip()
        for node in soup.select("input[name='cmid'][value], input[name='coursemodule'][value]")
    }
    if course_evidence != course_id or str(cmid) not in cmid_controls:
        raise MoodleMarkupError("Moodle quiz question page identifies another quiz")

    slots = soup.select(".slots .slot, li.slot, [data-region='slot'][data-slot]")
    # Some older themes omit the slots wrapper but retain one data-slot per row.
    if not slots:
        slots = soup.select("[data-slot][data-questionid]")
    unique: dict[str, Tag] = {}
    for index, slot in enumerate(slots):
        key = str(slot.get("data-slot", "")).strip() or str(slot.get("id", "")).strip()
        unique.setdefault(key or f"position-{index}", slot)
    essay_count = 0
    random_count = 0
    essay_slot: Tag | None = None
    random_slot: Tag | None = None
    for slot in unique.values():
        random_markers = slot.select("[data-action='editrandomquestion']")
        if random_markers:
            random_count += 1
            random_slot = slot
        marker = " ".join(str(value).lower() for value in slot.get("class") or [])
        marker += " " + str(slot.get("data-question-type", slot.get("data-qtype", ""))).lower()
        marker += " " + " ".join(
            str(node.get("src", "")).lower() + " " + str(node.get("title", "")).lower()
            for node in slot.select("img[src], [data-question-type], [data-qtype]")
        )
        if re.search(r"(?:qtype[/_-]?essay|question[-_ ]?type[-_ ]?essay|\bessay\b|эссе)", marker):
            essay_count += 1
            essay_slot = slot
    result: dict[str, Any] = {
        "question_count": len(unique),
        "essay_question_count": essay_count,
        "random_question_count": random_count,
        "random_essay_confirmed": False,
        "statement_deferred": False,
        "import_supported": len(unique) == 1 and essay_count == 1 and random_count == 0,
    }
    if result["import_supported"] and essay_slot is not None and base_url:
        reference = _quiz_essay_edit_reference(essay_slot, base_url=base_url, cmid=cmid)
        if reference is not None:
            (
                result["_essay_edit_url"],
                result["_essay_question_id"],
                result["_essay_question_query_name"],
            ) = reference
    if len(unique) == 1 and random_count == 1 and random_slot is not None and base_url:
        references: list[str] = []
        for anchor in random_slot.select("a.mod_quiz_random_qbank_link[href]"):
            url = _same_origin_url(anchor.get("href"), base_url)
            if not url:
                continue
            parsed = urlsplit(url)
            query = parse_qs(parsed.query, keep_blank_values=True)
            cmids = query.get("cmid", [])
            filters = query.get("filter", [])
            if (
                parsed.path.rstrip("/") != "/question/edit.php"
                or len(cmids) != 1
                or not _POSITIVE_ID.fullmatch(cmids[0])
                or len(filters) != 1
                or not filters[0]
                or len(filters[0]) > 8_192
            ):
                continue
            try:
                filter_payload = json.loads(filters[0])
            except (TypeError, ValueError):
                continue
            if not isinstance(filter_payload, dict):
                continue
            references.append(url)
        unique_references = list(dict.fromkeys(references))
        if len(unique_references) == 1:
            result["_random_qbank_url"] = unique_references[0]
    if 1 < len(unique) <= 32 and base_url:
        questions: list[dict[str, Any]] = []
        for slot in unique.values():
            slot_id = str(slot.get("data-slot", "")).strip()
            if not slot_id:
                match = re.fullmatch(r"slot-([0-9]+)", str(slot.get("id", "")))
                slot_id = match.group(1) if match else ""
            if not _POSITIVE_ID.fullmatch(slot_id):
                break
            # Reuse the same origin-bound Essay/random reference validation
            # per slot; synthetic context uses identifiers proven above.
            child = parse_quiz_question_summary(
                f'<body class="course-{course_id}"><input name="cmid" value="{cmid}">'
                f'<ul class="slots">{slot}</ul></body>',
                course_id=course_id,
                cmid=cmid,
                base_url=base_url,
            )
            if not child.get("_essay_edit_url") and not child.get("_random_qbank_url"):
                break
            questions.append({"question_slot": slot_id, **child})
        if len(questions) == len(unique) and len({q["question_slot"] for q in questions}) == len(
            unique
        ):
            result["_questions"] = questions
    return result


def parse_quiz_random_question_bank(
    html: str,
    *,
    base_url: str,
    course_id: str,
) -> dict[str, Any]:
    """Prove that one bounded random-question bank page contains only Essay rows.

    Random quiz slots do not expose a concrete question before Moodle creates a
    student attempt.  Discovery may nevertheless permit a deferred statement
    when the slot's own question-bank link leads to a complete, non-empty page
    whose every rendered question row is explicitly typed as Essay.  Any
    pagination evidence fails closed because unvisited rows could have another
    type.
    """

    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    body_classes = (
        {str(value).lower() for value in body.get("class") or []}
        if isinstance(body, Tag)
        else set()
    )
    course_evidence = str(body.get("data-courseid", "")) if isinstance(body, Tag) else ""
    if not course_evidence:
        course_evidence = course_id if f"course-{course_id}" in body_classes else ""
    if not course_evidence:
        for anchor in soup.select("a[href*='/course/view.php']"):
            url = _same_origin_url(anchor.get("href"), base_url)
            if not url or urlsplit(url).path.rstrip("/") != "/course/view.php":
                continue
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            if query.get("id") == [course_id]:
                course_evidence = course_id
                break
    if course_evidence != course_id:
        raise MoodleMarkupError("Moodle question bank identifies another course")

    tables = soup.select("table#categoryquestions.question-bank-table")
    if len(tables) != 1:
        raise MoodleMarkupError("Moodle question bank table is missing or ambiguous")
    table = tables[0]
    type_cells = table.select("tbody tr td.qtype")
    if not type_cells:
        type_cells = table.select(
            "tbody tr td[data-columnid*='question_type_column'], "
            "tbody tr [data-region='question-type']"
        )
    if not type_cells:
        raise MoodleMarkupError("Moodle random question bank contains no typed questions")

    for anchor in soup.select("a[href*='qpage=']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        pages = parse_qs(urlsplit(url).query, keep_blank_values=True).get("qpage", [])
        if len(pages) == 1 and pages[0].isdigit() and int(pages[0]) > 0:
            raise MoodleMarkupError("Moodle random question bank is paginated")
    for node in soup.select("[data-totalpages], [data-page-count]"):
        raw = str(node.get("data-totalpages", node.get("data-page-count", ""))).strip()
        if raw.isdigit() and int(raw) > 1:
            raise MoodleMarkupError("Moodle random question bank is paginated")
    paging_regions = list(
        soup.select(
            ".paging, .paging-bar, .pagination, "
            "[data-region='paging-control-container'], [data-region='paging-bar']"
        )
    )
    paging_regions.extend(
        node
        for node in soup.select("[aria-label]")
        if re.search(
            r"(?:pagination|page\s+navigation|навигац\w*\s+по\s+страниц|страниц\w*)",
            str(node.get("aria-label", "")),
            re.I,
        )
    )
    if any(
        region.select_one("a[href], button:not([disabled]), [data-page], [data-page-number]")
        is not None
        for region in paging_regions
    ):
        raise MoodleMarkupError("Moodle random question bank is paginated")

    essay_count = 0
    for cell in type_cells:
        marker_parts = [
            " ".join(str(value) for value in cell.get("class") or []),
            str(cell.get("data-question-type", "")),
            str(cell.get("data-qtype", "")),
            _text(cell, limit=255),
        ]
        for node in cell.select("img, [data-question-type], [data-qtype]"):
            marker_parts.extend(
                [
                    str(node.get("src", "")),
                    str(node.get("alt", "")),
                    str(node.get("title", "")),
                    str(node.get("data-question-type", "")),
                    str(node.get("data-qtype", "")),
                ]
            )
        marker = " ".join(marker_parts).lower()
        if re.search(
            r"(?:qtype[/_-]?essay|question[/_-]?type[/_-]?essay|\bessay\b|эссе)",
            marker,
        ):
            essay_count += 1

    return {
        "question_count": len(type_cells),
        "essay_question_count": essay_count,
        "all_essay": essay_count == len(type_cells),
        "complete": True,
    }


def parse_quiz_essay_question_edit(
    html: str,
    *,
    course_id: str,
    cmid: int,
    question_id: str,
) -> dict[str, str]:
    """Read the statement of one origin-checked Essay question edit form.

    The caller obtains this page with a GET from the single quiz slot's own edit
    link.  Requiring course, activity, question and Essay evidence prevents a
    redirected or unrelated editor page from becoming a task statement.
    """

    if (
        cmid <= 0
        or not _POSITIVE_ID.fullmatch(course_id)
        or not _POSITIVE_ID.fullmatch(question_id)
    ):
        raise MoodleMarkupError("Moodle Essay question request is invalid")
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    body_classes = (
        {str(value).lower() for value in body.get("class") or []}
        if isinstance(body, Tag)
        else set()
    )
    form: Tag | None = None
    for candidate in soup.select("form.mform, form[id^='mform']"):
        candidate_cmid = _selected_value(candidate, "cmid") or _selected_value(
            candidate, "coursemodule"
        )
        candidate_id = _selected_value(candidate, "id") or _selected_value(candidate, "questionid")
        if candidate_cmid == str(cmid) and candidate_id == question_id:
            form = candidate
            break
    if not isinstance(form, Tag):
        raise MoodleMarkupError("Moodle Essay question form is missing or identifies another item")

    course_evidence = _selected_value(form, "courseid")
    if not course_evidence and isinstance(body, Tag):
        course_evidence = str(body.get("data-courseid", ""))
    if not course_evidence and f"course-{course_id}" in body_classes:
        course_evidence = course_id
    if course_evidence != course_id:
        raise MoodleMarkupError("Moodle Essay question form identifies another course")

    qtype = _selected_value(form, "qtype").lower()
    essay_class_evidence = any(
        marker in body_classes
        for marker in ("path-question-type-essay", "qtype-essay", "qtype_essay")
    )
    if qtype != "essay" and not essay_class_evidence:
        raise MoodleMarkupError("Moodle question form is not an Essay")

    result = {
        "description": _plain_editor_text(
            form,
            "questiontexteditor",
            "questiontext",
        ),
    }
    result["statement_confirmed"] = bool(result["description"])
    response_format = _selected_value(form, "responseformat").lower()
    attachments = _selected_value(form, "attachments")
    if response_format in {"editor", "editorfilepicker", "plain", "monospaced"}:
        result["answer_transport"] = "ESSAY_ONLINE_TEXT"
    elif response_format == "noinline" and attachments not in {"", "0"}:
        result["answer_transport"] = "ESSAY_ATTACHMENT"
    return result


def _header_indexes(table: Tag) -> dict[str, int]:
    result: dict[str, int] = {}
    for index, header in enumerate(table.select("thead tr th")):
        value = _text(header).lower()
        if any(word in value for word in ("role", "рол")):
            result["role"] = index
        elif any(word in value for word in ("group", "групп")):
            result["group"] = index
        elif "email" in value or "почт" in value:
            result["email"] = index
    return result


def _role_values(value: str) -> list[str]:
    normalized = _WHITESPACE.sub(" ", value).strip().lower()
    roles: list[str] = []
    if any(word in normalized for word in _TEACHER_WORDS):
        roles.append("TEACHER")
    if any(word in normalized for word in _STUDENT_WORDS):
        roles.append("STUDENT")
    return roles


def _group_values(cell: Tag | None) -> list[dict[str, str]]:
    if cell is None:
        return []
    groups: list[dict[str, str]] = []
    seen: set[str] = set()
    nodes = cell.select("a") or [cell]
    for node in nodes:
        name = _visible_text(node)
        if not name or name.lower() in {"-", "no groups", "нет групп", "без групп"}:
            continue
        raw_id = ""
        href = str(node.get("href", "")) if isinstance(node, Tag) else ""
        if href:
            values = parse_qs(urlsplit(href).query, keep_blank_values=True).get("group", [])
            if len(values) == 1 and _POSITIVE_ID.fullmatch(values[0]):
                raw_id = values[0]
        external_id = raw_id or f"name-{hashlib.sha256(name.encode()).hexdigest()[:16]}"
        if external_id not in seen:
            seen.add(external_id)
            groups.append({"external_id": external_id, "name": name[:255]})
    return groups[:128]


def parse_participants_page(
    html: str,
    base_url: str,
    course_id: str,
) -> ParsedParticipants:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table#participants")
    if table is None:
        return ParsedParticipants(
            members=[], has_next=False, table_present=False, all_rows_classified=False
        )
    indexes = _header_indexes(table)
    members: list[dict[str, Any]] = []
    seen: set[str] = set()
    all_rows_classified = True
    for row in table.select("tbody tr"):
        profile: Tag | None = None
        user_id = ""
        for anchor in row.select("a[href*='/user/view.php'], a[href*='/user/profile.php']"):
            url = _same_origin_url(anchor.get("href"), base_url)
            if not url:
                continue
            for path in ("/user/view.php", "/user/profile.php"):
                candidate = _query_id(url, path=path)
                if candidate:
                    user_id = candidate
                    profile = anchor
                    break
            if user_id:
                break
        if not user_id or user_id in seen or profile is None:
            continue
        display_name = _visible_text(profile)
        if not display_name:
            continue
        cells = row.find_all(["th", "td"], recursive=False)
        role_index = indexes.get("role", -1)
        role_cell = cells[role_index] if 0 <= role_index < len(cells) else None
        if role_cell is None:
            role_cell = row.select_one("td.c2")
        roles = _role_values(_visible_text(role_cell, limit=1_024))
        if not roles:
            all_rows_classified = False
            continue
        group_index = indexes.get("group", -1)
        group_cell = cells[group_index] if 0 <= group_index < len(cells) else None
        if group_cell is None:
            group_cell = row.select_one("td.c3")
        email = ""
        email_link = row.select_one("a[href^='mailto:']")
        if email_link is not None:
            candidate = str(email_link.get("href", ""))[7:].split("?", 1)[0].strip()
            if "@" in candidate and len(candidate) <= 254:
                email = candidate
        elif 0 <= indexes.get("email", -1) < len(cells):
            candidate = _visible_text(cells[indexes["email"]], limit=254)
            if "@" in candidate:
                email = candidate
        row_text = _text(row, limit=4_096).lower()
        suspended = "suspended" in row_text or "приостанов" in row_text
        seen.add(user_id)
        members.append(
            {
                "user_id": user_id,
                "display_name": display_name,
                "email": email,
                "suspended": suspended,
                "role": "TEACHER" if "TEACHER" in roles else "STUDENT",
                "roles": roles,
                "groups": _group_values(group_cell),
            }
        )

    has_next = False
    for anchor in soup.select(
        "a[rel='next'][href], .pagination .page-item:not(.disabled) a[href], "
        "nav[aria-label] a[href]"
    ):
        label = " ".join(
            (
                str(anchor.get("aria-label", "")),
                str(anchor.get("title", "")),
                _text(anchor),
            )
        ).lower()
        if not any(word in label for word in ("next", "след", "далее", "»")):
            continue
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        parsed = urlsplit(url)
        values = parse_qs(parsed.query, keep_blank_values=True)
        if parsed.path.rstrip("/") == "/user/index.php" and values.get("id") == [course_id]:
            has_next = True
            break
    return ParsedParticipants(
        members=members,
        has_next=has_next,
        table_present=True,
        all_rows_classified=all_rows_classified,
    )
