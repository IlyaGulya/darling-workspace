#!/usr/bin/env python3
"""Emit the guest-side compile and anchor-runtime script for the batch."""

from __future__ import annotations

import csv
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


MANIFEST_HEADER = (
    "name",
    "guest-source",
    "guest-binary",
    "version",
    "origin",
    "compile-flags",
    "link-flags",
)
ANCHOR_NAME = "select_fdset_guest"


@dataclass(frozen=True)
class Fixture:
    name: str
    source: str
    binary: str
    version: str
    origin: str
    compile_flags: tuple[str, ...]
    link_flags: tuple[str, ...]


def fail(message: str) -> NoReturn:
    print(f"emit-guest-macho-batch-script: {message}", file=sys.stderr)
    raise SystemExit(1)


def parse_json_flags(value: str, field: str, line_number: int) -> tuple[str, ...]:
    import json

    try:
        flags = json.loads(value)
    except ValueError as error:
        fail(f"manifest line {line_number} has invalid {field}: {error}")
    if not isinstance(flags, list) or any(not isinstance(flag, str) for flag in flags):
        fail(f"manifest line {line_number} has non-string {field}")
    return tuple(flags)


def read_manifest(path: Path) -> tuple[Fixture, ...]:
    try:
        rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines(), delimiter="\t"))
    except (OSError, UnicodeError) as error:
        fail(f"cannot read manifest {path}: {error}")
    if not rows or tuple(rows[0]) != MANIFEST_HEADER:
        fail(f"manifest {path} has an invalid header")
    fixtures: list[Fixture] = []
    seen: set[str] = set()
    for line_number, row in enumerate(rows[1:], start=2):
        if len(row) != len(MANIFEST_HEADER) or any(not value for value in row):
            fail(f"manifest line {line_number} is malformed")
        name, source, binary, version, origin, compile_json, link_json = row
        if name in seen:
            fail(f"manifest contains duplicate fixture {name}")
        seen.add(name)
        fixtures.append(
            Fixture(
                name,
                source,
                binary,
                version,
                origin,
                parse_json_flags(compile_json, "compile-flags", line_number),
                parse_json_flags(link_json, "link-flags", line_number),
            )
        )
    if not fixtures:
        fail("manifest has no fixtures")
    if sum(fixture.name == ANCHOR_NAME for fixture in fixtures) != 1:
        fail(f"manifest must contain exactly one {ANCHOR_NAME} anchor")
    return tuple(fixtures)


def shell_join(values: list[str]) -> str:
    return " ".join(shlex.quote(value) for value in values)


def emit(output: Path, compiler: str, fixtures: tuple[Fixture, ...]) -> None:
    anchor = next(fixture for fixture in fixtures if fixture.name == ANCHOR_NAME)
    lines = ["#!/usr/bin/env bash", "set -u", "compile_failed=0", "anchor_compile_rc=1"]
    for fixture in fixtures:
        version = f"{fixture.binary}.clang-version"
        origin = f"{fixture.binary}.clang-origin"
        compile_log = f"{fixture.binary}.compile.log"
        compile_status = f"{fixture.binary}.compile-status.tsv"
        lines.append(
            f"{shell_join([compiler, '--version'])} > {shlex.quote(version)}"
        )
        lines.append(
            "printf \"%s\\n\" "
            f"{shlex.quote('execution-context=guest')} "
            f"{shlex.quote(f'executable={compiler}')} > {shlex.quote(origin)}"
        )
        lines.append(
            f"{shell_join([compiler, *fixture.compile_flags, fixture.source, *fixture.link_flags, '-o', fixture.binary])}"
            f" > {shlex.quote(compile_log)} 2>&1"
        )
        lines.append("compile_rc=$?")
        lines.append("if (( compile_rc == 0 )); then")
        lines.append(
            "  printf \"field\\tvalue\\nfixture\\t%s\\ncompile-status\\tPASS\\ncompile-exit-code\\t0\\n\" "
            f"{shlex.quote(fixture.name)} > {shlex.quote(compile_status)}"
        )
        lines.append("else")
        lines.append("  compile_failed=1")
        lines.append(
            "  printf \"field\\tvalue\\nfixture\\t%s\\ncompile-status\\tFAILED\\ncompile-exit-code\\t%s\\n\" "
            f"{shlex.quote(fixture.name)} \"$compile_rc\" > {shlex.quote(compile_status)}"
        )
        lines.append("fi")
        if fixture.name == ANCHOR_NAME:
            lines.append("anchor_compile_rc=$compile_rc")

    anchor_log = f"{anchor.binary}.runtime.log"
    anchor_status = f"{anchor.binary}.runtime-status.tsv"
    lines.extend(
        [
            "runtime_rc=125",
            "if (( anchor_compile_rc == 0 )); then",
            "  runtime_rc=0",
            f"  {shlex.quote(anchor.binary)} > {shlex.quote(anchor_log)} 2>&1 || runtime_rc=$?",
            "  printf \"field\\tvalue\\nfixture\\tselect_fdset_guest\\nruntime-status\\tEXECUTED\\nruntime-exit-code\\t%s\\n\" \"$runtime_rc\""
            f" > {shlex.quote(anchor_status)}",
            "else",
            "  printf \"field\\tvalue\\nfixture\\tselect_fdset_guest\\nruntime-status\\tNOT_RUN\\nruntime-exit-code\\tNOT_RUN\\n\""
            f" > {shlex.quote(anchor_status)}",
            "fi",
            "if (( compile_failed != 0 )); then exit 1; fi",
            'exit "$runtime_rc"',
            "",
        ]
    )
    output.write_text("\n".join(lines), encoding="utf-8")
    output.chmod(0o755)


def main() -> int:
    if len(sys.argv) != 4:
        fail("usage: emit-guest-macho-batch-script.py OUTPUT COMPILER MANIFEST")
    output, compiler, manifest = map(Path, sys.argv[1:])
    emit(output, str(compiler), read_manifest(manifest))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
