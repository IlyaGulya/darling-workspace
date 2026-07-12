"""Create and remove isolated disposable Darling prefixes."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from prefix_repair import cleanup_prefix_mounts, repair_prefix_boot_prerequisites
from test_prefix import cleanup_rootless_prefix_processes, cleanup_rootless_runtime_sockets, rootless_prefix_process_snapshot


_PREFIX_NAME = "darling-fresh-prefix-"
_FICLONE = 0x40049409
_REFLINK_UNAVAILABLE = frozenset(
    {errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV}
)


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


def _ignored_prefix_entries(source: Path, directory: Path, names: list[str]) -> set[str]:
    """Return non-runtime entries excluded from a disposable prefix copy."""

    relative = directory.relative_to(source)
    ignored: set[str] = set()
    # Test sandboxes and stale sockets under private/tmp are never part of the
    # boot template; the factory recreates the required tmp roots.
    if relative == Path("private"):
        ignored.add("tmp")
    # Prefix repair can leave historical CLT installations beside the one
    # canonical guest compiler path. Guest fixtures use CommandLineTools, not
    # versioned/incompatible repair backups, which are large and unreachable
    # through the supported test-toolchain contract.
    if relative == Path("Library/Developer"):
        ignored.update(
            name
            for name in names
            if name.startswith("CommandLineTools.")
        )
    for name in names:
        if name in ignored:
            continue
        try:
            mode = (directory / name).lstat().st_mode
        except OSError:
            ignored.add(name)
            continue
        if stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
            ignored.add(name)
    return ignored


def prefix_tree_size(path: Path) -> int:
    """Return bytes in the exact non-transient prefix copy set."""

    total = 0
    pending = [path]
    while pending:
        current = pending.pop()
        entries = list(current.iterdir())
        ignored = _ignored_prefix_entries(path, current, [entry.name for entry in entries])
        for entry in entries:
            if entry.name in ignored:
                continue
            stat = entry.lstat()
            if entry.is_dir() and not entry.is_symlink():
                pending.append(entry)
            elif stat.st_nlink >= 1:
                total += stat.st_size
    return total


def _ignore_prefix_transients(source: Path):
    """Return a copytree callback that excludes live runtime-only entries."""

    def ignore(directory: str, names: list[str]) -> set[str]:
        return _ignored_prefix_entries(source, Path(directory), names)

    return ignore


def _reflink_available(source: Path, directory: Path) -> tuple[bool, str]:
    """Probe CoW cloning on the target filesystem without touching a baseline."""

    if source.stat().st_dev != directory.stat().st_dev:
        return False, "baseline and destination use different filesystems"
    try:
        import fcntl
    except ImportError:
        return False, "platform has no FICLONE support"

    probe = directory / f".west-reflink-probe-{uuid.uuid4().hex}"
    clone = probe.with_name(f"{probe.name}.clone")
    try:
        probe.write_bytes(b"west reflink probe\n")
        source_fd = os.open(probe, os.O_RDONLY)
        try:
            target_fd = os.open(clone, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                fcntl.ioctl(target_fd, _FICLONE, source_fd)
            finally:
                os.close(target_fd)
        finally:
            os.close(source_fd)
    except OSError as error:
        detail = os.strerror(error.errno) if error.errno else str(error)
        return False, detail
    finally:
        for path in (clone, probe):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return True, "FICLONE"


def _reflink_copy(source: str, destination: str) -> str:
    """Clone one regular file while preserving copy2 metadata semantics."""

    import fcntl

    source_fd = os.open(source, os.O_RDONLY)
    try:
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            fcntl.ioctl(destination_fd, _FICLONE, source_fd)
        finally:
            os.close(destination_fd)
    finally:
        os.close(source_fd)
    shutil.copystat(source, destination)
    return destination


def _copy_file(source: str, destination: str, *, reflink: bool) -> str:
    """Copy one file, falling back only when a probed CoW clone is unavailable."""

    if not reflink:
        return shutil.copy2(source, destination)
    try:
        return _reflink_copy(source, destination)
    except OSError as error:
        try:
            Path(destination).unlink()
        except FileNotFoundError:
            pass
        if error.errno not in _REFLINK_UNAVAILABLE:
            raise
        return shutil.copy2(source, destination)


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
    reflink, reflink_detail = _reflink_available(source, target.parent)
    try:
        shutil.copytree(
            source,
            target,
            symlinks=True,
            copy_function=lambda src, dst: _copy_file(src, dst, reflink=reflink),
            ignore=_ignore_prefix_transients(source),
        )
    except (OSError, shutil.Error) as error:
        shutil.rmtree(target, ignore_errors=True)
        return FreshPrefixResult(problems=[f"failed to copy baseline {source}: {error}"])
    strategy = "reflink" if reflink else f"byte-copy (reflink unavailable: {reflink_detail})"
    result = FreshPrefixResult(
        path=target,
        changed=[
            f"copied baseline {source} -> {target} using {strategy}; "
            "excluded transient private/tmp, special files, and stale CommandLineTools.* repairs"
        ],
    )
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
