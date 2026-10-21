"""Preserve task images and TeX without passing Moodle HTML to the application.

The connector emits canonical image tokens and explicit TeX delimiters in
otherwise plain text. Browsers never receive Moodle cookies or execute its
MathJax/HTML; the application loads images through its authenticated endpoint.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from .config import exact_https_origin

_FORMULA_PATH = re.compile(r"/filter/tex/pix\.php/[a-fA-F0-9]{16,128}\.(?:png|gif|jpe?g)", re.I)
_RASTER_SUFFIX = re.compile(r"\.(?:png|gif|jpe?g|webp)$", re.I)
_IMAGE_TOKEN = re.compile(r"!\[(?:\\.|[^\]\\\r\n])*\]\(<https://[^<>\s]+>\)")
_WHITESPACE = re.compile(r"\s+")
_MAX_IMAGES = 32
_MATH_TOKEN = re.compile(r"\\\((?:(?!\\\)).){1,4096}?\\\)|\\\[(?:(?!\\\]).){1,4096}?\\\]", re.S)
_TEX_SCRIPT = re.compile(r"^math/tex(?:\s*;\s*mode\s*=\s*(?:display|inline))?\s*$", re.I)


@dataclass(frozen=True, slots=True)
class StatementContent:
    text: str
    legacy_hashes: tuple[str, ...] = ()


def _tex_source(value: str) -> str:
    # Old plain-text import folds newlines. Strip TeX comments first so a
    # comment cannot swallow the rest of a formula after that whitespace fold.
    lines = []
    for line in value.splitlines():
        for index, character in enumerate(line):
            if character != "%":
                continue
            preceding = len(line[:index]) - len(line[:index].rstrip("\\"))
            if preceding % 2 == 0:
                line = line[:index]
                break
        lines.append(line)
    return _WHITESPACE.sub(" ", " ".join(lines)).strip()


def preserve_statement_math(fragment: Tag | BeautifulSoup) -> bool:
    """Retain TeX, not MathJax's positioned spans or duplicate preview text.

    Remove only output paired with an actual TeX source. Some Moodle question
    authors accidentally wrap ordinary instructions in ``MathJax`` spans too;
    deleting every such span would silently delete part of the task.
    """

    changed = False
    count = 0
    for script in list(fragment.find_all("script")):
        kind = str(script.get("type") or "")
        if _TEX_SCRIPT.fullmatch(kind) is None:
            continue
        tex = _tex_source(script.get_text())
        if not tex or len(tex) > 4096 or count >= 64:
            continue
        frame = None
        source_id = str(script.get("id") or "")
        if source_id:
            frame = fragment.find(id=source_id + "-Frame")
        previous = script.find_previous_sibling()
        if frame is None and isinstance(previous, Tag) and set(previous.get("class") or ()) & {
            "MathJax", "MathJax_SVG", "MathJax_CHTML", "MathJax_Display",
        }:
            frame = previous
        # A formula in its own paragraph is a display equation even when a
        # copied Moodle MathJax script says inline (as in older quiz questions).
        parent = script.parent
        standalone = False
        if isinstance(parent, Tag) and parent.name in {"p", "div"}:
            ignored = {"MathJax", "MathJax_SVG", "MathJax_CHTML", "MathJax_Preview"}
            standalone = all(
                child.name in {"script", "br"}
                or bool(set(child.get("class") or ()) & ignored)
                or (child.name != "img" and not child.get_text(strip=True))
                if isinstance(child, Tag) else not str(child).strip()
                for child in parent.children
            )
        preview = frame.find_previous_sibling() if frame is not None else previous
        if isinstance(preview, Tag) and "MathJax_Preview" in (preview.get("class") or ()):
            preview.decompose()
        if frame is not None and frame is not script and script not in frame.descendants:
            frame.decompose()
        display = "display" in kind.lower() or standalone
        opening, closing = (r"\[", r"\]") if display else (r"\(", r"\)")
        script.replace_with(f" {opening}{tex}{closing} ")
        changed = True
        count += 1
    # MathJax 3 / MathML output can carry its original TeX in a semantic
    # annotation rather than a v2 script. Never pass its HTML through.
    for annotation in list(fragment.select('annotation[encoding="application/x-tex"]')):
        container = annotation.find_parent("mjx-container") or annotation.find_parent("math")
        tex = _tex_source(annotation.get_text())
        if (
            container is None or container.parent is None
            or not tex or len(tex) > 4096 or count >= 64
        ):
            continue
        display = container.get("display") in {"block", "true"}
        opening, closing = (r"\[", r"\]") if display else (r"\(", r"\)")
        container.replace_with(f" {opening}{tex}{closing} ")
        changed = True
        count += 1
    return changed


def statement_image_url(value: str | None, base_url: str | None) -> str | None:
    """Accept only raster-image Moodle endpoints on the configured origin."""

    if not isinstance(value, str) or not value or len(value) > 4_096 or not base_url:
        return None
    if any(ord(character) < 32 for character in value) or "\\" in value:
        return None
    try:
        origin = exact_https_origin(base_url)
        absolute = urljoin(f"{origin}/", value)
        parsed = urlsplit(absolute)
        source_path = urlsplit(value).path
        candidate = exact_https_origin(f"{parsed.scheme}://{parsed.netloc}")
    except (ValueError, TypeError):
        return None
    if candidate != origin or parsed.username or parsed.password or parsed.fragment:
        return None
    # Moodle-session or web-service tokens in media URLs must never be copied
    # into a student-visible statement. Only harmless rendering flags qualify.
    query_values = parse_qs(parsed.query, keep_blank_values=True)
    for name, values in query_values.items():
        if len(values) != 1:
            return None
        if name == "forcedownload":
            if values[0] not in {"0", "1"}:
                return None
        elif name in {"rev", "cache"}:
            if not re.fullmatch(r"[0-9]{1,20}", values[0]):
                return None
        else:
            return None
    # Check the source as well as the absolute URL: urljoin normalizes literal
    # dot segments, which must not silently turn a rejected path into a valid one.
    for path_to_check in (source_path, parsed.path):
        decoded_path = path_to_check
        for _ in range(3):
            decoded_path = unquote(decoded_path)
            if "\\" in decoded_path or any(ord(character) < 32 for character in decoded_path):
                return None
            if any(part in {".", ".."} for part in decoded_path.split("/")):
                return None
    formula = _FORMULA_PATH.fullmatch(decoded_path) is not None
    attachment = decoded_path.startswith(("/pluginfile.php/", "/file.php/")) and bool(
        _RASTER_SUFFIX.search(decoded_path)
    )
    if not formula and not attachment:
        return None
    # Angle destinations are unambiguous even when a file name contains
    # parentheses. Encode whitespace and token delimiters before serializing.
    path = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~")
    normalized = urlunsplit(("https", urlsplit(origin).netloc, path, query, ""))
    return normalized if len(normalized) <= 4_096 else None


def preserve_statement_images(fragment: Tag | BeautifulSoup, *, base_url: str | None) -> None:
    """Replace image nodes in place, retaining readable alt text on rejection."""

    for index, image in enumerate(fragment.find_all("img")):
        alt = _WHITESPACE.sub(" ", str(image.get("alt") or image.get("title") or "")).strip()
        alt = alt[:2_048]
        source = statement_image_url(image.get("src"), base_url) if index < _MAX_IMAGES else None
        if source:
            escaped_alt = alt.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")
            image.replace_with(f" ![{escaped_alt}](<{source}>) ")
        else:
            image.replace_with(f" {alt} " if alt else " ")


def truncate_statement(value: str, *, maximum: int) -> str:
    """Keep a bounded text field without serializing half an image or formula."""

    if len(value) <= maximum:
        return value
    tokens = sorted(
        (*_IMAGE_TOKEN.finditer(value), *_MATH_TOKEN.finditer(value)),
        key=lambda match: match.start(),
    )
    for token in tokens:
        if token.start() < maximum < token.end():
            return value[:token.start()].rstrip()
        if token.start() >= maximum:
            break
    return value[:maximum]


def statement_node_text(node: Tag | None, *, base_url: str, maximum: int = 50_000) -> str:
    """Match the existing whitespace-normalized live/review text contract."""

    return statement_node_content(node, base_url=base_url, maximum=maximum).text


def statement_node_content(
    node: Tag | None, *, base_url: str, maximum: int = 50_000,
) -> StatementContent:
    """Return rich text and proof of its two former plain-text representations."""

    if node is None:
        return StatementContent("")
    fragment = BeautifulSoup(str(node), "html.parser")
    legacy_plain = _WHITESPACE.sub(" ", fragment.get_text(" ", strip=True)).strip()[:maximum]
    math_changed = preserve_statement_math(fragment)
    hashes: tuple[str, ...] = ()
    if math_changed:
        legacy_images = BeautifulSoup(str(node), "html.parser")
        preserve_statement_images(legacy_images, base_url=base_url)
        legacy_image_text = truncate_statement(
            _WHITESPACE.sub(" ", legacy_images.get_text(" ", strip=True)).strip(), maximum=maximum
        )
        hashes = tuple(dict.fromkeys(
            hashlib.sha256(" ".join(text.split()).encode()).hexdigest()
            for text in (legacy_plain, legacy_image_text)
        ))
    preserve_statement_images(fragment, base_url=base_url)
    value = _WHITESPACE.sub(" ", fragment.get_text(" ", strip=True)).strip()
    return StatementContent(truncate_statement(value, maximum=maximum), hashes)
