"""Create and remove isolated disposable Darling prefixes."""

from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from prefix_repair import cleanup_prefix_mounts, repair_prefix_boot_prerequisites
from test_prefix import cleanup_rootless_prefix_processes, cleanup_rootless_runtime_sockets, rootless_prefix_process_snapshot


_PREFIX_NAME = "darling-fresh-prefix-"


@dataclass
class FreshPrefixResult:
    path: Path | None = None
    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


def disposable_prefix_path(path: Path, *, temp_root: Path = Path("/tmp")) -> Path:
    """Validate a narrowly-scoped disposable prefix path."""

    root = temp_root.resolve()
    candidate = path.expanduser().resolve(strict=False)
    if candidate.parent != root or not candidate.name.startswith(_PREFIX_NAME):
        raise ValueError(f"fresh prefix must be directly under {root}/{_PREFIX_NAME}*")
    if candidate.is_symlink():
        raise ValueError(f"fresh prefix cannot be a symlink: {candidate}")
    return candidate


def prefix_tree_size(path: Path) -> int:
    """Return allocated file bytes without following directory symlinks."""

    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        for entry in current.iterdir():
            stat = entry.lstat()
            if entry.is_dir() and not entry.is_symlink():
                pending.append(entry)
            elif stat.st_nlink >= 1:
                total += stat.st_size
    return total


def create_fresh_prefix(
    baseline: Path,
    *,
    destination: Path | None = None,
    temp_root: Path = Path("/tmp"),
    reserve_bytes: int = 8 * 1024**3,
) -> FreshPrefixResult:
    """Copy a baseline prefix into a disposable path after an honest space check."""

    source = baseline.expanduser().resolve()
    if not source.is_dir() or source.is_symlink():
        return FreshPrefixResult(problems=[f"baseline is not a real directory: {source}"])
    target = disposable_prefix_path(
        destination or temp_root / f"{_PREFIX_NAME}{uuid.uuid4().hex[:12]}",
        temp_root=temp_root,
    )
    if target.exists():
        return FreshPrefixResult(problems=[f"fresh prefix already exists: {target}"])
    required = prefix_tree_size(source) + reserve_bytes
    available = shutil.disk_usage(target.parent).free
    if available < required:
        return FreshPrefixResult(
            problems=[
                f"fresh prefix needs {required} free bytes under {target.parent}, "
                f"but only {available} are available"
            ]
        )
    shutil.copytree(source, target, symlinks=True, copy_function=shutil.copy2)
    result = FreshPrefixResult(path=target, changed=[f"copied baseline {source} -> {target}"])
    result_provision = repair_prefix_boot_prerequisites(target)
    result.changed.extend(result_provision.changed)
    result.problems.extend(result_provision.problems)
    return result


def remove_fresh_prefix(path: Path, *, temp_root: Path = Path("/tmp")) -> FreshPrefixResult:
    """Remove a completed disposable prefix after lifecycle cleanup proves ownership."""

    target = disposable_prefix_path(path, temp_root=temp_root)
    if not target.is_dir():
        return FreshPrefixResult(problems=[f"fresh prefix is not a directory: {target}"])
    result = FreshPrefixResult(path=target)
    process_cleanup = cleanup_rootless_prefix_processes(target)
    result.changed.extend(process_cleanup.changed)
    result.problems.extend(process_cleanup.problems)
    mount_cleanup = cleanup_prefix_mounts(target)
    result.changed.extend(mount_cleanup.changed)
    result.problems.extend(mount_cleanup.problems)
    if rootless_prefix_process_snapshot(target):
        result.problems.append(f"rootless process remains for fresh prefix: {target}")
    if result.problems:
        return result
    socket_cleanup = cleanup_rootless_runtime_sockets(target)
    result.changed.extend(socket_cleanup.changed)
    result.problems.extend(socket_cleanup.problems)
    if result.problems:
        return result
    shutil.rmtree(target)
    result.changed.append(f"removed fresh prefix {target}")
    return result
