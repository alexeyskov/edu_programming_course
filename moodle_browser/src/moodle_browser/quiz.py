from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .config import exact_https_origin
from .parsers import MoodleMarkupError

QUIZ_START_FORM_SELECTOR = "form[action*='startattempt.php']"
QUIZ_DIRECT_START_FORM_SELECTOR = "form[action*='startattempt.php']:not(#mod_quiz_preflight_form)"
QUIZ_PREFLIGHT_FORM_SELECTOR = "form#mod_quiz_preflight_form[action*='startattempt.php']"
QUIZ_PREFLIGHT_START_SELECTOR = (
    f"{QUIZ_PREFLIGHT_FORM_SELECTOR} "
    "button[type='submit']:not([name='cancel']), "
    f"{QUIZ_PREFLIGHT_FORM_SELECTOR} "
    "input[type='submit']:not([name='cancel'])"
)
QUIZ_CONTINUE_LINK_SELECTOR = "a[href*='/mod/quiz/attempt.php']"
ESSAY_SELECTOR = ".que.essay"
FILEMANAGER_SELECTOR = f"{ESSAY_SELECTOR} .filemanager"
FILE_ADD_SELECTOR = f"{FILEMANAGER_SELECTOR} .fp-btn-add"
ONLINE_TEXT_SELECTOR = f"{ESSAY_SELECTOR} textarea[name$='_answer']"
FILE_INPUT_SELECTOR = "input[type='file']"
FILE_UPLOAD_SELECTOR = ".fp-upload-btn"
FILE_OVERWRITE_SELECTOR = ".fp-dlg-butoverwrite"
NEXT_NAV_SELECTOR = "#mod_quiz-next-nav"
FINALIZE_FORM_SELECTOR = "form[action*='processattempt.php']"
FINALIZE_ATTEMPT_SELECTOR = f"{FINALIZE_FORM_SELECTOR} input[name='attempt']"
FINALIZE_CMID_SELECTOR = f"{FINALIZE_FORM_SELECTOR} input[name='cmid']"
FINALIZE_FINISH_SELECTOR = f"{FINALIZE_FORM_SELECTOR} input[name='finishattempt']"
FINALIZE_SESSKEY_SELECTOR = f"{FINALIZE_FORM_SELECTOR} input[name='sesskey']"
FINALIZE_TIMEUP_SELECTOR = f"{FINALIZE_FORM_SELECTOR} input[name='timeup']"
FINALIZE_TRIGGER_SELECTOR = (
    f"{FINALIZE_FORM_SELECTOR} [name='submitallandfinish'], "
    f"{FINALIZE_FORM_SELECTOR} button[type='submit']"
)

_POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,19}$")
_PREVIEW = re.compile(r"(?:\bpreview\b|предварительн\w*\s+просмотр|\bпросмотр\w*)", re.I)
_CONTINUE_ATTEMPT = re.compile(
    r"(?:continue(?:\s+(?:the|your|current))?\s+attempt|"
    r"продолжить(?:\s+текущую)?\s+попытку)",
    re.I,
)
_FINALIZE = re.compile(
    r"(?:submit\s+all\s+(?:(?:your\s+)?answers\s+)?and\s+finish|"
    r"отправить\s+все\s+свои\s+ответы\s+и\s+закончить|"
    r"отправить\s+вс[её]\s+и\s+завершить|завершить\s+тест)",
    re.I,
)
_COMPLETED_STATE_LABEL = re.compile(r"^(?:state|состояние)$", re.I)
_COMPLETED_STATE_VALUE = re.compile(r"^(?:finished|completed|завершен(?:а|о|ы)?)$", re.I)
_COMPLETED_TIME_LABEL = re.compile(
    r"^(?:completed\s+on|finished\s+on|finished|завершен(?:а|о)?)$", re.I
)
_ATTEMPT_SUMMARY_CAPTION = re.compile(r"(?:attempt|попытк)", re.I)


@dataclass(frozen=True, slots=True)
class QuizLaunch:
    kind: Literal["FORM", "LINK"]
    target_url: str
    trigger_text: str
    requires_preflight: bool = False


@dataclass(frozen=True, slots=True)
class QuizAttempt:
    attempt_id: str
    question_slot: str
    existing_filenames: tuple[str, ...]
    available_transports: tuple[Literal["ESSAY_ONLINE_TEXT", "ESSAY_ATTACHMENT"], ...] = (
        "ESSAY_ATTACHMENT",
    )
    online_text_control_name: str | None = None
    attachment_urls: tuple[tuple[str, str], ...] = ()
    question_text: str = ""
    remaining_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class QuizSummary:
    attempt_id: str
    trigger_text: str


