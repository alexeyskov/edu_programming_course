from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


Language = Literal["c", "cpp"]
CompilerFamily = Literal["gcc", "clang"]
BuildMode = Literal["single", "multi"]


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    wall_seconds: float
    cpu_seconds: int
    memory_bytes: int
    process_count: int
    output_bytes: int
    file_bytes: int
    open_files: int = 64


@dataclass(frozen=True, slots=True)
class BuildProfile:
    id: str
    language: Language
    compiler_family: CompilerFamily
    standard: str
    mode: BuildMode
    compile_limits: ResourceLimits
    run_limits: ResourceLimits
    max_file_count: int
    max_source_bytes: int
    max_stdin_bytes: int = 256 * 1024

    @property
    def compiler_name(self) -> str:
        if self.language == "cpp":
            return "g++" if self.compiler_family == "gcc" else "clang++"
        return "gcc" if self.compiler_family == "gcc" else "clang"

    @property
    def source_extensions(self) -> frozenset[str]:
        if self.language == "cpp":
            return frozenset({".cpp", ".cc", ".cxx", ".c++"})
        return frozenset({".c"})

    @property
    def fixed_flags(self) -> tuple[str, ...]:
        common = (
            f"-std={self.standard}",
            "-Wall",
            "-Wextra",
            "-pedantic",
            "-fno-diagnostics-color",
            "-fno-color-diagnostics",
        )
        # GCC does not understand -fno-color-diagnostics; the executor removes
        # the flag that does not belong to the selected compiler.
        if self.compiler_family == "gcc":
            return tuple(
                flag for flag in common if flag != "-fno-color-diagnostics"
            ) + ("-fdiagnostics-format=json",)
        return tuple(flag for flag in common if flag != "-fno-diagnostics-color") + (
            "-fdiagnostics-format=sarif",
            "-Wno-sarif-format-unstable",
        )


SINGLE_COMPILE = ResourceLimits(
    wall_seconds=20,
    cpu_seconds=10,
    memory_bytes=1024 * 1024 * 1024,
    process_count=64,
    output_bytes=1024 * 1024,
    file_bytes=20 * 1024 * 1024,
)
MULTI_COMPILE = ResourceLimits(
    wall_seconds=40,
    cpu_seconds=20,
    memory_bytes=2 * 1024 * 1024 * 1024,
    process_count=96,
    output_bytes=2 * 1024 * 1024,
    file_bytes=50 * 1024 * 1024,
)
DEFAULT_RUN = ResourceLimits(
    wall_seconds=3,
    cpu_seconds=2,
    memory_bytes=256 * 1024 * 1024,
    process_count=32,
    output_bytes=256 * 1024,
    file_bytes=10 * 1024 * 1024,
)


def _profile(
    language: Language,
    compiler: CompilerFamily,
    standard: str,
    mode: BuildMode,
) -> BuildProfile:
    return BuildProfile(
        id=f"{language}-{compiler}-{standard}-{mode}",
        language=language,
        compiler_family=compiler,
        standard=standard,
        mode=mode,
        compile_limits=SINGLE_COMPILE if mode == "single" else MULTI_COMPILE,
        run_limits=DEFAULT_RUN,
        max_file_count=16 if mode == "single" else 64,
        max_source_bytes=2 * 1024 * 1024 if mode == "single" else 8 * 1024 * 1024,
    )


DEFAULT_PROFILES: Mapping[str, BuildProfile] = MappingProxyType(
    {
        profile.id: profile
        for profile in (
            _profile("cpp", "gcc", "c++20", "single"),
            _profile("cpp", "gcc", "c++20", "multi"),
            _profile("cpp", "clang", "c++20", "single"),
            _profile("cpp", "clang", "c++20", "multi"),
            _profile("c", "gcc", "c17", "single"),
            _profile("c", "gcc", "c17", "multi"),
            _profile("c", "clang", "c17", "single"),
            _profile("c", "clang", "c17", "multi"),
        )
    }
)
