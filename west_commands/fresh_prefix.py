"""Create and remove isolated disposable Darling prefixes."""

from __future__ import annotations

import errno
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from prefix_repair import cleanup_prefix_mounts
from test_prefix import cleanup_verified_prefix_processes, cleanup_rootless_runtime_sockets, prefix_process_ownership_snapshot

try:
    from .runtime_install import InstalledRuntime, RuntimeInstallationError, prefix_path_is_safe
    from .runtime_retention import RuntimeRetentionError, leased_runtime
except ImportError:  # Loaded as a West extension module, not a package.
    from runtime_install import InstalledRuntime, RuntimeInstallationError, prefix_path_is_safe
    from runtime_retention import RuntimeRetentionError, leased_runtime


_PREFIX_NAME = "darling-fresh-prefix-"
_FICLONE = 0x40049409
_REFLINK_UNAVAILABLE = frozenset(
    {errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTTY, errno.EXDEV}
)


class PrefixCloneError(RuntimeError):
    """The selected product launcher refused to bind a copied guest prefix."""


def bind_prefix_clone(
    runtime: InstalledRuntime,
    destination: str | Path,
    base: str | Path,
    *,
    runner=subprocess.run,
) -> None:
    """Bind a copied guest state tree through the product's checked clone operation.

    The launcher owns the prefix lifecycle record: it validates the stable BASE
    record, publishes the clone's own identity and generation, and refuses an
    unusable base. Workspace Python never parses or rewrites that record.
    """

    runtime.environment(base)
    environment = runtime.environment(destination)
    completed = runner(
        [str(runtime.launcher), "bind-clone", str(base)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        metadata_details = []
        for label, path in (("base", base), ("clone", destination)):
            try:
                metadata = Path(path).lstat()
                metadata_details.append(
                    f"{label}: dev={metadata.st_dev} inode={metadata.st_ino} "
                    f"uid={metadata.st_uid} gid={metadata.st_gid} "
                    f"mode={stat.S_IMODE(metadata.st_mode):04o}"
                )
            except OSError as error:
                metadata_details.append(f"{label}: metadata unavailable ({error})")
        raise PrefixCloneError(
            f"{runtime.launcher} bind-clone {base} -> {destination} failed "
            f"(rc={completed.returncode}): {detail or 'no diagnostic output'}; "
            f"post-failure root metadata: {'; '.join(metadata_details)}"
        )


@dataclass
class FreshPrefixResult:
    path: Path | None = None
    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return not self.problems


def disposable_prefix_path(path: str | Path, *, temp_root: Path) -> Path:
    """Validate a narrowly-scoped disposable prefix path."""

    raw_root = os.path.expanduser(os.fspath(temp_root))
    raw_candidate = os.path.expanduser(os.fspath(path))
    if not prefix_path_is_safe(raw_root) or not prefix_path_is_safe(raw_candidate):
        raise ValueError(f"unsafe fresh-prefix root or path: {raw_root}, {raw_candidate}")
    root = Path(raw_root)
    candidate = Path(raw_candidate)
    if candidate.parent != root or not candidate.name.startswith(_PREFIX_NAME):
        raise ValueError(f"fresh prefix must be directly under {root}/{_PREFIX_NAME}*")
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
    baseline: str | Path,
    *,
    destination: str | Path | None = None,
    temp_root: Path,
    reserve_bytes: int = 8 * 1024**3,
    runtime: InstalledRuntime | None = None,
    runtime_root: str | Path | None = None,
) -> FreshPrefixResult:
    """Copy writable guest state, then bind it through the selected product launcher.

    The runtime image is immutable and separate; copying a guest-state prefix never
    copies or repairs a launcher. A copied prefix is a different prefix, and only the
    product's checked ``bind-clone`` operation may publish its lifecycle identity.
    """

    raw_source = os.path.expanduser(os.fspath(baseline))
    try:
        with leased_runtime(runtime_root, runtime=runtime) as selected:
            selected.environment(raw_source)
            return _copy_fresh_prefix(
                raw_source, destination=destination, temp_root=temp_root,
                reserve_bytes=reserve_bytes, selected=selected,
            )
    except (OSError, RuntimeInstallationError, RuntimeRetentionError) as error:
        return FreshPrefixResult(problems=[str(error)])


def _copy_fresh_prefix(
    raw_source: str, *, destination, temp_root: Path,
    reserve_bytes: int, selected: InstalledRuntime,
) -> FreshPrefixResult:
    source = Path(raw_source)
    if not source.is_dir():
        return FreshPrefixResult(problems=[f"baseline is not a real directory: {source}"])
    target = disposable_prefix_path(
        destination or temp_root / f"{_PREFIX_NAME}{uuid.uuid4().hex[:12]}",
        temp_root=temp_root,
    )
    try:
        selected.environment(target)
    except RuntimeInstallationError as error:
        return FreshPrefixResult(problems=[str(error)])
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
    try:
        bind_prefix_clone(selected, target, source)
    except (OSError, subprocess.SubprocessError, PrefixCloneError, RuntimeInstallationError) as error:
        shutil.rmtree(target, ignore_errors=True)
        return FreshPrefixResult(
            problems=[f"cannot bind the copied guest prefix through {selected.launcher}: {error}"]
        )
    strategy = "reflink" if reflink else f"byte-copy (reflink unavailable: {reflink_detail})"
    return FreshPrefixResult(
        path=target,
        changed=[
            f"copied guest state {source} -> {target} using {strategy}; "
            "excluded transient private/tmp, special files, and stale CommandLineTools.* repairs",
            f"bound the copy through {selected.launcher} bind-clone {source}",
        ],
    )


def remove_fresh_prefix(path: Path, *, temp_root: Path) -> FreshPrefixResult:
    """Remove a completed disposable prefix after lifecycle cleanup proves ownership."""

    target = disposable_prefix_path(path, temp_root=temp_root)
    if not target.is_dir():
        return FreshPrefixResult(problems=[f"fresh prefix is not a directory: {target}"])
    result = FreshPrefixResult(path=target)
    process_cleanup = cleanup_verified_prefix_processes(target)
    result.changed.extend(process_cleanup.changed)
    result.problems.extend(process_cleanup.problems)
    mount_cleanup = cleanup_prefix_mounts(target)
    result.changed.extend(mount_cleanup.changed)
    result.problems.extend(mount_cleanup.problems)
    if prefix_process_ownership_snapshot(target):
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
