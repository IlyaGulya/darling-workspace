"""Safe cleanup for disposable rootless Darling debug prefixes."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from prefix_repair import prefix_mount_targets


_DEBUG_TREE_NAME = re.compile(r"darling-rootless-[A-Za-z0-9_.-]+-debug-[A-Za-z0-9_.-]+$")


@dataclass
class RootlessDebugCleanupResult:
    removed: bool = False
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


def validate_rootless_debug_tree(path: Path, *, temp_root: Path = Path("/tmp")) -> Path:
    """Return a canonical disposable debug path or reject a broad deletion target."""

    root = temp_root.resolve()
    target = path.expanduser().resolve(strict=False)
    if target.parent != root or not _DEBUG_TREE_NAME.fullmatch(target.name):
        raise ValueError(
            f"rootless debug cleanup only accepts {root}/darling-rootless-*-debug-*"
        )
    if target.is_symlink():
        raise ValueError(f"rootless debug cleanup refuses symlink: {target}")
    if not target.is_dir():
        raise ValueError(f"rootless debug tree is not a directory: {target}")
    return target


def rootless_debug_processes(path: Path, *, proc_root: Path = Path("/proc")) -> list[str]:
    """Find live processes explicitly owned by a disposable debug prefix."""

    target = path.resolve()
    marker = b"DARLING_PREFIX="
    entries: list[str] = []
    try:
        process_dirs = proc_root.iterdir()
    except OSError:
        return entries
    for process_dir in process_dirs:
        if not process_dir.name.isdigit():
            continue
        try:
            environment = (process_dir / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        prefix = next((item[len(marker):] for item in environment if item.startswith(marker)), None)
        if prefix is None:
            continue
        try:
            owner = Path(prefix.decode(errors="replace")).resolve()
        except OSError:
            continue
        if owner != target and target not in owner.parents:
            continue
        try:
            argv = [item.decode(errors="replace") for item in (process_dir / "cmdline").read_bytes().split(b"\0") if item]
        except OSError:
            argv = []
        entries.append(f"{process_dir.name} {' '.join(argv) if argv else '[unknown]'}")
    return sorted(entries)


def cleanup_rootless_debug_tree(
    path: Path,
    *,
    allow_sudo: bool = False,
    dry_run: bool = False,
    remover: Callable[[str], None] = shutil.rmtree,
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    mount_targets: Callable[[Path], list[Path]] = prefix_mount_targets,
    processes_for_path: Callable[[Path], list[str]] = rootless_debug_processes,
) -> RootlessDebugCleanupResult:
    """Remove one completed debug prefix, never a live or mounted runtime tree."""

    target = validate_rootless_debug_tree(path)
    result = RootlessDebugCleanupResult()
    mounts = mount_targets(target)
    if mounts:
        result.problems.extend(f"mounted filesystem under debug tree: {mount}" for mount in mounts)
    processes = processes_for_path(target)
    if processes:
        result.problems.extend(f"live rootless debug process: {entry}" for entry in processes)
    if not result.success or dry_run:
        return result
    try:
        remover(str(target))
    except PermissionError as error:
        if not allow_sudo:
            result.problems.append(
                f"cannot remove root-owned debug tree {target}: {error}; retry with --sudo"
            )
            return result
        completed = runner(
            ["sudo", "rm", "-rf", "--one-file-system", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode:
            detail = (completed.stdout + completed.stderr).strip()
            result.problems.append(
                f"sudo cleanup failed for {target} (rc={completed.returncode})"
                + (f": {detail}" if detail else "")
            )
            return result
    if target.exists():
        result.problems.append(f"debug tree remains after cleanup: {target}")
        return result
    result.removed = True
    return result