class QuizAttemptNotActive(MoodleMarkupError):
    """The previously bound student attempt is no longer offered for continuation."""


class QuizAttemptUnavailable(MoodleMarkupError):
    """The student page proves that no attempt can currently be opened."""


def _text(node: Tag | None, *, maximum: int = 2_000) -> str:
    if node is None:
        return ""
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()[:maximum]


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


def _remaining_seconds(html: str, soup: BeautifulSoup) -> int | None:
    """Read Moodle's live Quiz countdown without making it authoritative locally."""

    timers = soup.select("#quiz-time-left")
    if len(timers) == 1:
        value = _text(timers[0], maximum=128)
        match = re.fullmatch(r"([0-9]+):([0-5][0-9]):([0-5][0-9])", value)
        if match:
            hours, minutes, seconds = (int(part) for part in match.groups())
            return hours * 3600 + minutes * 60 + seconds
        match = re.fullmatch(r"([0-5]?[0-9]):([0-5][0-9])", value)
        if match:
            minutes, seconds = (int(part) for part in match.groups())
            return minutes * 60 + seconds

    # Moodle also embeds the same server-derived value in its timer bootstrap.
    # This fallback covers localized textual renderings while keeping the
    # selector-independent value bounded and unambiguous.
    values = {
        int(value)
        for value in re.findall(
            r"M\.mod_quiz\.timer\.init\(\s*Y\s*,\s*([0-9]{1,10})\s*,",
            html,
        )
        if int(value) <= 315_360_000
    }
    return next(iter(values)) if len(values) == 1 else None


def _course_context(soup: BeautifulSoup, base_url: str, course_id: str) -> bool:
    body = soup.body
    if isinstance(body, Tag):
        if str(body.get("data-courseid", "")) == course_id:
            return True
        if f"course-{course_id}" in {str(value) for value in body.get("class") or []}:
            return True
    for anchor in soup.select("a[href*='/course/view.php']"):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url or urlsplit(url).path.rstrip("/") != "/course/view.php":
            continue
        if _query_positive(url, "id") == course_id:
            return True
    return False


