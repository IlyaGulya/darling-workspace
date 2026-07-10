"""CTest backend helpers for ``west test``."""

from __future__ import annotations

import re
from pathlib import Path
from shlex import quote


def ctest_submodule_label_name(submodule: str) -> str:
    """Return the CTest submodule label suffix for a West project path/name."""
    name = Path(submodule).name
    if not name:
        raise ValueError(f"empty submodule selector: {submodule!r}")
    return name


def ctest_label_args(build_dir: Path, label: str) -> list[str]:
    return [
        "ctest",
        "--test-dir",
        str(build_dir),
        "--output-on-failure",
        "-L",
        label,
    ]


def ctest_label_display(build_dir: Path, label: str) -> str:
    return " ".join(quote(str(arg)) for arg in ctest_label_args(build_dir, label))


def ctest_selector_label_args(
    *,
    bead: str | None = None,
    env: str | None = None,
    diag: str | None = None,
    label: str | None = None,
    fuzz: bool = False,
    stress: bool = False,
    changed_submodules: list[str] | None = None,
    submodules: list[str] | None = None,
) -> list[str]:
    args: list[str] = []
    if bead:
        args += ["-L", f"bead:{bead}"]
    if env:
        args += ["-L", f"env:{env}"]
    if diag:
        args += ["-L", f"diag:{diag}"]
    if label:
        args += ["-L", label]
    if fuzz:
        args += ["-L", "fuzz:"]
    if stress:
        args += ["-L", "stress:"]
    submodule_names: list[str] = []
    for selector in [*(changed_submodules or []), *(submodules or [])]:
        name = ctest_submodule_label_name(selector)
        if name not in submodule_names:
            submodule_names.append(name)
    if submodule_names:
        alternation = "|".join(f"submod:{name}" for name in submodule_names)
        args += ["-L", alternation]
    return args


def ctest_command(
    build_dir: Path,
    *,
    label_args: list[str] | None = None,
    list_only: bool = False,
    passthrough: list[str] | None = None,
) -> list[str]:
    args = ["ctest", "--test-dir", str(build_dir), "--output-on-failure"]
    args += list(label_args or [])
    if list_only:
        args.append("--show-only")
    args += list(passthrough or [])
    return args


def ctest_test_name_regex(names: list[str]) -> str:
    """Return an exact CTest regex for already-discovered test names."""

    if not names:
        raise ValueError("CTest runtime group needs at least one test name")
    # CTest's regex implementation accepts ordinary grouping but not Python's
    # non-capturing ``(?:...)`` syntax.
    return "^(" + "|".join(re.escape(name) for name in names) + ")$"


def ctest_runtime_group_passthrough(passthrough: list[str]) -> list[str]:
    """Drop CTest selectors after discovery has frozen an exact test group.

    Repeating ``-R`` is a union in CTest, so retaining a caller's selector
    alongside the group's exact regex can run tests from another runtime
    lifecycle. Labels, fixtures, and test ranges are likewise already reflected
    in JSON discovery. Keep only execution/reporting options for the replay.
    """

    selectors_with_value = {
        "-R",
        "--tests-regex",
        "-E",
        "--exclude-regex",
        "-I",
        "--tests-information",
        "-L",
        "--label-regex",
        "--fixture-exclude-any",
        "--fixture-exclude-setup",
        "--fixture-exclude-cleanup",
        "--fixture-required",
        "--fixture-setup",
        "--fixture-cleanup",
    }
    selectors_without_value = {"--rerun-failed", "--union"}
    result: list[str] = []
    index = 0
    while index < len(passthrough):
        argument = passthrough[index]
        if argument in selectors_without_value:
            index += 1
            continue
        if argument in selectors_with_value:
            if index + 1 >= len(passthrough):
                raise ValueError(f"CTest selector {argument} needs a value")
            index += 2
            continue
        result.append(argument)
        index += 1
    return result


def ctest_selection_command(
    build_dir: Path,
    *,
    label_args: list[str] | None = None,
    passthrough: list[str] | None = None,
) -> list[str]:
    """Return the machine-readable discovery command for a CTest selection.

    CTest owns filtering.  Consumers use this only to inspect properties of the
    exact tests CTest will run; it must never reimplement the selector logic.
    """

    return [
        "ctest",
        "--test-dir",
        str(build_dir),
        "--show-only=json-v1",
        *list(label_args or []),
        *list(passthrough or []),
    ]


def ctest_uses_prefix(*, env: str | None, list_only: bool) -> bool:
    """Whether a CTest selection owns a live Darling prefix lifecycle."""

    return env == "darling" and not list_only
