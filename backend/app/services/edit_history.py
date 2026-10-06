"""Exact edit descriptions; legacy replay is read-only and content-hash checked."""

from __future__ import annotations

import uuid
from typing import Any

from app.models.attempts import EditEvent, Snapshot
from app.services.common import sha256_text


def _position(content: str, offset: int) -> tuple[int, int]:
    prefix = content[:offset]
    line = prefix.count("\n") + 1
    line_prefix = prefix.rsplit("\n", 1)[-1]
    # Monaco columns are one-based UTF-16 offsets, not UTF-8 bytes or Python's
    # codepoint offsets. Tabs count as one column, just as in editor selections.
    return line, len(line_prefix.encode("utf-16-le")) // 2 + 1


def describe_delta(previous: str, change: dict[str, Any]) -> dict[str, Any]:
    offset, count = change["offset"], change["delete_count"]
    start_line, start_column = _position(previous, offset)
    end_line, end_column = _position(previous, offset + count)
    return {
        "deleted_text": previous[offset:offset + count],
        "start_line": start_line, "start_column": start_column,
        "end_line": end_line, "end_column": end_column,
        "column_encoding": "utf16",
    }


def _matches(content: str | None, digest: Any) -> bool:
    return isinstance(content, str) and isinstance(digest, str) and sha256_text(content) == digest


def edit_details(
    events: list[EditEvent], snapshots: list[Snapshot],
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Do not alter EditEvent.changes or its hash chain when enriching old rows."""
    anchors = iter(sorted(snapshots, key=lambda item: item.revision))
    anchor = next(anchors, None)
    contents: dict[str, str] = {}
    result: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for event in sorted(events, key=lambda item: item.sequence):
        while anchor is not None and anchor.revision < event.sequence:
            contents = {
                str(item["id"]): item["content"] for item in anchor.files
                if isinstance(item, dict) and item.get("id")
                and isinstance(item.get("content"), str)
            }
            anchor = next(anchors, None)
        file_id = str(event.file_id)
        details: list[dict[str, Any]] = []
        for change in event.changes:
            if not isinstance(change, dict):
                continue
            kind = change.get("type")
            if kind == "contiguous_delta":
                offset, count, inserted = (
                    change.get("offset"), change.get("delete_count"), change.get("insert_text"),
                )
                if (
                    type(offset) is not int or type(count) is not int
                    or offset < 0 or count < 0 or not isinstance(inserted, str)
                    or change.get("offset_encoding") != "unicode_codepoint"
                ):
                    contents.pop(file_id, None)
                    continue
                description = {
                    key: change[key] for key in (
                        "deleted_text", "start_line", "start_column", "end_line", "end_column",
                    ) if key in change
                }
                previous = contents.pop(file_id, None)
                if _matches(previous, change.get("previous_content_hash")) and (
                    offset + count <= len(previous)
                ):
                    updated = previous[:offset] + inserted + previous[offset + count:]
                    if _matches(updated, change.get("content_hash")):
                        description = describe_delta(previous, change)
                        contents[file_id] = updated
                deleted = description.get("deleted_text")
                if not isinstance(deleted, str) or len(deleted) != count:
                    deleted = "" if count == 0 else None
                coordinates = {}
                if all(type(description.get(key)) is int and description[key] >= 1 for key in (
                    "start_line", "start_column", "end_line", "end_column",
                )):
                    coordinates = {key: description[key] for key in (
                        "start_line", "start_column", "end_line", "end_column",
                    )}
                details.append({
                    "operation": "replace" if count and inserted else "delete" if count
                    else "insert" if inserted else "unchanged",
                    "offset": offset, "delete_count": count,
                    "insert_text": inserted, "deleted_text": deleted, **coordinates,
                })
            elif kind == "file_create":
                content = change.get("content")
                if _matches(content, change.get("content_hash")):
                    contents[file_id] = content
                else:
                    contents.pop(file_id, None)
                    content = None
                details.append({
                    "operation": "create", "insert_text": content, "deleted_text": "",
                    "start_line": 1, "start_column": 1, "end_line": 1, "end_column": 1,
                })
            elif kind == "file_delete":
                content = change.get("deleted_text", contents.get(file_id))
                coordinates = {}
                if _matches(content, change.get("content_hash")):
                    end_line, end_column = _position(content, len(content))
                    coordinates = {
                        "start_line": 1, "start_column": 1,
                        "end_line": end_line, "end_column": end_column,
                    }
                else:
                    content = None
                contents.pop(file_id, None)
                details.append({
                    "operation": "remove", "insert_text": "", "deleted_text": content,
                    "delete_count": len(content) if content is not None else None, **coordinates,
                })
        result[event.id] = details
    return result