def preview_evidence(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if isinstance(body, Tag):
        marker = " ".join(
            [str(body.get("id", "")), *(str(value) for value in body.get("class") or [])]
        )
        if re.search(r"(?:^|[-_])preview(?:$|[-_])", marker, re.I):
            return True
    for warning in soup.select(
        ".alert-warning, .alert-danger, .quizpreview, [data-region='warning'], .notification"
    ):
        if _PREVIEW.search(_text(warning)):
            return True
    return False


def parse_quiz_view(
    html: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    expected_attempt_id: str | None = None,
) -> QuizLaunch:
    soup = BeautifulSoup(html, "html.parser")
    if not _course_context(soup, base_url, course_id):
        raise MoodleMarkupError("Moodle quiz does not belong to the requested course")
    if preview_evidence(html):
        raise MoodleMarkupError("Moodle quiz is a teacher preview")

    launches: list[QuizLaunch] = []
    links: dict[str, QuizLaunch] = {}
    expected_launches: list[QuizLaunch] = []
    direct_forms = soup.select(QUIZ_DIRECT_START_FORM_SELECTOR)
    preflight_forms = soup.select(QUIZ_PREFLIGHT_FORM_SELECTOR)
    if len(preflight_forms) > 1:
        raise MoodleMarkupError("Moodle quiz preflight form is ambiguous")

    preflight_action = ""
    if preflight_forms:
        preflight = preflight_forms[0]
        preflight_action = _same_origin_url(preflight.get("action"), base_url) or ""
        if urlsplit(preflight_action).path.rstrip("/") != "/mod/quiz/startattempt.php":
            raise MoodleMarkupError("Moodle quiz preflight form target is invalid")
        hidden_cmid = preflight.select_one("input[name='cmid']")
        preflight_cmid = (
            str(hidden_cmid.get("value", "")) if hidden_cmid is not None else None
        ) or _query_positive(preflight_action, "cmid")
        if preflight_cmid != str(cmid):
            raise MoodleMarkupError("Moodle quiz preflight form identifies another quiz")
        preflight_triggers = preflight.select(
            "button[type='submit']:not([name='cancel']), input[type='submit']:not([name='cancel'])"
        )
        if len(preflight_triggers) != 1:
            raise MoodleMarkupError("Moodle quiz preflight start trigger is ambiguous")
        preflight_trigger = preflight_triggers[0]
        preflight_text = (
            _text(preflight_trigger) or str(preflight_trigger.get("value", "")).strip()[:2_000]
        )
        if not preflight_text or _PREVIEW.search(preflight_text):
            raise MoodleMarkupError("Moodle quiz preflight start trigger is invalid")

    for form in direct_forms:
        action = _same_origin_url(form.get("action"), base_url)
        if not action or urlsplit(action).path.rstrip("/") != "/mod/quiz/startattempt.php":
            continue
        hidden_cmid = form.select_one("input[name='cmid']")
        form_cmid = (
            str(hidden_cmid.get("value", "")) if hidden_cmid is not None else None
        ) or _query_positive(action, "cmid")
        if form_cmid != str(cmid):
            continue
        triggers = form.select(
            "button[type='submit']:not([name='cancel']), input[type='submit']:not([name='cancel'])"
        )
        if len(triggers) != 1:
            raise MoodleMarkupError("Moodle quiz start form is ambiguous")
        trigger = triggers[0]
        trigger_text = _text(trigger) or str(trigger.get("value", "")).strip()[:2_000]
        if not trigger_text:
            raise MoodleMarkupError("Moodle quiz start form has no trigger")
        if _PREVIEW.search(trigger_text):
            raise MoodleMarkupError("Moodle quiz trigger is a teacher preview")
        launch = QuizLaunch(
            "FORM",
            action,
            trigger_text,
            requires_preflight=bool(
                preflight_action
                and preflight_action == action
                and not _CONTINUE_ATTEMPT.fullmatch(trigger_text)
            ),
        )
        if expected_attempt_id is None:
            launches.append(launch)
        elif _CONTINUE_ATTEMPT.fullmatch(trigger_text):
            # Recent Moodle versions expose an active attempt as a POST form
            # without putting its attempt id in the markup.  The explicit
            # continuation label lets us follow that form, but it does not
            # prove identity: parse_attempt_page and the caller must still
            # pin both the resulting attempt id and the question slot.
            expected_launches.append(launch)

    for anchor in soup.select(QUIZ_CONTINUE_LINK_SELECTOR):
        url = _same_origin_url(anchor.get("href"), base_url)
        if not url or urlsplit(url).path.rstrip("/") != "/mod/quiz/attempt.php":
            continue
        if _query_positive(url, "cmid") != str(cmid) or _query_positive(url, "attempt") is None:
            continue
        trigger_text = _text(anchor)
        if _PREVIEW.search(trigger_text):
            raise MoodleMarkupError("Moodle quiz trigger is a teacher preview")
        launch = QuizLaunch("LINK", url, trigger_text)
        links[url] = launch
        if _query_positive(url, "attempt") == expected_attempt_id:
            expected_launches.append(launch)

    if expected_attempt_id is not None:
        if len(expected_launches) != 1:
            # A start form or a continuation link for another attempt must
            # never be used after the application has bound the local session
            # to a concrete Moodle attempt.  The exact attempt disappearing is
            # Moodle's durable signal that it can no longer be edited.
            raise QuizAttemptNotActive("The bound Moodle quiz attempt is no longer active")
        return expected_launches[0]

    launches.extend(links.values())
    if not launches:
        raise QuizAttemptUnavailable("Moodle quiz is not available to this student")
    if len(launches) != 1:
        raise MoodleMarkupError("Moodle quiz has ambiguous real attempt triggers")
    return launches[0]


def parse_attempt_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
) -> QuizAttempt:
    soup = BeautifulSoup(html, "html.parser")
    if preview_evidence(html):
        raise MoodleMarkupError("Moodle attempt is a teacher preview")
    if not _course_context(soup, base_url, course_id):
        raise MoodleMarkupError("Moodle attempt course context is invalid")
    parsed = urlsplit(current_url)
    if parsed.path.rstrip("/") != "/mod/quiz/attempt.php":
        raise MoodleMarkupError("Moodle did not open a real quiz attempt")
    attempt_id = _query_positive(current_url, "attempt")
    if attempt_id is None or _query_positive(current_url, "cmid") != str(cmid):
        raise MoodleMarkupError("Moodle attempt identifiers are invalid")
    essays = soup.select(ESSAY_SELECTOR)
    if len(essays) != 1:
        raise MoodleMarkupError("Moodle attempt must contain exactly one essay question")
    essay = essays[0]
    question_nodes = essay.select(".qtext")
    if len(question_nodes) > 1:
        raise MoodleMarkupError("Moodle essay question text is ambiguous")
    question_text = _text(question_nodes[0], maximum=50_000) if question_nodes else ""
    managers = essay.select(".filemanager")
    if len(managers) > 1:
        raise MoodleMarkupError("Moodle essay file manager is ambiguous")
    online_controls = essay.select("textarea[name$='_answer']")
    if len(online_controls) > 1:
        raise MoodleMarkupError("Moodle essay online-text control is ambiguous")
    if not managers and not online_controls:
        raise MoodleMarkupError("Moodle essay has no supported response control")
    question_slot = str(essay.get("data-slot", "")).strip()
    if not question_slot:
        match = re.search(r"(?:^|-)question-[0-9]+-([0-9]+)$", str(essay.get("id", "")))
        question_slot = match.group(1) if match else ""
    if not _POSITIVE_ID.fullmatch(question_slot):
        raise MoodleMarkupError("Moodle essay has no stable question slot")
    names: set[str] = set()
    attachment_urls: set[tuple[str, str]] = set()
    transports: list[Literal["ESSAY_ONLINE_TEXT", "ESSAY_ATTACHMENT"]] = []
    if online_controls:
        control_name = str(online_controls[0].get("name", "")).strip()
        if not re.fullmatch(r"q[0-9]+:[0-9]+_answer", control_name):
            raise MoodleMarkupError("Moodle essay online-text control is not bound to a question")
        transports.append("ESSAY_ONLINE_TEXT")
    else:
        control_name = None
    if managers:
        manager = managers[0]
        if len(manager.select(".fp-btn-add")) != 1:
            raise MoodleMarkupError("Moodle essay file manager has no unambiguous add button")
        transports.append("ESSAY_ATTACHMENT")
        for node in manager.select(
            ".fp-filename, [data-filename], .fp-file [title], .filemanager-container a"
        ):
            name = (
                str(node.get("data-filename", "")).strip()
                or str(node.get("title", "")).strip()
                or _text(node, maximum=128)
            )
            if name:
                names.add(name)
        # Retain only exact, same-origin Moodle download URLs.  A durable
        # connector receipt identifies the managed filename; when Moodle also
        # exposes the current draft bytes, the service hashes them before it
        # accepts an overwrite confirmation.
        for node in manager.select(".fp-file, [data-filename], a[href]"):
            name = str(node.get("data-filename", "")).strip()
            if not name:
                filename_node = node.select_one(".fp-filename")
                name = (
                    str(node.get("title", "")).strip()
                    or _text(filename_node, maximum=128)
                    or (_text(node, maximum=128) if node.name == "a" else "")
                )
            links = [node] if node.name == "a" else list(node.select("a[href]"))
            for link in links:
                target = _same_origin_url(link.get("href"), base_url)
                if not target:
                    continue
                path = unquote(urlsplit(target).path)
                if not (path.startswith("/draftfile.php/") or path.startswith("/pluginfile.php/")):
                    continue
                if name and PurePosixPath(path).name == name:
                    attachment_urls.add((name, target))
    return QuizAttempt(
        attempt_id=attempt_id,
        question_slot=question_slot,
        existing_filenames=tuple(sorted(names)),
        available_transports=tuple(transports),
        online_text_control_name=control_name,
        attachment_urls=tuple(sorted(attachment_urls)),
        question_text=question_text,
        remaining_seconds=_remaining_seconds(html, soup),
    )


