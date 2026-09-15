"""Read Moodle's native draft-file contract without running its filepicker JS.

These are the student's normal web endpoints, not admin/webservice APIs. Draft
IDs and repository options must come from the exact, freshly read Essay form.
No JavaScript from the document is evaluated and no draft ID is cached/reused
across pages, questions, attempts or students.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

from bs4 import BeautifulSoup
from playwright.async_api import APIRequestContext

from .parsers import MoodleMarkupError
from .quiz import QuizAttempt, essay_slot_selector


class DraftUnavailable(RuntimeError):
    """A temporary HTTP failure; the durable outbox may retry the same snapshot."""


class DraftSessionExpired(RuntimeError):
    """Moodle explicitly rejected the student's authentication/session key."""


@dataclass(frozen=True)
class EssayDraft:
    itemid: str
    sesskey: str
    repo_id: str
    client_id: str
    context_id: str
    maxbytes: int
    areamaxbytes: int
    accepted_types: tuple[str, ...]
    author: str
    license: str
    files: tuple[tuple[str, str], ...]


def _positive(value: Any) -> str:
    if not re.fullmatch(r"[1-9][0-9]{0,19}", str(value)):
        raise MoodleMarkupError("Moodle draft identifier is invalid")
    return str(value)


def draft_url(value: Any, *, base_url: str, itemid: str, filename: str) -> str:
    if not isinstance(value, str) or len(value) > 4096:
        raise MoodleMarkupError("Moodle draft file URL is missing")
    value = html.unescape(value)
    url = urlsplit(value)
    path = unquote(url.path)
    if (
        f"{url.scheme}://{url.netloc}" != base_url
        or url.username
        or url.password
        or url.fragment
        or not re.fullmatch(
            rf"/draftfile\.php/[1-9][0-9]*/user/draft/{re.escape(itemid)}/" + re.escape(filename),
            path,
        )
        or not filename
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
    ):
        raise MoodleMarkupError("Moodle draft file URL identifies a foreign file")
    return value


def parse_essay_draft(
    markup: str,
    *,
    base_url: str,
    cmid: int,
    question: QuizAttempt,
) -> EssayDraft:
    if len(markup.encode("utf-8")) > 2 * 1024 * 1024:
        raise MoodleMarkupError("Moodle draft form is too large")
    soup = BeautifulSoup(markup, "html.parser")
    essays = soup.select(essay_slot_selector(question.question_slot))
    if len(essays) != 1:
        raise MoodleMarkupError("Moodle draft Essay is ambiguous")
    essay = essays[0]
    form = essay.find_parent("form", id="responseform")
    if form is None or str(form.get("method", "")).lower() != "post":
        raise MoodleMarkupError("Moodle draft response form is missing")
    action = urlsplit(str(form.get("action", "")))
    if (
        f"{action.scheme}://{action.netloc}" != base_url
        or action.path != "/mod/quiz/processattempt.php"
        or parse_qs(action.query).get("cmid") not in (None, [str(cmid)])
        or action.username
        or action.password
        or action.fragment
    ):
        raise MoodleMarkupError("Moodle draft response form target changed")

    def field(name: str) -> str:
        matches = form.select(f'input[type="hidden"][name="{name}"]')
        if len(matches) != 1:
            raise MoodleMarkupError("Moodle draft response field is ambiguous")
        return str(matches[0].get("value", ""))

    if field("attempt") != question.attempt_id:
        raise MoodleMarkupError("Moodle draft response attempt changed")
    sesskey = field("sesskey")
    if not re.fullmatch(r"[A-Za-z0-9]{1,128}", sesskey):
        raise MoodleMarkupError("Moodle draft session key is invalid")
    attachments = [
        node
        for node in essay.select('input[type="hidden"][name]')
        if re.fullmatch(rf"q[0-9]+:{question.question_slot}_attachments", str(node["name"]))
    ]
    if len(attachments) != 1 or len(essay.select(".filemanager")) != 1:
        raise MoodleMarkupError("Moodle draft attachment control is ambiguous")
    itemid = _positive(attachments[0].get("value"))
    options = []
    for script in soup.select("script"):
        source = script.string or ""
        for match in re.finditer(r"M\.form_filemanager\.init\(\s*Y\s*,\s*", source):
            try:
                value, _ = json.JSONDecoder().raw_decode(source[match.end() :])
            except (ValueError, RecursionError) as exc:
                raise MoodleMarkupError("Moodle draft bootstrap is invalid") from exc
            if isinstance(value, dict) and str(value.get("itemid")) == itemid:
                options.append(value)
    if len(options) != 1:
        raise MoodleMarkupError("Moodle draft bootstrap is missing or ambiguous")
    data = options[0]
    try:
        context_id = _positive(data["context"]["id"])
        if str(data["context"].get("instanceid")) != str(cmid):
            raise MoodleMarkupError("Moodle draft context identifies another activity")
        repositories = data["filepicker"]["repositories"]
        uploads = [repo for repo in repositories.values() if repo.get("type") == "upload"]
        if len(uploads) != 1:
            raise MoodleMarkupError("Moodle draft upload repository is ambiguous")
        repo_id = _positive(uploads[0]["id"])
        client_id = str(data["client_id"])
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id):
            raise MoodleMarkupError("Moodle draft client identifier is invalid")
        types = data["accepted_types"]
        if not isinstance(types, list) or any(not isinstance(t, str) for t in types):
            raise MoodleMarkupError("Moodle draft accepted types are invalid")
        files = []
        for entry in data["list"]:
            # Preserve all unrelated files/folders. Only root files with an
            # exact same-draft URL can ever become an overwrite/delete target.
            if entry.get("type") == "folder":
                continue
            name = str(entry["filename"])
            if entry.get("filepath") != "/" or entry.get("isref"):
                raise MoodleMarkupError("Moodle draft contains unsupported file references")
            url = draft_url(entry["url"], base_url=base_url, itemid=itemid, filename=name)
            files.append((name, url))
        if len({name for name, _ in files}) != len(files):
            raise MoodleMarkupError("Moodle draft filenames are ambiguous")
        return EssayDraft(
            itemid=itemid,
            sesskey=sesskey,
            repo_id=repo_id,
            client_id=client_id,
            context_id=context_id,
            maxbytes=int(data["maxbytes"]),
            areamaxbytes=int(data["areamaxbytes"]),
            accepted_types=tuple(types),
            author=str(data.get("author", "")),
            license=str(data["defaultlicense"]),
            files=tuple(files),
        )
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise MoodleMarkupError("Moodle draft options are incomplete") from exc


