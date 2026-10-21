"""The narrow image-only markup emitted by the Moodle statement parser.

Everything outside these canonical image markers remains literal source text.
This module neither fetches URLs nor grants permission to access an image.
"""

from __future__ import annotations

import re

# Only bracket/backslash escapes are emitted in alt text. TeX backslashes are
# doubled by the connector, then unescaped by the frontend's matching parser.
STATEMENT_IMAGE_PATTERN = re.compile(
    r"!\[((?:\\[\\\[\]]|[^\]\\\r\n])*)\]\(<([^<>\s\x00-\x1f\x7f]+)>\)"
)
STATEMENT_MATH_PATTERN = re.compile(
    r"\\\((?:(?!\\\)).){1,4096}?\\\)|\\\[(?:(?!\\\]).){1,4096}?\\\]", re.S
)


def statement_image_urls(text: str) -> list[str]:
    """Return unique declared image URLs, in statement order, not HTML links."""

    return list(dict.fromkeys(match.group(2) for match in STATEMENT_IMAGE_PATTERN.finditer(text)))


def statement_without_images(text: str) -> str:
    """Match the legacy import, which dropped img elements including alt text."""

    return STATEMENT_IMAGE_PATTERN.sub("", text)