def parse_summary_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    attempt_id: str,
    require_finalize: bool = True,
) -> QuizSummary:
    soup = BeautifulSoup(html, "html.parser")
    if preview_evidence(html):
        raise MoodleMarkupError("Moodle attempt summary is a teacher preview")
    if not _course_context(soup, base_url, course_id):
        raise MoodleMarkupError("Moodle summary course context is invalid")
    parsed = urlsplit(current_url)
    if parsed.path.rstrip("/") != "/mod/quiz/summary.php":
        raise MoodleMarkupError("Moodle did not open the attempt summary")
    if _query_positive(current_url, "attempt") != attempt_id or _query_positive(
        current_url, "cmid"
    ) != str(cmid):
        raise MoodleMarkupError("Moodle summary attempt identifiers changed")

    if not require_finalize:
        return QuizSummary(attempt_id=attempt_id, trigger_text="")

    candidates: list[str] = []
    for form in soup.select(FINALIZE_FORM_SELECTOR):
        action = _same_origin_url(form.get("action"), base_url)
        if not action or urlsplit(action).path.rstrip("/") != "/mod/quiz/processattempt.php":
            continue
        hidden_attempt = form.select_one("input[name='attempt']")
        if hidden_attempt is not None and str(hidden_attempt.get("value", "")) != attempt_id:
            continue
        triggers = form.select("[name='submitallandfinish'], button[type='submit']")
        matching = []
        for trigger in triggers:
            text = _text(trigger) or str(trigger.get("value", "")).strip()[:2_000]
            if _FINALIZE.search(text):
                matching.append(text)
        if len(matching) == 1:
            candidates.append(matching[0])
    if len(candidates) != 1:
        raise MoodleMarkupError("Moodle final submission form is ambiguous")
    return QuizSummary(attempt_id=attempt_id, trigger_text=candidates[0])


