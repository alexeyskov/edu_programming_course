from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

from .models import (
    Diagnostic,
    DiagnosticRange,
    FixIt,
    RelatedDiagnostic,
    Severity,
)


_TEXT_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+):(?P<column>\d+):\s*"
    r"(?P<severity>fatal error|error|warning|note):\s*(?P<message>.*)$"
)
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


class PathMapper:
    def __init__(self, source_root: Path, build_root: Path, job_root: Path) -> None:
        self.source_root = source_root.resolve()
        self.build_root = build_root.resolve()
        self.job_root = job_root.resolve()

    @staticmethod
    def _uri_to_path(value: str) -> str:
        if value.startswith("file:"):
            parsed = urlparse(value)
            return unquote(parsed.path)
        return value

    def source_path(self, value: str | None) -> str | None:
        if not value:
            return None
        decoded = self._uri_to_path(value)
        normalized = decoded.replace("\\", "/")
        for prefix in ("/workspace/source/", f"{self.source_root.as_posix()}/"):
            if normalized.startswith(prefix):
                return normalized[len(prefix) :]
        if normalized in {"/workspace/source", self.source_root.as_posix()}:
            return None
        for prefix in ("/workspace/build/", f"{self.build_root.as_posix()}/"):
            if normalized.startswith(prefix):
                return f"@build/{normalized[len(prefix) :]}"
        if normalized.startswith("/"):
            return f"@toolchain{normalized}"
        return normalized

    def sanitize_output(self, raw: bytes) -> str:
        text = raw.decode("utf-8", errors="replace")
        text = _ANSI_ESCAPE.sub("", text)
        replacements = (
            (self.source_root.as_posix() + "/", ""),
            (self.build_root.as_posix() + "/", "@build/"),
            (self.job_root.as_posix() + "/", "@job/"),
            ("/workspace/source/", ""),
            ("/workspace/build/", "@build/"),
        )
        for old, new in replacements:
            text = text.replace(old, new)
        # Preserve line feeds/tabs while removing terminal and other control bytes.
        return "".join(char for char in text if char in "\n\r\t" or ord(char) >= 32)


def _severity(value: str | None) -> Severity:
    normalized = (value or "error").lower()
    if normalized in {"warning"}:
        return Severity.WARNING
    if normalized in {"note", "info", "none"}:
        return Severity.INFO
    return Severity.ERROR


def _location_range(location: dict[str, Any] | None) -> DiagnosticRange | None:
    if not location:
        return None
    caret = location.get("caret") or location.get("start") or location
    start = location.get("start") or caret
    finish = location.get("finish") or caret
    try:
        start_line = int(start.get("line") or caret.get("line"))
        start_column = int(
            start.get("display-column")
            or start.get("column")
            or start.get("byte-column")
            or 1
        )
        end_line = int(finish.get("line") or start_line)
        finish_column = int(
            finish.get("display-column")
            or finish.get("column")
            or finish.get("byte-column")
            or start_column
        )
    except (AttributeError, TypeError, ValueError):
        return None
    return DiagnosticRange(
        start_line=max(1, start_line),
        start_column=max(1, start_column),
        end_line=max(1, end_line),
        end_column=max(start_column, finish_column + 1),
    )


def _extract_json_values(text: str) -> list[Any]:
    decoder = json.JSONDecoder()
    values: list[Any] = []
    cursor = 0
    while cursor < len(text):
        candidates = [
            index for token in "[{" if (index := text.find(token, cursor)) >= 0
        ]
        if not candidates:
            break
        start = min(candidates)
        try:
            value, length = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            cursor = start + 1
            continue
        values.append(value)
        cursor = start + length
    return values


def _gcc_fix_its(item: dict[str, Any], mapper: PathMapper) -> list[FixIt]:
    result: list[FixIt] = []
    for fix in item.get("fixits", []):
        start = fix.get("start", {})
        end = fix.get("next", {}) or fix.get("finish", {})
        file_name = mapper.source_path(start.get("file"))
        if not file_name:
            continue
        location = {"start": start, "finish": end or start, "caret": start}
        range_value = _location_range(location)
        if range_value is None:
            continue
        result.append(
            FixIt(
                file=file_name,
                range=range_value,
                replacement=str(fix.get("string", "")),
            )
        )
    return result


def _parse_gcc(
    values: Iterable[Any], mapper: PathMapper, version: str | None
) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for value in values:
        if not isinstance(value, list):
            continue
        for item in value:
            if (
                not isinstance(item, dict)
                or "kind" not in item
                or "message" not in item
            ):
                continue
            locations = item.get("locations") or []
            primary = locations[0] if locations else None
            point = (primary or {}).get("caret") or (primary or {}).get("start") or {}
            related: list[RelatedDiagnostic] = []
            for child in item.get("children", []):
                child_locations = child.get("locations") or []
                child_primary = child_locations[0] if child_locations else None
                child_point = (
                    (child_primary or {}).get("caret")
                    or (child_primary or {}).get("start")
                    or {}
                )
                related.append(
                    RelatedDiagnostic(
                        message=str(child.get("message", "")),
                        file=mapper.source_path(child_point.get("file")),
                        range=_location_range(child_primary),
                    )
                )
            result.append(
                Diagnostic(
                    producer="gcc",
                    producer_version=version,
                    severity=_severity(item.get("kind")),
                    code=item.get("option"),
                    message=str(item.get("message", "")),
                    file=mapper.source_path(point.get("file")),
                    range=_location_range(primary),
                    related=related,
                    fix_its=_gcc_fix_its(item, mapper),
                )
            )
    return result


