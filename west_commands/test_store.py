"""Content-addressed store primitives shared by the west test caches.

Both the runtime build cache and the stock stack cache keep entries under a
registered state root, publish them with a completion marker, bound the store by
least-recent use, and report hit and miss accounting. Those mechanics are the
same in both, so they live here once and the caches supply only their own
identity, entry layout and payload handling.

A marker is written only when an entry is complete, so an interrupted capture or
materialization is never consumed; nothing here widens that rule.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1
STATS_NAME = "stats.json"
DISABLED_VALUES = frozenset({"", "0", "off", "no", "false"})


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 of a canonical JSON encoding."""

    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def store_disabled(environ: Mapping[str, str], name: str) -> bool:
    """Return whether ``name`` switches a store off."""

    raw = environ.get(name)
    return raw is not None and raw.strip().lower() in DISABLED_VALUES


# One owned state root per task or lane. When it is declared, everything a run
# produces - job state, debug bundles, proof scratch, runtime evidence - lives
# beneath it, and a maintenance operation refuses to touch anything outside it.
# Undeclared keeps the previous defaults, so existing callers are unchanged.
STATE_ROOT_ENV = "DW_STATE_ROOT"


def state_root(environ: Mapping[str, str] | None = None) -> Path | None:
    """The declared state root for this task or lane, or None when undeclared."""

    source = os.environ if environ is None else environ
    value = str(source.get(STATE_ROOT_ENV, "") or "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


def state_subdir(name: str, environ: Mapping[str, str] | None = None) -> Path | None:
    """A named directory beneath the declared state root, or None when undeclared."""

    root = state_root(environ)
    return None if root is None else root / name


def inside_state_root(path: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Whether ``path`` is inside the declared root; always true when undeclared."""

    root = state_root(environ)
    if root is None:
        return True
    try:
        Path(path).expanduser().resolve().relative_to(root)
    except ValueError:
        return False
    return True


def store_root(
    manifest_repo: Path,
    environ: Mapping[str, str],
    *,
    switch: str,
    directory: str,
    default_name: str,
) -> Path | None:
    """Return a store root under the manifest, or ``None`` when switched off.

    ``<switch>_DIR`` names the store explicitly, which is how a CI tier pins it;
    otherwise it lives under the manifest repository so that it is a registered
    state root rather than an unowned temporary tree.
    """

    if store_disabled(environ, switch):
        return None
    configured = environ.get(f"{switch}_DIR")
    root = Path(configured).expanduser() if configured else manifest_repo / directory / default_name
    if not root.is_absolute():
        raise ValueError(f"{switch} root must be absolute")
    return root


def parse_bound(environ: Mapping[str, str], name: str, default: int) -> int:
    """Return a configured store bound in bytes."""

    raw = environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{name} is not an integer: {raw}") from error
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def write_json(path: Path, value: Any) -> None:
    """Write JSON atomically: temporary sibling, fsync, rename."""

    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def sync_directory(path: Path) -> None:
    """Flush a directory entry so a published rename survives a crash."""

    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class CacheStats:
    """Accounting for one store."""

    counters: dict[str, int] = field(default_factory=dict)

    def record(self, event: str, amount: int = 1) -> None:
        if amount < 0:
            raise ValueError("cache accounting increments must not be negative")
        self.counters[event] = self.counters.get(event, 0) + amount

    def value(self, event: str) -> int:
        return self.counters.get(event, 0)

    def rate(self, hits: str, misses: str) -> float:
        attempts = self.value(hits) + self.value(misses)
        if not attempts:
            return 100.0
        return round(100 * self.value(hits) / attempts, 1)


def read_stats(root: Path, *, schema: int = SCHEMA) -> CacheStats:
    """Return the accounting recorded so far, tolerating an absent store."""

    marker = root / STATS_NAME
    if not marker.is_file() or marker.is_symlink():
        return CacheStats()
    try:
        value = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return CacheStats()
    counters = value.get("counters") if isinstance(value, dict) else None
    if not isinstance(counters, dict):
        return CacheStats()
    stats = CacheStats()
    for name, amount in counters.items():
        if isinstance(name, str) and isinstance(amount, int) and amount >= 0:
            stats.counters[name] = amount
    return stats


def write_stats(root: Path, stats: CacheStats, *, schema: int = SCHEMA) -> None:
    write_json(root / STATS_NAME, {"schema": schema, "counters": stats.counters})


def record_event(
    root: Path, event: str, amount: int = 1, *, schema: int = SCHEMA
) -> CacheStats:
    """Increment one counter under an exclusive lock and return the totals."""

    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with locked_root(root):
        stats = read_stats(root, schema=schema)
        stats.record(event, amount)
        write_stats(root, stats, schema=schema)
    return stats


# Ownership for disposable scratch directories. GC may only collect a
# directory that carries this marker: the identity of the creator and the task
# it belongs to, written before anything else is placed inside, so a pass that
# finds a name-matching directory without one reports it instead of deleting it.
SCRATCH_KIND = "west-test-scratch"
SCRATCH_MARKER_NAME = ".west-test-scratch.json"


def owned_scratch_dir(prefix: str, *, key: str, **fields: Any) -> Path:
    """Create a marked scratch directory that GC is allowed to collect."""

    entry = Path(tempfile.mkdtemp(prefix=prefix))
    write_marker(
        entry, kind=SCRATCH_KIND, key=key, name=SCRATCH_MARKER_NAME, **fields
    )
    return entry


def scratch_owner(entry: Path) -> dict[str, Any] | None:
    """Return who owns a marked scratch directory, or None when unproven."""

    marker = marker_path(entry, name=SCRATCH_MARKER_NAME)
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        value = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("kind") != SCRATCH_KIND:
        return None
    if value.get("schema") != SCHEMA:
        return None
    return value


def marker_path(entry: Path, *, name: str) -> Path:
    return entry / name


def read_marker(
    entry: Path, *, kind: str, key: str, name: str, schema: int = SCHEMA
) -> dict[str, Any] | None:
    """Return a completed entry's marker when it matches ``kind`` and ``key``."""

    marker = marker_path(entry, name=name)
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        value = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema") != schema or value.get("kind") != kind:
        return None
    if value.get("key") != key:
        return None
    return value


def write_marker(
    entry: Path,
    *,
    kind: str,
    key: str,
    name: str,
    schema: int = SCHEMA,
    **fields: Any,
) -> None:
    """Record that ``entry`` is complete for ``kind`` and ``key``."""

    entry.mkdir(parents=True, mode=0o700, exist_ok=True)
    write_json(
        marker_path(entry, name=name),
        {"schema": schema, "kind": kind, "key": key, **fields},
    )


def entry_reusable(
    entry: Path,
    *,
    kind: str,
    key: str,
    name: str,
    schema: int = SCHEMA,
    validate: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Return whether ``entry`` may be reused for ``kind`` and ``key``."""

    if entry.is_symlink() or not entry.is_dir():
        return False
    marker = read_marker(entry, kind=kind, key=key, name=name, schema=schema)
    if marker is None:
        return False
    if validate is not None and not validate(marker):
        return False
    return True


def touch_entry(entry: Path) -> None:
    """Mark one entry as most recently used, so bounds evict cold entries first."""

    with contextlib.suppress(OSError):
        os.utime(entry, None)


@contextlib.contextmanager
def locked_entry(entry: Path) -> Iterator[None]:
    """Serialize work on one entry across concurrent runs."""

    entry.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with locked_root(entry.parent, name=f".{entry.name}.lock"):
        yield


@contextlib.contextmanager
def locked_root(root: Path, name: str = ".store.lock") -> Iterator[None]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(root / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def entry_bytes(entry: Path) -> int:
    """Return the on-disk size of one entry."""

    total = 0
    for path in entry.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


def prune_entries(
    root: Path,
    directories: Sequence[str],
    max_bytes: int,
    *,
    protect: Iterable[Path] = (),
    schema: int = SCHEMA,
) -> dict[str, Any]:
    """Evict least-recently-used entries until the store fits ``max_bytes``.

    ``directories`` names the entry buckets under ``root``; an empty name scans
    ``root`` itself. Entries named by ``protect`` are never evicted, so a caller
    can bound the store while the entry it is about to use stays available.
    """

    if max_bytes <= 0:
        raise ValueError("store bound must be positive")
    protected = {path.resolve() for path in protect}
    entries: list[tuple[float, Path, int, bool, str]] = []
    for name in directories:
        directory = root / name if name else root
        if not directory.is_dir() or directory.is_symlink():
            continue
        for entry in sorted(directory.iterdir()):
            if entry.is_symlink() or not entry.is_dir():
                continue
            try:
                stamp = entry.stat().st_mtime
            except OSError:
                stamp = 0.0
            entries.append(
                (
                    stamp,
                    entry,
                    entry_bytes(entry),
                    entry.resolve() in protected,
                    name or root.name,
                )
            )
    total = sum(size for _stamp, _entry, size, _kept, _label in entries)
    evicted: list[str] = []
    evicted_bytes = 0
    for _stamp, entry, size, kept, label in sorted(entries):
        if total <= max_bytes or kept:
            continue
        with contextlib.suppress(OSError):
            shutil.rmtree(entry)
        total -= size
        evicted.append(f"{label}/{entry.name}")
        evicted_bytes += size
    if evicted:
        record_event(root, "entries_evicted", len(evicted), schema=schema)
        record_event(root, "bytes_evicted", evicted_bytes, schema=schema)
    return {
        "evicted": evicted,
        "evicted_bytes": evicted_bytes,
        "bytes": max(total, 0),
        "max_bytes": max_bytes,
    }