def validate_final_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    attempt_id: str,
    allow_causal_completed_view: bool = False,
) -> None:
    soup = BeautifulSoup(html, "html.parser")
    if preview_evidence(html):
        raise MoodleMarkupError("Moodle finalized a teacher preview")
    if not _course_context(soup, base_url, course_id):
        raise MoodleMarkupError("Moodle final page course context is invalid")
    parsed = urlsplit(current_url)
    path = parsed.path.rstrip("/")
    if path == "/mod/quiz/review.php":
        if _query_positive(current_url, "attempt") != attempt_id:
            raise MoodleMarkupError("Moodle review attempt id changed")
        query_cmid = _query_positive(current_url, "cmid")
        if query_cmid is not None and query_cmid != str(cmid):
            raise MoodleMarkupError("Moodle review cmid changed")
        return
    if path == "/mod/quiz/view.php" and _query_positive(current_url, "id") == str(cmid):
        try:
            parse_quiz_view(
                html,
                base_url=base_url,
                course_id=course_id,
                cmid=cmid,
                expected_attempt_id=attempt_id,
            )
        except QuizAttemptNotActive:
            pass
        else:
            raise MoodleMarkupError("Moodle quiz attempt is still active")

        review_urls: set[str] = set()
        review_table_evidence = False
        for anchor in soup.select("a[href*='/mod/quiz/review.php']"):
            url = _same_origin_url(anchor.get("href"), base_url)
            if not url or urlsplit(url).path.rstrip("/") != "/mod/quiz/review.php":
                continue
            if _query_positive(url, "attempt") != attempt_id:
                continue
            query_cmid = _query_positive(url, "cmid")
            if query_cmid is not None and query_cmid != str(cmid):
                continue
            review_urls.add(url)
            if anchor.find_parent("table") is not None:
                review_table_evidence = True
        if len(review_urls) == 1 and review_table_evidence:
            return
        # Moodle can embargo review.php until the quiz closes.  Immediately
        # after this service has submitted a form whose hidden ``attempt`` was
        # pinned to ``attempt_id``, Moodle 5.2 redirects to view.php and exposes
        # only its completed-attempt summary.  This branch is deliberately
        # opt-in: the view HTML does not contain the remote attempt id and is
        # therefore not sufficient proof outside that causally bound submit.
        if allow_causal_completed_view:
            summaries = soup.select("table.quizreviewsummary")
            if summaries:
                # Moodle renders newest attempts first.  Older completed
                # attempts remain as additional tables after a re-attempt and
                # must not make the just-submitted latest table ambiguous.
                summary = summaries[0]
                captions = summary.select("caption")
                caption_matches = len(captions) == 1 and bool(
                    _ATTEMPT_SUMMARY_CAPTION.search(_text(captions[0]))
                )
                state_matches = False
                completed_at_matches = False
                for row in summary.select("tr"):
                    cells = row.select(":scope > th, :scope > td")
                    if len(cells) < 2:
                        continue
                    label = _text(cells[0])
                    value = _text(cells[1])
                    if _COMPLETED_STATE_LABEL.fullmatch(label):
                        state_matches = bool(_COMPLETED_STATE_VALUE.fullmatch(value))
                    if _COMPLETED_TIME_LABEL.fullmatch(label):
                        completed_at_matches = bool(value)
                if caption_matches and state_matches and completed_at_matches:
                    return
    raise MoodleMarkupError("Moodle did not confirm final submission")


def finalize_text_matches(value: str) -> bool:
    return bool(_FINALIZE.search(value))