def _sarif_range(region: dict[str, Any] | None) -> DiagnosticRange | None:
    if not region or "startLine" not in region:
        return None
    start_line = max(1, int(region["startLine"]))
    start_column = max(1, int(region.get("startColumn", 1)))
    end_line = max(start_line, int(region.get("endLine", start_line)))
    end_column = max(start_column, int(region.get("endColumn", start_column + 1)))
    return DiagnosticRange(
        start_line=start_line,
        start_column=start_column,
        end_line=end_line,
        end_column=end_column,
    )


def _sarif_location(
    location: dict[str, Any] | None,
    artifacts: list[dict[str, Any]],
    mapper: PathMapper,
) -> tuple[str | None, DiagnosticRange | None]:
    physical = (location or {}).get("physicalLocation", {})
    artifact = physical.get("artifactLocation", {})
    uri = artifact.get("uri")
    index = artifact.get("index")
    if uri is None and isinstance(index, int) and 0 <= index < len(artifacts):
        uri = artifacts[index].get("location", {}).get("uri")
    return mapper.source_path(uri), _sarif_range(physical.get("region"))


def _sarif_artifact_path(
    artifact: dict[str, Any],
    artifacts: list[dict[str, Any]],
    mapper: PathMapper,
) -> str | None:
    uri = artifact.get("uri")
    index = artifact.get("index")
    if uri is None and isinstance(index, int) and 0 <= index < len(artifacts):
        uri = artifacts[index].get("location", {}).get("uri")
    return mapper.source_path(uri)


def _sarif_fix_its(
    item: dict[str, Any],
    artifacts: list[dict[str, Any]],
    mapper: PathMapper,
) -> list[FixIt]:
    result: list[FixIt] = []
    for fix in item.get("fixes", []):
        for change in fix.get("artifactChanges", []):
            file_name = _sarif_artifact_path(
                change.get("artifactLocation", {}), artifacts, mapper
            )
            if not file_name:
                continue
            for replacement in change.get("replacements", []):
                range_value = _sarif_range(replacement.get("deletedRegion"))
                if range_value is None:
                    continue
                result.append(
                    FixIt(
                        file=file_name,
                        range=range_value,
                        replacement=str(
                            replacement.get("insertedContent", {}).get("text", "")
                        ),
                    )
                )
    return result


def _parse_sarif(values: Iterable[Any], mapper: PathMapper) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for value in values:
        if not isinstance(value, dict) or "runs" not in value:
            continue
        for run in value.get("runs", []):
            driver = run.get("tool", {}).get("driver", {})
            producer = str(driver.get("name") or "clang")
            version = driver.get("version")
            artifacts = run.get("artifacts", [])
            for item in run.get("results", []):
                locations = item.get("locations") or []
                file_name, range_value = _sarif_location(
                    locations[0] if locations else None, artifacts, mapper
                )
                related: list[RelatedDiagnostic] = []
                for related_item in locations[1:] + item.get("relatedLocations", []):
                    related_file, related_range = _sarif_location(
                        related_item, artifacts, mapper
                    )
                    related.append(
                        RelatedDiagnostic(
                            message=str(
                                related_item.get("message", {}).get(
                                    "text", "related location"
                                )
                            ),
                            file=related_file,
                            range=related_range,
                        )
                    )
                result.append(
                    Diagnostic(
                        producer=producer,
                        producer_version=str(version) if version else None,
                        severity=_severity(item.get("level")),
                        code=str(item["ruleId"])
                        if item.get("ruleId") is not None
                        else None,
                        message=str(item.get("message", {}).get("text", "")),
                        file=file_name,
                        range=range_value,
                        related=related,
                        fix_its=_sarif_fix_its(item, artifacts, mapper),
                    )
                )
    return result


def _parse_text(
    text: str, mapper: PathMapper, producer: str, version: str | None
) -> list[Diagnostic]:
    result: list[Diagnostic] = []
    for line in text.splitlines():
        match = _TEXT_DIAGNOSTIC.match(line.strip())
        if not match:
            continue
        file_name = mapper.source_path(match.group("file"))
        line_number = int(match.group("line"))
        column = int(match.group("column"))
        result.append(
            Diagnostic(
                producer=producer,
                producer_version=version,
                severity=_severity(match.group("severity")),
                message=match.group("message"),
                file=file_name,
                range=DiagnosticRange(
                    start_line=line_number,
                    start_column=column,
                    end_line=line_number,
                    end_column=column + 1,
                ),
            )
        )
    return result


def parse_compiler_diagnostics(
    raw_stderr: bytes,
    *,
    compiler_family: str,
    compiler_version: str | None,
    mapper: PathMapper,
) -> list[Diagnostic]:
    text = raw_stderr.decode("utf-8", errors="replace")
    values = _extract_json_values(text)
    if compiler_family == "gcc":
        diagnostics = _parse_gcc(values, mapper, compiler_version)
    else:
        diagnostics = _parse_sarif(values, mapper)
    if diagnostics:
        return diagnostics
    return _parse_text(text, mapper, compiler_family, compiler_version)
