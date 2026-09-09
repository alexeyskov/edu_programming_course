from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .config import exact_https_origin
from .parsers import MoodleMarkupError

ASSIGNMENT_EDIT_FORM_SELECTOR = "form.mform"
ASSIGNMENT_FILEMANAGER_SELECTOR = (
    "#fitem_id_files_filemanager .filemanager, [data-fieldtype='filemanager'] .filemanager"
)
ASSIGNMENT_FILE_ADD_SELECTOR = (
    "#fitem_id_files_filemanager .filemanager .fp-btn-add, "
    "[data-fieldtype='filemanager'] .filemanager .fp-btn-add"
)
ASSIGNMENT_ONLINE_TEXT_SELECTOR = "textarea[name='onlinetext_editor[text]']"
ASSIGNMENT_SAVE_SELECTOR = (
    "form.mform button[type='submit'][name='submitbutton'], "
    "form.mform input[type='submit'][name='submitbutton']"
)

_POSITIVE_ID = re.compile(r"^[1-9][0-9]{0,19}$")


@dataclass(frozen=True, slots=True)
class AssignmentSubmissionForm:
    available_transports: tuple[Literal["ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"], ...]
    existing_filenames: tuple[str, ...]
    online_text_control_name: str | None
    submission_statement_control_name: str | None = None
    effective_max_bytes: int | None = None
    attachment_urls: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class AssignmentSubmissionView:
    status: Literal["DRAFT_SAVED", "FINALIZED", "UNKNOWN"]
    can_submit: bool
    can_edit: bool = False


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


def _query_id(url: str, name: str) -> str | None:
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
            and _query_id(url, "id") == course_id
        ):
            return True
    return False


def _control_value(form: Tag, name: str) -> str:
    control = form.select_one(f"[name='{name}']")
    return str(control.get("value", "")).strip() if isinstance(control, Tag) else ""


def _assignment_forms(
    soup: BeautifulSoup,
    *,
    base_url: str,
    cmid: int,
    action: str,
) -> list[Tag]:
    result: list[Tag] = []
    for form in soup.select("form"):
        target = _same_origin_url(form.get("action"), base_url)
        allowed_paths = {"/mod/assign/view.php"}
        if action == "confirmsubmit":
            allowed_paths.add("/mod/assign/submissionconfirmform.php")
        if not target or urlsplit(target).path.rstrip("/") not in allowed_paths:
            continue
        form_id = _control_value(form, "id") or _query_id(target, "id")
        form_action = _control_value(form, "action")
        if form_id == str(cmid) and form_action == action:
            result.append(form)
    return result


def _validate_assignment_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
    allow_confirmation: bool = False,
) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    parsed = urlsplit(current_url)
    allowed_paths = {"/mod/assign/view.php"}
    if allow_confirmation:
        allowed_paths.add("/mod/assign/submissionconfirmform.php")
    if (
        parsed.path.rstrip("/") not in allowed_paths
        or _query_id(current_url, "id") != str(cmid)
        or not _course_context(soup, base_url, course_id)
    ):
        raise MoodleMarkupError("Moodle assignment identifiers changed")
    return soup


def parse_assignment_edit_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
) -> AssignmentSubmissionForm:
    """Prove the concrete controls of one real student Assignment form."""

    soup = _validate_assignment_page(
        html,
        current_url,
        base_url=base_url,
        course_id=course_id,
        cmid=cmid,
    )
    forms = _assignment_forms(soup, base_url=base_url, cmid=cmid, action="savesubmission")
    if len(forms) != 1:
        raise MoodleMarkupError("Moodle assignment submission form is ambiguous or missing")
    form = forms[0]

    online_controls = form.select(ASSIGNMENT_ONLINE_TEXT_SELECTOR)
    if len(online_controls) > 1:
        raise MoodleMarkupError("Moodle assignment online-text control is ambiguous")
    managers = form.select(ASSIGNMENT_FILEMANAGER_SELECTOR)
    file_marker = form.select_one("input[name='files_filemanager']")
    if len(managers) > 1:
        raise MoodleMarkupError("Moodle assignment file manager is ambiguous")
    if bool(managers) != isinstance(file_marker, Tag):
        raise MoodleMarkupError("Moodle assignment file manager is not bound to its form")
    if managers and len(managers[0].select(".fp-btn-add")) != 1:
        raise MoodleMarkupError("Moodle assignment file manager has no unambiguous add button")
    if (
        len(
            form.select(
                "button[type='submit'][name='submitbutton'], "
                "input[type='submit'][name='submitbutton']"
            )
        )
        != 1
    ):
        raise MoodleMarkupError("Moodle assignment save trigger is ambiguous or missing")
    statements = form.select("input[name='submissionstatement'][type='checkbox']")
    if len(statements) > 1:
        raise MoodleMarkupError("Moodle assignment submission statement is ambiguous")

    transports: list[Literal["ASSIGN_ONLINE_TEXT", "ASSIGN_FILE"]] = []
    control_name: str | None = None
    if online_controls:
        control_name = str(online_controls[0].get("name", "")).strip()
        if control_name != "onlinetext_editor[text]":
            raise MoodleMarkupError("Moodle assignment online-text control is not canonical")
        transports.append("ASSIGN_ONLINE_TEXT")
    names: set[str] = set()
    attachment_urls: set[tuple[str, str]] = set()
    effective_limits: set[int] = set()
    if managers:
        transports.append("ASSIGN_FILE")
        for node in managers[0].select(
            ".fp-filename, [data-filename], .fp-file [title], .filemanager-container a"
        ):
            href = str(node.get("href", "")).strip()
            target = _same_origin_url(href, base_url) if href else None
            if (
                target
                and urlsplit(target).path.rstrip("/") == "/repository/draftfiles_manager.php"
            ):
                continue
            name = (
                str(node.get("data-filename", "")).strip()
                or str(node.get("title", "")).strip()
                or _text(node, maximum=128)
            )
            if name:
                names.add(name)
        # A rendered filename alone is never enough evidence to overwrite an
        # existing response.  Retain only exact, same-origin Moodle file URLs
        # whose final path component agrees with the rendered filename.  The
        # browser service will require one and only one such URL before it
        # hashes the remote bytes and opens the upload picker.
        for node in managers[0].select(".fp-file, [data-filename], a[href]"):
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
        for node in (managers[0], file_marker):
            if not isinstance(node, Tag):
                continue
            for attribute in ("data-maxbytes", "data-max-bytes", "data-max-file-size"):
                raw_limit = str(node.get(attribute, "")).strip()
                if raw_limit.isdigit() and 0 < int(raw_limit) <= 4 * 1024 * 1024 * 1024:
                    effective_limits.add(int(raw_limit))
        for anchor in managers[0].select("a[href*='/repository/draftfiles_manager.php']"):
            target = _same_origin_url(anchor.get("href"), base_url)
            if (
                target is None
                or urlsplit(target).path.rstrip("/") != "/repository/draftfiles_manager.php"
            ):
                raise MoodleMarkupError("Moodle assignment effective file limit link is invalid")
            values = parse_qs(urlsplit(target).query, keep_blank_values=True).get("maxbytes", [])
            if len(values) != 1 or not values[0].isdigit():
                raise MoodleMarkupError("Moodle assignment effective file limit link is ambiguous")
            limit = int(values[0])
            if 0 < limit <= 4 * 1024 * 1024 * 1024:
                effective_limits.add(limit)
        if len(effective_limits) > 1:
            raise MoodleMarkupError("Moodle assignment effective file limit is ambiguous")
    if not transports:
        raise MoodleMarkupError("Moodle assignment has no supported submission control")
    statement_name = "submissionstatement" if statements else None
    return AssignmentSubmissionForm(
        tuple(transports),
        tuple(sorted(names)),
        control_name,
        statement_name,
        next(iter(effective_limits), None),
        tuple(sorted(attachment_urls)),
    )


