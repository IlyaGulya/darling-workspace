"""CTest backend helpers for ``west test``."""

from __future__ import annotations

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
