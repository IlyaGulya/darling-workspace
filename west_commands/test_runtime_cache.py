"""Safe maintenance of historical workspace runtime build caches.

Normal tests consume immutable product installations and never produce source
forests or runtime deployments. Explicit GC still recognizes historical cache
ownership, producer locks and nested worktree registrations so retiring the old
execution path cannot strand state or delete unrelated data.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from test_store import (
    CacheStats,
    canonical_digest,
    entry_bytes,
    entry_reusable as _entry_reusable,
    inside_state_root,
    locked_entry,
    marker_path as _marker_path,
    parse_bound,
    read_marker as _read_marker,
    read_stats as _read_stats,
    record_event as _record_event,
    store_root,
    touch_entry,
    write_marker as _write_marker,
    write_stats as _write_stats,
)
from test_worktrees import remove_temporary_worktree

SCHEMA = 1
DEFAULT_MAX_BYTES = 12 * 1024**3
MARKER_NAME = ".west-runtime-cache.json"
SOURCE_KIND = "west-runtime-source"
BUILD_KIND = "west-runtime-build"




def marker_path(entry: Path) -> Path:
    return _marker_path(entry, name=MARKER_NAME)


def read_marker(entry: Path, *, kind: str, key: str) -> dict[str, Any] | None:
    """Return a completed entry's marker when it matches ``kind`` and ``key``."""

    return _read_marker(entry, kind=kind, key=key, name=MARKER_NAME, schema=SCHEMA)


def write_marker(entry: Path, *, kind: str, key: str, **fields: Any) -> None:
    """Record that ``entry`` is complete for ``kind`` and ``key``."""

    _write_marker(
        entry, kind=kind, key=key, name=MARKER_NAME, schema=SCHEMA, **fields
    )


