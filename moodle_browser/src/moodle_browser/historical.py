from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, unquote, urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag

from .config import exact_https_origin
from .parsers import MoodleMarkupError, canonical_hash

_POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_WHITESPACE = re.compile(r"\s+")
_NUMBER = r"([0-9]+(?:[.,][0-9]+)?)"
_GRADE_PAIR = re.compile(
    rf"{_NUMBER}\s*(?:/|из|out\s+of)\s*{_NUMBER}",
    re.IGNORECASE,
)
_GRADE_VALUE = re.compile(
    rf"(?:балл|оценка|grade|mark(?:ed)?)\s*:?\s*{_NUMBER}",
    re.IGNORECASE,
)
_REVIEWER_SIGNATURE = re.compile(
    r"^(?:[A-ZА-ЯЁ][A-Za-zА-Яа-яЁё'`’-]{1,99})"
    r"(?:\s+(?:[A-ZА-ЯЁ]\.\s*){1,3})$"
)
_BLOCK_ELEMENTS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
_SKIPPED_TEXT_ELEMENTS = frozenset({"button", "input", "noscript", "script", "style"})
_RU_MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}


@dataclass(frozen=True, slots=True)
class HistoricalIndexPage:
    items: list[dict[str, Any]]
    has_next: bool
    skipped_rows: int = 0


def _text(node: Tag | None, *, maximum: int = 255) -> str:
    if node is None:
        return ""
    return _WHITESPACE.sub(" ", node.get_text(" ", strip=True)).strip()[:maximum]


def _trim_empty_boundary_lines(value: str, *, maximum: int) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")
    lines = value.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)[:maximum]


def _render_rich_text(node: Tag, *, maximum: int) -> str:
    """Render Moodle rich text without breaking inline code spans.

    ``BeautifulSoup.get_text("\\n")`` inserts a separator between *all* text
    nodes.  Moodle's editors commonly wrap syntax fragments in inline spans, so
    that behaviour can turn ``std::cin`` into several source lines.  This small
    renderer adds line boundaries only for actual block elements and ``<br>``.
    """

    preformatted = node if node.name == "pre" else node.select_one("pre")
    if isinstance(preformatted, Tag):
        return _trim_empty_boundary_lines(
            preformatted.get_text("", strip=False),
            maximum=maximum,
        )

    parts: list[str] = []

    def newline() -> None:
        if parts and not parts[-1].endswith("\n"):
            parts.append("\n")

    def visit(value: Tag | NavigableString) -> None:
        if isinstance(value, NavigableString):
            parts.append(str(value))
            return
        name = (value.name or "").lower()
        if name in _SKIPPED_TEXT_ELEMENTS:
            return
        classes = {str(item) for item in value.get("class") or []}
        if classes.intersection({"attachments", "commentlink", "filemanager", "files"}):
            return
        if name == "br":
            newline()
            return
        is_block = name in _BLOCK_ELEMENTS
        if is_block:
            newline()
        for child in value.children:
            if isinstance(child, Tag | NavigableString):
                visit(child)
        if is_block:
            newline()

    visit(node)
    return _trim_empty_boundary_lines("".join(parts), maximum=maximum)


def _multiline_text(node: Tag | None, *, maximum: int = 1_000_000) -> str:
    if node is None:
        return ""
    return _render_rich_text(node, maximum=maximum)


def _answer_text(node: Tag | None, *, maximum: int = 1_000_000) -> str:
    if node is None:
        return ""
    if node.name == "pre" or node.select_one("pre") is not None:
        return _render_rich_text(node, maximum=maximum)
    paragraphs = node.select("p")
    if not paragraphs:
        return _render_rich_text(node, maximum=maximum)
    # Reading each editor paragraph independently avoids treating the source
    # indentation between HTML tags as an extra blank line.  Empty paragraphs
    # inside the response remain intentional empty source lines.
    rendered = [_render_rich_text(paragraph, maximum=maximum) for paragraph in paragraphs]
    return _trim_empty_boundary_lines("\n".join(rendered), maximum=maximum)


def _same_origin_url(value: str | None, base_url: str) -> str | None:
    if not value or len(value) > 4_096:
        return None
    try:
        absolute = urljoin(f"{base_url}/", value)
        parsed = urlsplit(absolute)
        origin = exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
    except (TypeError, ValueError):
        return None
    if origin != base_url or parsed.username or parsed.password or parsed.fragment:
        return None
    return absolute