async def draft_request(
    client: APIRequestContext,
    *,
    base_url: str,
    draft: EssayDraft,
    action: str,
    timeout_ms: int,
    filename: str,
    artifact: bytes | None = None,
    mime_type: str = "application/octet-stream",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Exactly one native student draft operation; never follow a POST redirect."""
    if action not in {"upload", "delete"}:
        raise MoodleMarkupError("Unsupported Moodle draft operation")
    params = {"sesskey": draft.sesskey, "itemid": draft.itemid}
    endpoint = "repository_ajax.php" if action == "upload" else "draftfiles_ajax.php"
    query = [("action", action)]
    if action == "upload":
        if not artifact:
            raise MoodleMarkupError("Moodle requires a nonempty upload (use a ZIP for empty code)")
        if draft.maxbytes > 0 and len(artifact) > draft.maxbytes:
            raise MoodleMarkupError("Moodle draft upload exceeds its file limit")
        query.extend(("accepted_types[]", value) for value in draft.accepted_types)
        params.update(
            {
                "repo_id": draft.repo_id,
                "client_id": draft.client_id,
                "ctx_id": draft.context_id,
                "env": "filemanager",
                "savepath": "/",
                "maxbytes": str(draft.maxbytes),
                "areamaxbytes": str(draft.areamaxbytes),
                "title": filename,
                "author": draft.author,
                "license": draft.license,
                "overwrite": "1" if overwrite else "0",
            }
        )
        body = {
            "multipart": {
                **params,
                "repo_upload_file": {
                    "name": filename,
                    "mimeType": mime_type,
                    "buffer": artifact,
                },
            }
        }
    else:
        params.update({"filename": filename, "filepath": "/"})
        body = {"form": params}
    url = f"{base_url}/repository/{endpoint}?{urlencode(query)}"
    response = await client.post(url, **body, timeout=timeout_ms, max_redirects=0)
    try:
        if response.status >= 500 or response.status == 429:
            raise DraftUnavailable("Moodle draft endpoint is temporarily unavailable")
        if response.status == 401:
            raise DraftSessionExpired("Moodle draft session expired")
        if response.status != 200:
            raise MoodleMarkupError("Moodle draft request did not return HTTP 200")
        raw = await response.body()
        if len(raw) > 1024 * 1024:
            raise MoodleMarkupError("Moodle draft response is too large")
        try:
            result = json.loads(raw)
        except (ValueError, RecursionError) as exc:
            raise MoodleMarkupError("Moodle draft response is not JSON") from exc
        if isinstance(result, dict) and result.get("errorcode") in {
            "invalidsesskey",
            "notloggedin",
            "requireloginerror",
        }:
            raise DraftSessionExpired("Moodle draft session expired")
        if not isinstance(result, dict) or result.get("error") or result.get("exception"):
            raise MoodleMarkupError("Moodle rejected the draft file operation")
        if action == "upload":
            if str(result.get("id")) != draft.itemid or result.get("file") != filename:
                raise MoodleMarkupError("Moodle did not acknowledge the exact uploaded file")
            draft_url(result.get("url"), base_url=base_url, itemid=draft.itemid, filename=filename)
        elif result.get("filepath") != "/":
            raise MoodleMarkupError("Moodle did not acknowledge the managed draft deletion")
        return result
    finally:
        await response.dispose()
