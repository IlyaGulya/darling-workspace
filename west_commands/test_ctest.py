"""CTest backend helpers for ``west test``."""

from __future__ import annotations

from pathlib import Path
from shlex import quote


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
    changed_submodules: list[str] | None = None,
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
    if changed_submodules:
        alternation = "|".join(f"submod:{name}" for name in changed_submodules)
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