def entry_reusable(
    entry: Path,
    *,
    kind: str,
    key: str,
    validate: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Return whether ``entry`` may be reused for ``kind`` and ``key``."""

    return _entry_reusable(
        entry, kind=kind, key=key, name=MARKER_NAME, schema=SCHEMA, validate=validate
    )


def read_stats(root: Path) -> CacheStats:
    return _read_stats(root, schema=SCHEMA)


def write_stats(root: Path, stats: CacheStats) -> None:
    _write_stats(root, stats, schema=SCHEMA)


def record_event(root: Path, event: str, amount: int = 1) -> CacheStats:
    return _record_event(root, event, amount, schema=SCHEMA)


def maintenance_root(
    manifest_repo: Path,
    environ: Mapping[str, str],
    override: str | None = None,
) -> Path:
    """Resolve GC's store even when reuse is disabled; do not create anything."""

    if override is not None:
        return Path(override).expanduser().absolute()
    configured = dict(environ)
    configured.pop("WEST_RUNTIME_BUILD_CACHE", None)
    root = cache_root(manifest_repo, configured)
    assert root is not None
    return root


def _legacy_owned(entry: Path) -> bool:
    """Prove the old build layout's identity receipt belongs to this directory."""

    receipt = entry / "cache-index.json"
    if receipt.is_symlink() or not receipt.is_file():
        return False
    try:
        value = json.loads(receipt.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(value, dict):
        return False
    identity = value.get("identity")
    if (
        value.get("schema_version") != 1
        or value.get("kind") != BUILD_KIND
        or not isinstance(identity, dict)
        or set(identity) != {
            "schema_version", "key", "proof", "prefix", "targets", "configure_args",
        }
        or identity.get("schema_version") != 1
        or not isinstance(identity.get("key"), str)
        or re.fullmatch(r"[0-9a-f]{64}", identity["key"]) is None
        or not isinstance(identity.get("proof"), dict)
        or not isinstance(identity.get("prefix"), str)
        or not Path(identity["prefix"]).is_absolute()
        or not isinstance(identity.get("targets"), list)
        or not identity["targets"]
        or not all(isinstance(target, str) for target in identity["targets"])
        or not isinstance(identity.get("configure_args"), list)
        or not all(isinstance(arg, str) for arg in identity["configure_args"])
        or not isinstance(value.get("artifacts"), list)
        or not value["artifacts"]
        or canonical_digest(identity) != entry.name
    ):
        return False
    for artifact in value["artifacts"]:
        if (
            not isinstance(artifact, dict)
            or set(artifact) != {"path", "bytes", "mode", "sha256"}
            or not isinstance(artifact.get("path"), str)
            or not artifact["path"]
            or Path(artifact["path"]).is_absolute()
            or ".." in Path(artifact["path"]).parts
            or type(artifact.get("bytes")) is not int
            or artifact["bytes"] < 0
            or type(artifact.get("mode")) is not int
            or not isinstance(artifact.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"]) is None
        ):
            return False
    build = entry / "build"
    return build.is_dir() and not build.is_symlink()


def _owned(entry: Path, bucket: str) -> bool:
    if re.fullmatch(r"[0-9a-f]{64}", entry.name) is None:
        return False
    if bucket in ("source", "build"):
        kind = SOURCE_KIND if bucket == "source" else BUILD_KIND
        if read_marker(entry, kind=kind, key=entry.name) is not None:
            return True
    return bucket != "source" and _legacy_owned(entry)


@contextlib.contextmanager
def _idle_entry(entry: Path, *, dry_run: bool) -> Iterator[bool]:
    """Hold the producer's lock, without even creating a lock during planning."""

    lock = entry.parent / f".{entry.name}.lock"
    flags = os.O_CLOEXEC | os.O_NOFOLLOW
    flags |= os.O_RDONLY if dry_run else os.O_RDWR | os.O_CREAT
    try:
        descriptor = os.open(lock, flags, 0o600)
    except FileNotFoundError:
        if not dry_run:
            raise
        yield True
        return
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(f"cache entry lock is not a regular file: {lock}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _entry_worktrees(entry: Path) -> list[tuple[Path, Path]]:
    """Inspect every nested registration before removing any of the forest."""

    markers: list[Path] = []
    repositories: list[Path] = []
    for directory, directories, files in os.walk(entry, followlinks=False):
        if ".git" in directories:
            marker = Path(directory) / ".git"
            if marker.is_symlink():
                raise ValueError(f"symlinked Git metadata: {marker}")
            if (marker / "worktrees").is_dir() and any((marker / "worktrees").iterdir()):
                raise ValueError(f"embedded cache repository has dependent worktrees: {marker}")
            repositories.append(marker.parent)
            directories.remove(".git")
        if ".git" in files:
            markers.append(Path(directory) / ".git")
    worktrees = []
    for marker in sorted(markers, key=lambda path: len(path.parts), reverse=True):
        if marker.is_symlink():
            raise ValueError(f"symlinked Git metadata: {marker}")
        target = marker.parent
        repositories.append(target)
        observed = subprocess.run(
            [
                "git", "-C", str(target), "rev-parse", "--path-format=absolute",
                "--git-common-dir", "--absolute-git-dir",
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        paths = observed.stdout.splitlines()
        if observed.returncode or len(paths) != 2:
            detail = observed.stderr.strip() or observed.stdout.strip()
            raise ValueError(f"cannot inspect cache worktree {target}: {detail}")
        common, gitdir = (Path(path).resolve() for path in paths)
        if gitdir == common:
            # An embedded ordinary repository has no external registration.
            if not common.is_relative_to(entry):
                raise ValueError(f"external non-worktree Git metadata: {marker}")
            continue
        if gitdir.parent.name != "worktrees" or gitdir.parent.parent != common:
            raise ValueError(f"unrecognized cache worktree registration: {marker}")
        backlink = gitdir / "gitdir"
        if Path(backlink.read_text().strip()).resolve() != marker.resolve():
            raise ValueError(f"cache worktree registration points elsewhere: {marker}")
        if (gitdir / "locked").exists() or (gitdir / "index.lock").exists():
            raise ValueError(f"locked Git worktree: {target}")
        repo = common.parent if common.name == ".git" else common
        worktrees.append((repo, target))
    # Hydration/profile application creates independent nested worktrees and
    # commits in them, without updating their parents' gitlink pointers. Exclude
    # only those exact child roots from parent status; each child is checked
    # independently below, so this cannot hide an untracked sibling or an edit.
    for target in repositories:
        children = [
            f":(top,exclude,literal){child.relative_to(target)}"
            for child in repositories
            if child != target and child.is_relative_to(target)
        ]
        status = subprocess.run(
            [
                "git", "-C", str(target), "status", "--porcelain=v1", "-z",
                "--untracked-files=all", "--ignore-submodules=none",
                "--", ".", *children,
            ],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if status.returncode:
            raise ValueError(
                f"cannot inspect cache source changes at {target}: {status.stderr.strip()}"
            )
        if status.stdout:
            raise ValueError(f"tracked or untracked changes in cache source: {target}")
    owner = read_marker(entry, kind=SOURCE_KIND, key=entry.name)
    source = entry / "darling"
    if owner is not None and source.is_dir():
        expected = owner.get("darling")
        head = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
        if not isinstance(expected, str) or head.returncode or head.stdout.strip() != expected:
            raise ValueError(f"cache source HEAD differs from ownership marker: {source}")
    return worktrees


def _dispose_entry(entry: Path, worktrees: list[tuple[Path, Path]]) -> None:
    for repo, target in worktrees:
        error = remove_temporary_worktree(repo, target)
        if error:
            raise RuntimeError(f"could not remove runtime cache worktree: {error}")
    shutil.rmtree(entry)


def _prune(
    root: Path,
    max_bytes: int,
    *,
    protect: Iterable[Path],
    dry_run: bool,
    progress: Callable[[str], None] | None,
    environ: Mapping[str, str] | None,
) -> dict[str, Any]:
    root = Path(root).expanduser().absolute()
    if not inside_state_root(root, environ):
        raise ValueError(f"runtime build cache root {root} is outside DW_STATE_ROOT")
    for component in (root, *root.parents):
        if component.is_symlink():
            raise ValueError(f"runtime build cache root traverses a symlink: {component}")
    protected = {Path(path).resolve() for path in protect}
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []

    def report(message: str) -> None:
        if progress is not None:
            progress(message)

    def skip(entry: Path, reason: str) -> None:
        skipped.append({"path": str(entry), "reason": reason})
        report(f"left alone runtime build cache ({reason}): {entry}")

    entries: list[tuple[float, Path, int, str]] = []
    if root.is_dir():
        for bucket in ("", "source", "build"):
            directory = root / bucket
            if directory.is_symlink():
                skip(directory, "symlink")
                continue
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not bucket and entry.name in ("source", "build"):
                    continue
                if entry.is_symlink():
                    skip(entry, "symlink")
                elif entry.is_dir():
                    if _owned(entry, bucket):
                        entries.append((
                            entry.stat().st_mtime, entry, entry_bytes(entry),
                            str(entry.relative_to(root)),
                        ))
                    else:
                        skip(entry, "unproven ownership")
    total = sum(size for _stamp, _entry, size, _label in entries)
    evicted: list[str] = []
    planned: list[str] = []
    planned_bytes = 0
    evicted_bytes = 0
    for _stamp, entry, size, label in sorted(entries):
        if total <= max_bytes:
            break
        if any(entry.resolve().is_relative_to(path) or path.is_relative_to(entry.resolve())
               for path in protected):
            skip(entry, "protected/in use")
            continue
        try:
            with _idle_entry(entry, dry_run=dry_run) as idle:
                if not idle:
                    skip(entry, "locked/in use")
                    continue
                # Ownership can change while waiting for another cache user.
                bucket = entry.parent.name if entry.parent != root else ""
                if entry.is_symlink() or not entry.is_dir() or not _owned(entry, bucket):
                    skip(entry, "ownership changed")
                    continue
                try:
                    worktrees = _entry_worktrees(entry)
                except ValueError as error:
                    skip(entry, str(error))
                    continue
                observed_size = entry_bytes(entry)
                total += observed_size - size
                size = observed_size
                planned.append(label)
                planned_bytes += size
                if dry_run:
                    report(f"would prune runtime build cache: {entry} ({size} bytes)")
                else:
                    report(f"pruning runtime build cache: {entry} ({size} bytes)")
                    _dispose_entry(entry, worktrees)
                    evicted.append(label)
                    evicted_bytes += size
                    report(f"pruned runtime build cache: {entry} ({size} bytes)")
                total -= size
        except (OSError, RuntimeError, ValueError) as error:
            errors.append({"path": str(entry), "error": str(error)})
            report(f"failed to prune runtime build cache: {entry}: {error}")
    if evicted:
        record_event(root, "entries_evicted", len(evicted))
        record_event(root, "bytes_evicted", evicted_bytes)
    report(
        f"runtime build cache gc: {'would prune' if dry_run else 'pruned'} "
        f"{len(planned) if dry_run else len(evicted)} entry(s), "
        f"{'would free' if dry_run else 'freed'} "
        f"{planned_bytes if dry_run else evicted_bytes} bytes; "
        f"left alone {len(skipped)}, failed {len(errors)}; root={root}"
    )
    return {
        "evicted": evicted,
        "evicted_bytes": evicted_bytes,
        "planned": planned,
        "planned_bytes": planned_bytes,
        "bytes": max(total, 0),
        "max_bytes": max_bytes,
        "skipped": skipped,
        "errors": errors,
    }


def gc(
    root: Path,
    *,
    dry_run: bool = False,
    progress: Callable[[str], None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Explicitly discard all idle, proven-owned reproducible cache entries."""

    return _prune(
        root, 0, protect=(), dry_run=dry_run, progress=progress, environ=environ
    )


def prune(
    root: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    protect: Iterable[Path] = (),
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Bound owned entries by LRU, using the same safe disposal as explicit GC."""

    if max_bytes <= 0:
        raise ValueError("store bound must be positive")
    return _prune(
        root, max_bytes, protect=protect, dry_run=False, progress=None, environ=environ
    )
