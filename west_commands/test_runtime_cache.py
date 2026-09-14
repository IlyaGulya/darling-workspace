"""Identity-keyed reuse for runtime source forests and build trees.

Runtime profiles compile the same trees on every run, and ccache cannot make
that cheap on its own. ccache normalises absolute paths against the current
working directory, so a source forest materialised under a per-run temporary
root keeps hashing differently: measured on this host, compiling one unchanged
file from two run roots produced two misses and a CCACHE_DEBUG diff whose only
difference was ``../run1/src/foo.c`` against ``../run2/src/foo.c``. Neither
``CCACHE_BASEDIR`` nor ``-fdebug-prefix-map`` removes a run-specific component
that sits inside the path.

Reuse therefore has to key on content identity instead of on paths:

* the source forest is materialised under ``<root>/source/<source-key>`` and
  reused only when the recorded identity still matches, so the compiler sees
  the same paths on a repeat run;
* the build tree lives under ``<root>/build/<build-key>`` and is reused only
  when the same identity produced it, so an incremental build is a no-op
  instead of a rebuild.

The store is bounded and reports its own accounting, so reuse is observable
rather than assumed.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = 1
DEFAULT_MAX_BYTES = 12 * 1024**3
MARKER_NAME = ".west-runtime-cache.json"
DISABLED_VALUES = frozenset({"", "0", "off", "no", "false"})
SOURCE_KIND = "west-runtime-source"
BUILD_KIND = "west-runtime-build"
MATERIALIZER_IDENTITY = "west-runtime-source-forest-v1"


def canonical_digest(value: Any) -> str:
    """Return the SHA-256 of a canonical JSON encoding."""

    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def cache_root(manifest_repo: Path, environ: Mapping[str, str]) -> Path | None:
    """Return the runtime reuse state root, or ``None`` when reuse is disabled.

    ``WEST_RUNTIME_BUILD_CACHE_DIR`` names the store explicitly, which is how a
    CI tier pins it. Otherwise the store lives under the manifest repository so
    that it is a registered state root rather than an unowned ``/tmp`` tree.
    """

    raw = environ.get("WEST_RUNTIME_BUILD_CACHE")
    if raw is not None and raw.strip().lower() in DISABLED_VALUES:
        return None
    configured = environ.get("WEST_RUNTIME_BUILD_CACHE_DIR")
    if configured:
        root = Path(configured).expanduser()
    else:
        root = manifest_repo / ".west-test" / "runtime-build-cache"
    if not root.is_absolute():
        raise ValueError("runtime build cache root must be absolute")
    return root


def max_bytes(environ: Mapping[str, str]) -> int:
    """Return the configured store bound in bytes."""

    raw = environ.get("WEST_RUNTIME_BUILD_CACHE_MAX_BYTES")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_BYTES
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(
            f"runtime build cache bound is not an integer: {raw}"
        ) from error
    if value <= 0:
        raise ValueError("runtime build cache bound must be positive")
    return value


def source_identity(
    identity: Mapping[str, Any],
    *,
    omit_patch: bool,
    patch_path: str,
    bad_profile: str | None,
    bad_revision: str | None = None,
) -> dict[str, Any]:
    """Reduce a retained-runtime identity to the inputs that fix source content.

    ``runtime_identity`` also covers the deployed launcher, which changes with
    every prefix and must not influence source reuse. A RED arm builds from a
    revision that the profile identity does not name, so that revision is part
    of the source identity whenever it applies, and the materializer version is
    included so a change to how forests are built invalidates older entries.
    """

    reduced = {
        key: value for key, value in identity.items() if key != "launcher-sha256"
    }
    reduced["omit-patch"] = bool(omit_patch)
    reduced["patch-path"] = str(patch_path)
    reduced["bad-profile"] = bad_profile
    reduced["bad-revision"] = bad_revision if omit_patch else None
    reduced["materializer"] = MATERIALIZER_IDENTITY
    return reduced


def source_key(identity: Mapping[str, Any]) -> str:
    """Return the cache key for one materialised source forest."""

    return canonical_digest({"schema": SCHEMA, "kind": SOURCE_KIND, **identity})


def build_key(source: str, identity: Mapping[str, Any]) -> str:
    """Return the cache key for one build tree produced from ``source``."""

    return canonical_digest(
        {"schema": SCHEMA, "kind": BUILD_KIND, "source": source, **identity}
    )


def source_entry(root: Path, key: str) -> Path:
    return root / "source" / key


def build_entry(root: Path, key: str) -> Path:
    return root / "build" / key


def marker_path(entry: Path) -> Path:
    return entry / MARKER_NAME


@dataclass
class CacheStats:
    """Accounting for one reuse store."""

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


def read_stats(root: Path) -> CacheStats:
    """Return the accounting recorded so far, tolerating an absent store."""

    marker = root / "stats.json"
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


def write_stats(root: Path, stats: CacheStats) -> None:
    _write_json(root / "stats.json", {"schema": SCHEMA, "counters": stats.counters})


def record_event(root: Path, event: str, amount: int = 1) -> CacheStats:
    """Increment one counter under an exclusive lock and return the totals."""

    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _root_lock(root):
        stats = read_stats(root)
        stats.record(event, amount)
        write_stats(root, stats)
    return stats


@dataclass(frozen=True)
class RuntimeReusePlan:
    """The identity-keyed locations and accounting for one runtime profile."""

    store: Path
    source_key: str
    build_key: str

    @property
    def source_entry(self) -> Path:
        return source_entry(self.store, self.source_key)

    @property
    def build_cache(self) -> tuple[Path, str]:
        return (self.store, self.build_key)

    def record_build(self, reused: bool) -> None:
        record_event(self.store, "build_hits" if reused else "build_misses")

    def report(self) -> str:
        stats = read_stats(self.store)
        return (
            f"reuse source={stats.value('source_hits')}hit/"
            f"{stats.value('source_misses')}miss "
            f"build={stats.value('build_hits')}hit/"
            f"{stats.value('build_misses')}miss store={self.store}"
        )


def read_marker(entry: Path, *, kind: str, key: str) -> dict[str, Any] | None:
    """Return a completed entry's marker when it matches ``kind`` and ``key``."""

    marker = marker_path(entry)
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        value = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    if value.get("schema") != SCHEMA or value.get("kind") != kind:
        return None
    if value.get("key") != key:
        return None
    return value