def parse_assignment_view_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
) -> AssignmentSubmissionView:
    """Read only stable status classes and the canonical submit-draft form."""

    soup = _validate_assignment_page(
        html,
        current_url,
        base_url=base_url,
        course_id=course_id,
        cmid=cmid,
    )
    submitted = soup.select(".submissionstatussubmitted")
    draft = soup.select(".submissionstatusdraft")
    if submitted and draft:
        raise MoodleMarkupError("Moodle assignment submission status is ambiguous")
    submit_forms = _assignment_forms(soup, base_url=base_url, cmid=cmid, action="submit")
    if len(submit_forms) > 1:
        raise MoodleMarkupError("Moodle assignment final submission trigger is ambiguous")
    if submit_forms:
        triggers = submit_forms[0].select("button[type='submit'], input[type='submit']")
        triggers = [node for node in triggers if str(node.get("name", "")) != "cancel"]
        if len(triggers) != 1:
            raise MoodleMarkupError("Moodle assignment final submission trigger is ambiguous")
    edit_targets: set[str] = set()
    for anchor in soup.select("a[href]"):
        target = _same_origin_url(anchor.get("href"), base_url)
        if (
            target
            and urlsplit(target).path.rstrip("/") == "/mod/assign/view.php"
            and _query_id(target, "id") == str(cmid)
            and parse_qs(urlsplit(target).query, keep_blank_values=True).get("action")
            == ["editsubmission"]
        ):
            edit_targets.add(target)
    status: Literal["DRAFT_SAVED", "FINALIZED", "UNKNOWN"] = "UNKNOWN"
    if submitted:
        status = "FINALIZED"
    elif draft or submit_forms:
        status = "DRAFT_SAVED"
    return AssignmentSubmissionView(status, bool(submit_forms), bool(edit_targets))


def parse_assignment_confirmation_page(
    html: str,
    current_url: str,
    *,
    base_url: str,
    course_id: str,
    cmid: int,
) -> bool:
    """Validate the optional submit-for-grading confirmation form."""

    soup = _validate_assignment_page(
        html,
        current_url,
        base_url=base_url,
        course_id=course_id,
        cmid=cmid,
        allow_confirmation=True,
    )
    forms = _assignment_forms(soup, base_url=base_url, cmid=cmid, action="confirmsubmit")
    if not forms:
        return False
    if len(forms) != 1:
        raise MoodleMarkupError("Moodle assignment confirmation form is ambiguous")
    triggers = forms[0].select("button[type='submit'], input[type='submit']")
    triggers = [node for node in triggers if str(node.get("name", "")) != "cancel"]
    if len(triggers) != 1:
        raise MoodleMarkupError("Moodle assignment confirmation trigger is ambiguous")
    return True


__all__ = [
    "ASSIGNMENT_EDIT_FORM_SELECTOR",
    "ASSIGNMENT_FILEMANAGER_SELECTOR",
    "ASSIGNMENT_FILE_ADD_SELECTOR",
    "ASSIGNMENT_ONLINE_TEXT_SELECTOR",
    "ASSIGNMENT_SAVE_SELECTOR",
    "AssignmentSubmissionForm",
    "AssignmentSubmissionView",
    "parse_assignment_confirmation_page",
    "parse_assignment_edit_page",
    "parse_assignment_view_page",
]
