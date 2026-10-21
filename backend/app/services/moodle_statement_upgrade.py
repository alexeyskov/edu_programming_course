"""One-way repair for statements imported before image preservation existed."""

from __future__ import annotations

from app.services.common import sha256_text
from app.services.statement_markup import (
    STATEMENT_MATH_PATTERN,
    statement_image_urls,
    statement_without_images,
)

STATEMENT_FORMAT = "MOODLE_TEXT_IMAGES_MATH_V2"
LEGACY_STATEMENT_FORMATS = frozenset({None, "MOODLE_TEXT_IMAGES_V1"})


def is_statement_math_upgrade(previous: str, enriched: str, legacy_hashes: tuple[str, ...]) -> bool:
    """Prove old MathJax flattening against the *same* current Moodle DOM.

    Proof comes only from the signed internal connector, not student input.
    Callers still enforce the pinned activity, attempt, slot, mark and transport
    and never apply this repair twice to an already-upgraded version.
    """

    return bool(
        STATEMENT_MATH_PATTERN.search(enriched)
        and sha256_text(" ".join(previous.split())) in legacy_hashes
    )


def is_statement_image_upgrade(previous: str, enriched: str) -> bool:
    """Only restore formerly dropped img elements, never replace task words.

    Callers must also prove the same Moodle attempt/question/answer transport
    and a legacy task version with no statement-format marker. Image access is
    independently checked by the authenticated proxy, not authorized here.
    """

    if not previous.strip() or statement_image_urls(previous) or not statement_image_urls(enriched):
        return False
    return " ".join(previous.split()) == " ".join(statement_without_images(enriched).split())