def write_marker(entry: Path, *, kind: str, key: str, **fields: Any) -> None:
    """Record that ``entry`` is complete for ``kind`` and ``key``."""

    entry.mkdir(parents=True, mode=0o700, exist_ok=True)
    _write_json(
        marker_path(entry), {"schema": SCHEMA, "kind": kind, "key": key, **fields}
    )


def entry_reusable(
    entry: Path,
    *,
    kind: str,
    key: str,
    validate: Callable[[dict[str, Any]], bool] | None = None,
) -> bool:
    """Return whether ``entry`` may be reused for ``kind`` and ``key``."""

    if entry.is_symlink() or not entry.is_dir():
        return False
    marker = read_marker(entry, kind=kind, key=key)
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
    """Serialize materialisation of one entry across concurrent runs."""

    entry.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with _root_lock(entry.parent, name=f".{entry.name}.lock"):
        yield


@contextlib.contextmanager
def _root_lock(root: Path, name: str = ".store.lock") -> Iterator[None]:
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor = os.open(root / name, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def entry_bytes(entry: Path) -> int:
    """Return the on-disk size of one store entry."""

    total = 0
    for path in entry.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            total += path.stat().st_size
        except OSError:
            continue
    return total


def prune(
    root: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    protect: Iterable[Path] = (),
) -> dict[str, Any]:
    """Evict least-recently-used entries until the store fits ``max_bytes``.

    Entries named by ``protect`` are never evicted, so a caller can bound the
    store while the entry it is about to use stays available.
    """

    if max_bytes <= 0:
        raise ValueError("runtime build cache bound must be positive")
    protected = {path.resolve() for path in protect}
    entries: list[tuple[float, Path, int, bool]] = []
    for kind in (SOURCE_KIND, BUILD_KIND):
        directory = root / ("source" if kind == SOURCE_KIND else "build")
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
                (stamp, entry, entry_bytes(entry), entry.resolve() in protected)
            )
    total = sum(size for _stamp, _entry, size, _kept in entries)
    evicted: list[str] = []
    evicted_bytes = 0
    for _stamp, entry, size, kept in sorted(entries):
        if total <= max_bytes or kept:
            continue
        with contextlib.suppress(OSError):
            shutil.rmtree(entry)
        total -= size
        evicted.append(f"{entry.parent.name}/{entry.name}")
        evicted_bytes += size
    if evicted:
        record_event(root, "entries_evicted", len(evicted))
        record_event(root, "bytes_evicted", evicted_bytes)
    return {
        "evicted": evicted,
        "evicted_bytes": evicted_bytes,
        "bytes": max(total, 0),
        "max_bytes": max_bytes,
    }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
