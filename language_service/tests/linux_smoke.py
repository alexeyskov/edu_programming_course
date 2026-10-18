"""Run inside the production image: python /testing/tests/linux_smoke.py.

No pytest/development packages are required. Mount language_service at /testing
read-only for the optional C policy probe; /tmp must permit its test executable.
Set CODE_INTELLIGENCE_ISOLATION=container (default) or landlock to exercise the
actual production launcher. No test bypass is used by the service smoke.
"""

from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import tempfile
from pathlib import Path

from edu_language.config import Settings
from edu_language.models import Query
from edu_language.service import LanguageService


async def main() -> None:
    assert platform.system() == "Linux", "this smoke test verifies the Linux sandbox"
    root = Path(
        os.environ.get("CODE_INTELLIGENCE_WORK_ROOT", "/tmp/edu-language-smoke")
    )
    settings = Settings(
        shared_secret=b"smoke-test-not-production-secret-123",
        work_root=root,
        isolation=os.environ.get("CODE_INTELLIGENCE_ISOLATION", "container"),
    )
    service = LanguageService(settings)
    await service.start()
    assert service.ready, f"{settings.isolation}/seccomp/clangd readiness failed"
    try:
        source = "#include <vector>\nint main(){ std::vector<int> values; values. }\n"
        result = await service.query(
            Query(
                workspace_id="a" * 64,
                build_profile="cpp-gcc-c++20-single",
                files=[{"path": "main.cpp", "content": source}],
                path="main.cpp",
                method="completion",
                position={
                    "line": 1,
                    "character": source.splitlines()[1].index("values.")
                    + len("values."),
                },
            )
        )
        names = {item.get("filterText") for item in result["result"]["items"]}
        assert "push_back" in names and "size" in names, result
        print("PASS: sandboxed standard-library member completion")

        files = [
            {
                "path": "main.cpp",
                "content": '#include "helper.hpp"\nint main(){ return answer(); }\n',
            },
            {"path": "helper.hpp", "content": "inline int answer() { return 42; }\n"},
        ]
        result = await service.query(
            Query(
                workspace_id="a" * 64,
                build_profile="cpp-gcc-c++20-single",
                files=files,
                path="main.cpp",
                method="definition",
                position={"line": 1, "character": 21},
            )
        )
        assert "helper.hpp" in str(result["result"]) and "file://" not in str(result)
        print("PASS: sandboxed cross-file definition with relative path")

        for method, code, position in [
            (
                "hover",
                "int value = 1;\nint main(){ return value; }\n",
                {"line": 1, "character": 20},
            ),
            (
                "signatureHelp",
                "int add(int left, int right);\nint main(){ add(1, ); }\n",
                {"line": 1, "character": 18},
            ),
            ("formatting", "int main(){return 0;}\n", None),
            ("diagnostics", "int bad = ;\n", None),
        ]:
            result = await service.query(
                Query(
                    workspace_id="a" * 64,
                    build_profile="cpp-gcc-c++20-single",
                    files=[{"path": "main.cpp", "content": code}],
                    path="main.cpp",
                    method=method,
                    position=position,
                )
            )
            if method == "diagnostics":
                assert any(entry["diagnostics"] for entry in result["diagnostics"])
            else:
                assert result["result"], (method, result)
        print(
            f"PASS: {settings.isolation} hover, signatures, formatting and diagnostics"
        )

        if settings.isolation == "landlock":
            with tempfile.TemporaryDirectory(
                prefix="other-student-", dir=root
            ) as other:
                private = Path(other) / "confidential.hpp"
                private.write_text("inline int PRIVATE_CANARY_STUDENT_SYMBOL = 42;\n")
                for blocked in [
                    str(private),
                    "/proc/self/environ",
                    "/etc/passwd",
                    "/opt/language-service/src/edu_language/config.py",
                ]:
                    code = f'#include "{blocked}"\nint main() {{ return 0; }}\n'
                    result = await service.query(
                        Query(
                            workspace_id="a" * 64,
                            build_profile="cpp-gcc-c++20-single",
                            files=[{"path": "main.cpp", "content": code}],
                            path="main.cpp",
                            method="diagnostics",
                        )
                    )
                    messages = [
                        diagnostic
                        for entry in result["diagnostics"]
                        for diagnostic in entry["diagnostics"]
                    ]
                    assert any(
                        diagnostic.get("code") == "pp_file_not_found"
                        or diagnostic.get("code")
                        == "pp_file_not_found_angled_include_not_fatal"
                        or "file not found" in diagnostic.get("message", "").lower()
                        or "permission denied" in diagnostic.get("message", "").lower()
                        for diagnostic in messages
                    ), (blocked, messages)
                    assert "PRIVATE_CANARY_STUDENT_SYMBOL" not in str(result)
                print("PASS: other student, proc, etc and service-code reads denied")

        source_path = Path(__file__).with_name("sandbox_checks.c")
        if (
            source_path.exists()
            and source_path.parent.parent.joinpath("sandbox.c").exists()
        ):
            with tempfile.TemporaryDirectory(prefix="sandbox-policy-probe-") as temp:
                binary = Path(temp) / "policy-probe"
                subprocess.run(
                    [
                        "gcc",
                        "-O2",
                        "-Wall",
                        "-Wextra",
                        "-Werror",
                        str(source_path),
                        "-o",
                        str(binary),
                    ],
                    check=True,
                )
                private = Path(temp) / "private.hpp"
                private.write_text("must not be readable")
                work = Path(temp) / "workspace"
                work.mkdir()
                subprocess.run(
                    [str(binary), settings.isolation, str(work), str(private)],
                    check=True,
                    timeout=3,
                )
                print(f"PASS: direct {settings.isolation} and seccomp policy probes")
                unsupported = Path(temp) / "unsupported-landlock"
                subprocess.run(
                    [
                        "gcc",
                        "-O2",
                        "-Wall",
                        "-Wextra",
                        "-Werror",
                        str(source_path.with_name("unsupported_landlock.c")),
                        "-o",
                        str(unsupported),
                    ],
                    check=True,
                )
                subprocess.run(
                    [str(unsupported), "--isolation", "container", "--probe"],
                    check=True,
                    timeout=3,
                    stdout=subprocess.DEVNULL,
                )
                rejected = subprocess.run(
                    [str(unsupported), "--isolation", "landlock", "--probe"],
                    check=False,
                    timeout=3,
                    capture_output=True,
                )
                assert rejected.returncode == 125, rejected.returncode
                assert b"Landlock ABI" in rejected.stderr
                print(
                    "PASS: unavailable Landlock permits container; explicit Landlock fails closed"
                )
    finally:
        await service.close()
    assert not list(root.glob("scope-*")), "session source files were not cleaned"
    print("PASS: all clangd sessions and workspace files cleaned")


if __name__ == "__main__":
    asyncio.run(main())