def _query_positive(url: str, name: str) -> str | None:
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get(name, [])
    if len(values) != 1 or not _POSITIVE_ID.fullmatch(values[0]):
        return None
    return values[0]


def _course_context(soup: BeautifulSoup, base_url: str, course_id: str) -> bool:
    body = soup.body
    if isinstance(body, Tag):
        classes = {str(value) for value in body.get("class") or []}
        if str(body.get("data-courseid", "")) == course_id or f"course-{course_id}" in classes:
            return True
    for anchor in soup.select("a[href*='/course/view.php']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if (
            url
            and urlsplit(url).path.rstrip("/") == "/course/view.php"
            and _query_positive(url, "id") == course_id
        ):
            return True
    return False


def _user_evidence(row: Tag, base_url: str) -> tuple[str, str]:
    for anchor in row.select("a[href*='/user/view.php'], a[href*='/user/profile.php']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url:
            continue
        path = urlsplit(url).path.rstrip("/")
        if path not in {"/user/view.php", "/user/profile.php"}:
            continue
        user_id = _query_positive(url, "id")
        display_name = _text(anchor)
        if user_id and display_name:
            return user_id, display_name
    for control in row.select("input[name='selectedusers'][value], [data-userid]"):
        user_id = str(control.get("value", "") or control.get("data-userid", "")).strip()
        if _POSITIVE_ID.fullmatch(user_id):
            name = _text(row.select_one(".fullname, .userfullname, .c1"))
            if name:
                return user_id, name
    return "", ""


def _timestamp(node: Tag) -> int:
    # Moodle tables use ``data-sort`` for many unrelated numeric columns
    # (user ids, attempt numbers and grades included).  Prefer explicit time
    # cells and reject tiny Unix values so a grade such as ``8`` cannot turn
    # into a misleading 1970 submission date.
    time_candidates = list(
        node.select(
            "time, .timefinish, .timesubmitted, .submissiontime, "
            "td[data-column*='time'], td[data-column*='date'], "
            "[data-region*='time'][data-timestamp], [data-region*='date'][data-timestamp]"
        )
    )
    generic_candidates = list(node.select("[data-timestamp], [data-time], [data-sort]"))
    candidates = [node, *time_candidates]
    candidates.extend(candidate for candidate in generic_candidates if candidate not in candidates)
    for candidate in candidates:
        for attribute in ("data-timestamp", "data-time", "data-sort"):
            raw = str(candidate.get(attribute, "")).strip()
            if raw.isdigit():
                value = int(raw)
                if value > 10**12:
                    value //= 1_000
                if 946_684_800 <= value <= 4_102_444_800:
                    return value
        raw_datetime = str(candidate.get("datetime", "")).strip()
        if raw_datetime:
            with_timezone = raw_datetime.replace("Z", "+00:00")
            try:
                return max(0, int(datetime.fromisoformat(with_timezone).timestamp()))
            except ValueError:
                pass

    value = _text(node, maximum=2_000)
    match = re.search(
        r"([0-9]{1,2})\s+([А-Яа-яЁё]+)\s+([0-9]{4})[^0-9]+([0-9]{1,2}):([0-9]{2})",
        value,
    )
    if match and match.group(2).casefold() in _RU_MONTHS:
        try:
            parsed = datetime(
                int(match.group(3)),
                _RU_MONTHS[match.group(2).casefold()],
                int(match.group(1)),
                int(match.group(4)),
                int(match.group(5)),
                tzinfo=ZoneInfo("Europe/Moscow"),
            )
            return int(parsed.timestamp())
        except ValueError:
            return 0
    return 0


def _grade(value: str) -> tuple[float | None, float | None]:
    normalized = value.replace("\xa0", " ")
    pair = _GRADE_PAIR.search(normalized)
    if pair:
        return float(pair.group(1).replace(",", ".")), float(pair.group(2).replace(",", "."))
    single = _GRADE_VALUE.search(normalized)
    if single:
        return float(single.group(1).replace(",", ".")), None
    return None, None


def _state(
    value: str, *, grade: float | None
) -> Literal["IN_PROGRESS", "SUBMITTED", "GRADED", "UNKNOWN"]:
    normalized = value.casefold()
    in_progress = any(
        word in normalized for word in ("в процессе", "in progress", "черновик", "draft")
    )
    terminal = any(
        word in normalized for word in ("завершен", "отправлен", "submitted", "finished", "done")
    )
    explicitly_ungraded = any(
        word in normalized
        for word in (
            "не оценен",
            "не оценено",
            "не оценена",
            "не оценены",
            "not graded",
            "not yet graded",
            "ungraded",
            "requires grading",
            "needs grading",
            "требуется оценивание",
        )
    )
    explicitly_graded = any(
        word in normalized for word in ("оценен", "graded", "проверен")
    ) and not explicitly_ungraded
    if in_progress:
        return "IN_PROGRESS"
    if terminal:
        return "GRADED" if grade is not None or explicitly_graded else "SUBMITTED"
    if grade is not None or explicitly_graded:
        return "GRADED"
    return "UNKNOWN"


def _historical_attempt_recency(item: dict[str, Any]) -> tuple[int, int, int, str]:
    """Order attempts by their authoritative Moodle identity when possible."""

    attempt_id = str(item.get("attempt_id", "")).strip()
    if attempt_id.isdigit():
        return 2, int(attempt_id), int(item.get("submitted_at_epoch") or 0), attempt_id
    reopened = re.search(r"(?:^|-)attempt-([0-9]+)$", attempt_id)
    if reopened is not None:
        return 1, int(reopened.group(1)), int(item.get("submitted_at_epoch") or 0), attempt_id
    return 0, 0, int(item.get("submitted_at_epoch") or 0), attempt_id


def prioritize_historical_attempts(
    items: list[dict[str, Any]],
    *,
    pending_only: bool = False,
) -> list[dict[str, Any]]:
    """Keep the latest row per student and put review candidates first.

    Moodle's Quiz overview is commonly sorted by surname.  Processing that DOM
    order made a new ungraded submission wait behind every old graded row.  A
    student can also have several attempts; only the latest remote attempt is a
    current review candidate.  Keep that invariant before opening expensive
    review pages, matching the proven behaviour of the previous connector.
    """

    latest: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        scope = (
            str(item.get("module", "")),
            str(item.get("cmid", "")),
            str(item.get("user_id", "")),
        )
        current = latest.get(scope)
        if current is None or _historical_attempt_recency(item) > _historical_attempt_recency(
            current
        ):
            latest[scope] = item

    candidates = list(latest.values())
    if pending_only:
        candidates = [
            item
            for item in candidates
            if item.get("grade") is None
            and str(item.get("state", "")).upper() in {"SUBMITTED", "IN_PROGRESS"}
        ]

    # Stable two-pass ordering keeps newest attempts first within each state.
    candidates.sort(key=_historical_attempt_recency, reverse=True)
    state_priority = {"SUBMITTED": 0, "IN_PROGRESS": 1, "GRADED": 2, "UNKNOWN": 3}
    candidates.sort(
        key=lambda item: state_priority.get(str(item.get("state", "")).upper(), 4)
    )
    return candidates


def _has_next_page(soup: BeautifulSoup, base_url: str, path: str, current_page: int) -> bool:
    for anchor in soup.select("a[href]"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url or urlsplit(url).path.rstrip("/") != path:
            continue
        pages = parse_qs(urlsplit(url).query, keep_blank_values=True).get("page", [])
        if len(pages) != 1 or not pages[0].isdigit() or int(pages[0]) <= current_page:
            continue
        label = " ".join(
            (
                str(anchor.get("rel", "")),
                str(anchor.get("aria-label", "")),
                str(anchor.get("title", "")),
                _text(anchor),
            )
        ).casefold()
        if int(pages[0]) == current_page + 1 or any(
            marker in label for marker in ("next", "след", "далее", "»")
        ):
            return True
    return False


def _report_explicitly_empty(
    soup: BeautifulSoup,
    base_url: str,
    report_path: str,
    page_number: int,
) -> bool:
    """Recognise Moodle's empty-table output without hiding a broken report.

    Moodle's flexible_table renders an informational notification (a heading
    in older versions), not a table, when no rows match. Missing markup alone
    is not evidence of an empty result. Authentication and target validation
    remain mandatory in the caller.
    """

    region = soup.select_one("#region-main, main, [role='main']")
    if region is None or soup.select_one(
        ".errorbox, .errormessage, .alert-danger, .alert-error, "
        "#page-error, form#login, input[type='password']"
    ):
        return False
    if region.select_one(
        "a[href*='attempt='], input[name='attemptid[]'], input[name='attemptid'], "
        "a[href*='/mod/assign/view.php'][href*='userid='], "
        "table#attempts, table#mod_assign_grading, table#gradingtable, table.gradingtable"
    ) or _has_next_page(soup, base_url, report_path, page_number):
        return False
    empty_messages = {
        "nothing to display",
        "нечего показывать",
        "нет данных для отображения",
        "no attempts",
        "no attempts have been made",
        "попыток пока нет",
        "нет попыток",
        "there are no students enrolled in this course",
        "на этот курс пока не записан ни один студент",
        "на этот курс пока не записаны студенты",
        "no users found",
        "пользователи не найдены",
    }
    return any(
        _text(node, maximum=500).casefold().rstrip(".! ") in empty_messages
        for node in region.select(".alert-info, .notification-info, h2, h3")
    )


def parse_quiz_report_page(
    html: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    page_number: int,
) -> HistoricalIndexPage:
    soup = BeautifulSoup(html, "html.parser")
    if not _course_context(soup, base_url, course_id):
        raise MoodleMarkupError("Moodle quiz report has another course context")
    table = soup.select_one("table#attempts")
    if table is None:
        if _report_explicitly_empty(soup, base_url, "/mod/quiz/report.php", page_number):
            return HistoricalIndexPage(items=[], has_next=False)
        raise MoodleMarkupError("Moodle quiz report has no attempts table")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for row in table.select("tbody tr"):
        user_id, display_name = _user_evidence(row, base_url)
        grade_node = row.select_one("td.grade, .grade, [data-column='grade']")
        grade, grade_max = _grade(_text(grade_node, maximum=2_000))
        row_text = _text(row, maximum=8_000)
        parsed_state = _state(row_text, grade=grade)
        submitted_at = _timestamp(row)
        raw_attempt_evidence = bool(
            row.select(
                "a[href*='attempt='], input[name='attemptid[]'][value], "
                "input[name='attemptid'][value]"
            )
        )
        review_url = ""
        attempt_id = ""
        review_attempts: dict[str, str] = {}
        for anchor in row.select(
            "a.reviewlink[href], a[href*='/mod/quiz/review.php'][href*='attempt=']"
        ):
            candidate = _same_origin_url(anchor.get("href"), base_url)
            if not candidate or urlsplit(candidate).path.rstrip("/") != "/mod/quiz/review.php":
                continue
            candidate_attempt = _query_positive(candidate, "attempt")
            candidate_cmid = _query_positive(candidate, "cmid")
            if not candidate_attempt or candidate_cmid not in {None, str(cmid)}:
                continue
            review_attempts.setdefault(candidate_attempt, candidate)
        if len(review_attempts) == 1:
            attempt_id, review_url = next(iter(review_attempts.items()))

        # Moodle 5 can omit review.php for a still-running attempt.  Preserve
        # that row as an IN_PROGRESS marker using only exact same-origin
        # attempt/summary links or the report's own attemptid checkbox.  These
        # fallbacks are intentionally unavailable for terminal rows, whose
        # response import still requires a review URL.
        if not attempt_id and parsed_state == "IN_PROGRESS":
            active_attempts: dict[str, str] = {}
            for anchor in row.select("a[href*='attempt=']"):
                candidate = _same_origin_url(anchor.get("href"), base_url)
                if not candidate or urlsplit(candidate).path.rstrip("/") not in {
                    "/mod/quiz/attempt.php",
                    "/mod/quiz/summary.php",
                }:
                    continue
                candidate_attempt = _query_positive(candidate, "attempt")
                candidate_cmid = _query_positive(candidate, "cmid")
                if not candidate_attempt or candidate_cmid not in {None, str(cmid)}:
                    continue
                active_attempts.setdefault(candidate_attempt, candidate)
            for control in row.select(
                "input[name='attemptid[]'][value], input[name='attemptid'][value]"
            ):
                candidate_attempt = str(control.get("value", "")).strip()
                if re.fullmatch(r"[1-9][0-9]{0,19}", candidate_attempt):
                    active_attempts.setdefault(candidate_attempt, "")
            if len(active_attempts) == 1:
                attempt_id, review_url = next(iter(active_attempts.items()))
        # Moodle 5.2 places an empty spacer row and an aggregate/average row in
        # the report tbody. It may also list enrolled students who have no
        # attempt. None of those rows is an unidentified attempt. Conversely,
        # retain fail-closed accounting whenever a row contains raw attempt
        # evidence, or claims a terminal/active state that we could not bind.
        if not raw_attempt_evidence and (
            not user_id or (parsed_state == "UNKNOWN" and grade is None and submitted_at == 0)
        ):
            continue
        identified = bool(
            user_id
            and display_name
            and attempt_id
            and attempt_id not in seen
            and (review_url or parsed_state == "IN_PROGRESS")
        )
        if not identified:
            skipped += 1
            continue
        seen.add(attempt_id)
        items.append(
            {
                "module": "quiz",
                "cmid": cmid,
                "attempt_id": attempt_id,
                "user_id": user_id,
                "display_name": display_name,
                "state": parsed_state,
                "submitted_at_epoch": submitted_at,
                "grade": grade,
                "grade_max": grade_max,
                "comment": "",
                "responses": [],
                "_detail_url": review_url,
            }
        )
    return HistoricalIndexPage(
        items=items,
        has_next=_has_next_page(soup, base_url, "/mod/quiz/report.php", page_number),
        skipped_rows=skipped,
    )


def _artifact_links(node: Tag, base_url: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for anchor in node.select("a[href*='/pluginfile.php/']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url or not urlsplit(url).path.startswith("/pluginfile.php/"):
            continue
        if url in seen:
            continue
        seen.add(url)
        filename = _text(anchor, maximum=255)
        if not filename:
            filename = unquote(urlsplit(url).path.rsplit("/", 1)[-1])[:255]
        filename = filename.replace("/", "_").replace("\\", "_").replace("\0", "")
        filename = "".join("_" if ord(character) < 32 else character for character in filename)
        if not filename or filename in {".", ".."}:
            filename = f"moodle-file-{hashlib.sha256(url.encode()).hexdigest()[:12]}"
        result.append(
            {
                "external_id": hashlib.sha256(url.encode()).hexdigest(),
                "filename": filename,
                "url": url,
            }
        )
        if len(result) >= 16:
            break
    return result


def _response_grade(question: Tag) -> tuple[float | None, float | None]:
    for node in question.select(".grade, .info .grade"):
        value = _text(node, maximum=2_000)
        grade, grade_max = _grade(value)
        if grade_max is not None:
            return grade, grade_max
        if grade is not None:
            # Moodle renders an ungraded Essay question as a single value such
            # as ``Балл: 3,00`` / ``Marked out of 3.00``.  That value is the
            # question maximum, not an awarded grade.  The previous connector
            # deliberately treated a singleton this way and only considered
            # ``X из Y`` / ``X out of Y`` to be a confirmed question grade.
            return None, grade
        numbers = re.findall(_NUMBER, value)
        if len(numbers) == 1:
            return None, float(numbers[0].replace(",", "."))
    return None, None


def _split_reviewer_signature(lines: list[str]) -> tuple[list[str], str]:
    """Detach an unmistakable ``Surname I.`` signature from a Moodle comment."""

    normalized = [line.strip() for line in lines if line.strip()]
    if normalized and _REVIEWER_SIGNATURE.fullmatch(normalized[-1]):
        return normalized[:-1], normalized[-1][:255]
    return normalized, ""


def _comment_paragraphs(
    node: Tag | None,
    *,
    direct_only: bool = False,
) -> tuple[str, str]:
    if node is None:
        return "", ""
    lines: list[str] = []
    for paragraph in node.find_all("p", recursive=not direct_only):
        value = _multiline_text(paragraph, maximum=20_000)
        lines.extend(value.splitlines())
    comment_lines, reviewer_name = _split_reviewer_signature(lines)
    return "\n".join(comment_lines)[:20_000], reviewer_name


def _response_comment(question: Tag) -> tuple[str, str]:
    # Moodle's manual-grading wrapper also contains the accessibility heading,
    # the literal ``Comment:`` label and the action link.  The old connector
    # intentionally read only non-empty paragraphs, which are the actual editor
    # payload.  Keep that behaviour and expose a conventional trailing teacher
    # signature separately.
    for selector in (".comment.clearfix", ".comment"):
        node = question.select_one(selector)
        if node is None:
            continue
        comment, reviewer_name = _comment_paragraphs(node, direct_only=True)
        if comment or reviewer_name:
            return comment, reviewer_name
    for selector in (
        ".outcome .specificfeedback",
        ".feedback .specificfeedback",
        ".specificfeedback",
    ):
        node = question.select_one(selector)
        value = _multiline_text(node, maximum=20_000)
        if value:
            lines, reviewer_name = _split_reviewer_signature(value.splitlines())
            return "\n".join(lines)[:20_000], reviewer_name
    return "", ""


def quiz_review_navigation_urls(
    html: str,
    current_url: str,
    *,
    base_url: str,
    cmid: int,
    attempt_id: str,
) -> tuple[str | None, list[str]]:
    """Return bounded, origin-checked navigation for one Quiz review attempt.

    Moodle can render a review either as one page (``showall=1``) or as one
    question page at a time (``page=N``).  Report links do not consistently
    include ``cmid``, so it remains optional, but whenever it is present it
    must identify the requested activity.  The attempt id is never optional.

    The returned show-all URL is deliberately limited to an explicit
    ``showall=1`` link emitted by Moodle.  We do not manufacture a privileged
    target from arbitrary markup.  Page links are sorted numerically and
    capped here as a second line of defence; the browser crawler applies its
    own visit bound as well.
    """

    soup = BeautifulSoup(html, "html.parser")
    show_all_url: str | None = None
    pages: dict[int, str] = {}
    for anchor in soup.select("a[href]"):
        candidate = _same_origin_url(anchor.get("href"), base_url)
        if not candidate or candidate == current_url:
            continue
        parsed = urlsplit(candidate)
        if parsed.path.rstrip("/") != "/mod/quiz/review.php":
            continue
        if _query_positive(candidate, "attempt") != attempt_id:
            continue
        candidate_cmid = _query_positive(candidate, "cmid")
        if candidate_cmid not in {None, str(cmid)}:
            continue
        query = parse_qs(parsed.query, keep_blank_values=True)
        if query.get("showall") == ["1"]:
            show_all_url = candidate
            continue
        raw_pages = query.get("page", [])
        if len(raw_pages) == 1 and raw_pages[0].isdigit() and 0 <= int(raw_pages[0]) < 64:
            pages.setdefault(int(raw_pages[0]), candidate)
    return show_all_url, [pages[number] for number in sorted(pages)]


def parse_quiz_review_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    attempt_id: str,
    user_id: str,
    allow_empty: bool = False,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    parsed = urlsplit(current_url)
    if (
        parsed.path.rstrip("/") != "/mod/quiz/review.php"
        or _query_positive(current_url, "attempt") != attempt_id
        or _query_positive(current_url, "cmid") not in {None, str(cmid)}
        or not _course_context(soup, base_url, course_id)
    ):
        raise MoodleMarkupError("Moodle quiz review identifiers changed")

    page_users = {
        found
        for anchor in soup.select("a[href*='/user/view.php'], a[href*='/user/profile.php']")
        if (url := _same_origin_url(anchor.get("href"), base_url))
        if (found := _query_positive(url, "id"))
    }
    if page_users and user_id not in page_users:
        raise MoodleMarkupError("Moodle quiz review identifies another user")

    responses: list[dict[str, Any]] = []
    for position, question in enumerate(soup.select(".que.essay"), start=1):
        response_id = str(question.get("data-slot", "")).strip()
        if not response_id:
            match = re.search(r"(?:^|-)question-[0-9]+-([0-9]+)$", str(question.get("id", "")))
            if match:
                response_id = match.group(1)
            else:
                question_number = _text(question.select_one(".info .qno, .qno"), maximum=80)
                number_match = re.search(r"[0-9]+", question_number)
                response_id = number_match.group(0) if number_match else f"position-{position}"
        answer = ""
        # Moodle 4.2 can render a reviewed Essay directly under ``.answer``
        # without the older ``.qtype_essay_response`` wrapper.  The rich-text
        # renderer excludes attachment controls from that fallback.
        for selector in (
            ".answer .qtype_essay_response",
            ".qtype_essay_response",
            ".answer",
        ):
            answer = _answer_text(question.select_one(selector))
            if answer:
                break
        grade, grade_max = _response_grade(question)
        artifacts = _artifact_links(question, base_url)
        comment, reviewer_name = _response_comment(question)
        responses.append(
            {
                "response_id": response_id[:255],
                "question_text": _text(question.select_one(".qtext"), maximum=50_000),
                "answer_text": answer,
                "answer_complete": True,
                "answer_omission_reason": "",
                "grade": grade,
                "grade_max": grade_max,
                "comment": comment,
                "reviewer_name": reviewer_name,
                "artifacts": [],
                "_artifact_links": artifacts,
            }
        )
    if not responses and not allow_empty:
        raise MoodleMarkupError("Moodle quiz review has no essay responses")
    return {"responses": responses}


def parse_assignment_grading_page(
    html: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    page_number: int,
) -> HistoricalIndexPage:
    soup = BeautifulSoup(html, "html.parser")
    if not _course_context(soup, base_url, course_id):
        raise MoodleMarkupError("Moodle assignment grading page has another course context")
    table = soup.select_one("table#mod_assign_grading, table#gradingtable, table.gradingtable")
    if table is None:
        if _report_explicitly_empty(soup, base_url, "/mod/assign/view.php", page_number):
            return HistoricalIndexPage(items=[], has_next=False)
        raise MoodleMarkupError("Moodle assignment grading page has no submissions table")

    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for row in table.select("tbody tr"):
        user_id, display_name = _user_evidence(row, base_url)
        if not user_id:
            skipped += 1
            continue
        detail_url = ""
        attempt_number = "0"
        for anchor in row.select("a[href*='/mod/assign/view.php'][href*='action=']"):
            candidate = _same_origin_url(anchor.get("href"), base_url)
            if not candidate or urlsplit(candidate).path.rstrip("/") != "/mod/assign/view.php":
                continue
            query = parse_qs(urlsplit(candidate).query, keep_blank_values=True)
            if query.get("id") != [str(cmid)] or query.get("userid") != [user_id]:
                continue
            if query.get("action", [""])[0] not in {"grader", "grade"}:
                continue
            raw_attempt = query.get("attemptnumber", ["0"])[0]
            if raw_attempt.isdigit() and int(raw_attempt) <= 1_000_000:
                attempt_number = raw_attempt
            detail_url = candidate
            break
        if not detail_url:
            # Standard Moodle accepts this canonical teacher grading URL even
            # when the table action is rendered as a JS menu.
            detail_url = f"{base_url}/mod/assign/view.php?id={cmid}&action=grader&userid={user_id}"
        attempt_id = f"user-{user_id}-attempt-{attempt_number}"
        if attempt_id in seen:
            skipped += 1
            continue
        seen.add(attempt_id)
        grade_node = row.select_one("td.grade, .grade, [data-column='grade']")
        grade, grade_max = _grade(_text(grade_node, maximum=2_000))
        row_text = _text(row, maximum=8_000)
        submitted_at = _timestamp(row)
        normalized_row = row_text.casefold()
        row_classes = {str(value).casefold() for value in row.get("class") or []}
        explicit_submission = bool(
            row_classes.intersection(
                {"submissionstatussubmitted", "submissionstatusdraft", "submitted"}
            )
        )
        explicit_absence = any(
            marker in normalized_row
            for marker in (
                "no submission",
                "not submitted",
                "не отправлено",
                "нет ответа",
                "ответ не предоставлен",
            )
        )
        parsed_state = _state(row_text, grade=grade)
        if grade is None and explicit_absence:
            # An enrolled student who has not submitted is a normal empty
            # result, not an unidentified/malformed historical submission.
            continue
        if (
            grade is None
            and parsed_state == "UNKNOWN"
            and not explicit_submission
            and not submitted_at
        ):
            skipped += 1
            continue
        items.append(
            {
                "module": "assign",
                "cmid": cmid,
                "attempt_id": attempt_id,
                "user_id": user_id,
                "display_name": display_name,
                "state": parsed_state,
                "submitted_at_epoch": submitted_at,
                "grade": grade,
                "grade_max": grade_max,
                "comment": "",
                "responses": [],
                "_detail_url": detail_url,
            }
        )
    return HistoricalIndexPage(
        items=items,
        has_next=_has_next_page(soup, base_url, "/mod/assign/view.php", page_number),
        skipped_rows=skipped,
    )


def parse_assignment_grader_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    user_id: str,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    parsed = urlsplit(current_url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (
        parsed.path.rstrip("/") != "/mod/assign/view.php"
        or query.get("id") != [str(cmid)]
        or query.get("action", [""])[0] not in {"grader", "grade"}
        or query.get("userid") != [user_id]
        or not _course_context(soup, base_url, course_id)
    ):
        raise MoodleMarkupError("Moodle assignment grader identifiers changed")

    answer = ""
    for selector in (
        ".assignsubmission_onlinetext",
        "[data-region='assignsubmission_onlinetext']",
        ".onlinetextsubmission",
    ):
        answer = _answer_text(soup.select_one(selector))
        if answer:
            break
    # File-submission links are valid only inside the corresponding submission
    # plugin.  Falling back to the whole grader document also picked up task
    # PDFs, course resources and navigation files as if the student submitted
    # them.  Moodle may render more than one plugin wrapper, so merge the exact
    # roots while retaining deterministic order and the global response bound.
    artifacts: list[dict[str, str]] = []
    artifact_ids: set[str] = set()
    for attachments_root in soup.select(
        ".assignsubmission_file, .fileuploadsubmission, [data-region='assignsubmission_file']"
    ):
        for artifact in _artifact_links(attachments_root, base_url):
            external_id = artifact["external_id"]
            if external_id in artifact_ids:
                continue
            artifact_ids.add(external_id)
            artifacts.append(artifact)
            if len(artifacts) >= 16:
                break
        if len(artifacts) >= 16:
            break

    grade: float | None = None
    grade_max: float | None = None
    grade_control = soup.select_one("input[name='grade'][value], select[name='grade']")
    if isinstance(grade_control, Tag):
        raw = str(grade_control.get("value", "")).strip()
        if grade_control.name == "select":
            selected = grade_control.select_one("option[selected]")
            raw = str(selected.get("value", "")).strip() if selected else raw
        try:
            grade = float(raw.replace(",", ".")) if raw not in {"", "-1"} else None
        except ValueError:
            grade = None
    for node in soup.select(".grade, .gradingform .fstatic"):
        parsed_grade = _grade(_text(node, maximum=2_000))
        if parsed_grade != (None, None):
            if grade is None:
                grade = parsed_grade[0]
            grade_max = parsed_grade[1]
            break

    comment = ""
    reviewer_name = ""
    editor = soup.select_one("textarea[name='assignfeedbackcomments_editor[text]']")
    if isinstance(editor, Tag):
        raw_comment = editor.get_text("", strip=False)
        # HTML editors store serialized paragraphs inside the textarea.  Parse
        # that payload as a fragment so tags never leak into the local comment.
        fragment = BeautifulSoup(raw_comment, "html.parser")
        paragraphs = fragment.select("p")
        if paragraphs:
            lines: list[str] = []
            for paragraph in paragraphs:
                lines.extend(_multiline_text(paragraph, maximum=20_000).splitlines())
            lines, reviewer_name = _split_reviewer_signature(lines)
            comment = "\n".join(lines)[:20_000]
        else:
            lines, reviewer_name = _split_reviewer_signature(
                _trim_empty_boundary_lines(raw_comment, maximum=20_000).splitlines()
            )
            comment = "\n".join(lines)[:20_000]
    else:
        feedback = soup.select_one("[data-region='assignfeedbackcomments']")
        comment, reviewer_name = _comment_paragraphs(feedback)

    return {
        "grade": grade,
        "grade_max": grade_max,
        "comment": comment,
        "responses": [
            {
                "response_id": "submission",
                "question_text": "",
                "answer_text": answer,
                "answer_complete": True,
                "answer_omission_reason": "",
                "grade": grade,
                "grade_max": grade_max,
                "comment": comment,
                "reviewer_name": reviewer_name,
                "artifacts": [],
                "_artifact_links": artifacts,
            }
        ],
    }


def finalize_historical_submission(item: dict[str, Any]) -> dict[str, Any]:
    def public_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: public_value(child)
                for key, child in value.items()
                if not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [public_value(child) for child in value]
        return value

    public = public_value(item)
    public["external_id"] = f"{public['module']}:{public['cmid']}:{public['attempt_id']}"
    public["external_revision"] = canonical_hash(public)
    return public
