from __future__ import annotations

from pathlib import Path

from edu_runner.diagnostics import PathMapper, parse_compiler_diagnostics


def test_parses_gcc_json_and_remaps_executor_path(tmp_path: Path) -> None:
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    raw = b"""[
      {
        "kind": "error",
        "message": "'answer' was not declared in this scope",
        "option": "-Wtemplate-body",
        "locations": [{
          "caret": {"file": "/workspace/source/src/main.cpp", "line": 2, "display-column": 10},
          "start": {"file": "/workspace/source/src/main.cpp", "line": 2, "display-column": 10},
          "finish": {"file": "/workspace/source/src/main.cpp", "line": 2, "display-column": 15}
        }],
        "children": []
      }
    ]"""
    diagnostics = parse_compiler_diagnostics(
        raw,
        compiler_family="gcc",
        compiler_version="g++ 14.2",
        mapper=PathMapper(source, build, tmp_path),
    )
    assert len(diagnostics) == 1
    diagnostic = diagnostics[0]
    assert diagnostic.producer == "gcc"
    assert diagnostic.file == "src/main.cpp"
    assert diagnostic.code == "-Wtemplate-body"
    assert diagnostic.range is not None
    assert diagnostic.range.start_line == 2
    assert diagnostic.range.start_column == 10
    assert diagnostic.range.end_column == 16


def test_parses_clang_sarif_fix_it(tmp_path: Path) -> None:
    source = tmp_path / "source"
    build = tmp_path / "build"
    source.mkdir()
    build.mkdir()
    raw = b"""{
      "version": "2.1.0",
      "runs": [{
        "tool": {"driver": {"name": "clang", "version": "19"}},
        "artifacts": [{"location": {"uri": "file:///workspace/source/main.cpp"}}],
        "results": [{
          "level": "error",
          "ruleId": "123",
          "message": {"text": "expected ';'"},
          "locations": [{"physicalLocation": {
            "artifactLocation": {"index": 0},
            "region": {"startLine": 1, "startColumn": 10, "endColumn": 10}
          }}],
          "fixes": [{"artifactChanges": [{
            "artifactLocation": {"index": 0},
            "replacements": [{
              "deletedRegion": {"startLine": 1, "startColumn": 10, "endColumn": 10},
              "insertedContent": {"text": ";"}
            }]
          }]}]
        }]
      }]
    }"""
    diagnostics = parse_compiler_diagnostics(
        raw,
        compiler_family="clang",
        compiler_version="clang 19",
        mapper=PathMapper(source, build, tmp_path),
    )
    assert diagnostics[0].file == "main.cpp"
    assert diagnostics[0].fix_its[0].replacement == ";"
    assert diagnostics[0].fix_its[0].file == "main.cpp"
