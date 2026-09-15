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

The store mechanics - markers, atomic publication, bounds and accounting - live
in ``test_store`` because the stock stack cache needs the same ones. The store
is bounded and reports its own accounting, so reuse is observable rather than
assumed.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from test_store import (
    CacheStats,
    canonical_digest,
    entry_bytes,
    entry_reusable as _entry_reusable,
    locked_entry,
    marker_path as _marker_path,
    parse_bound,
    prune_entries,
    read_marker as _read_marker,
    read_stats as _read_stats,
    record_event as _record_event,
    store_root,
    touch_entry,
    write_marker as _write_marker,
    write_stats as _write_stats,
)

SCHEMA = 1
DEFAULT_MAX_BYTES = 12 * 1024**3
MARKER_NAME = ".west-runtime-cache.json"
SOURCE_KIND = "west-runtime-source"
BUILD_KIND = "west-runtime-build"
MATERIALIZER_IDENTITY = "west-runtime-source-forest-v1"


def cache_root(manifest_repo: Path, environ: Mapping[str, str]) -> Path | None:
    """Return the runtime reuse state root, or ``None`` when reuse is disabled."""

    return store_root(
        manifest_repo,
        environ,
        switch="WEST_RUNTIME_BUILD_CACHE",
        directory=".west-test",
        default_name="runtime-build-cache",
    )


def max_bytes(environ: Mapping[str, str]) -> int:
    """Return the configured store bound in bytes."""

    return parse_bound(
        environ, "WEST_RUNTIME_BUILD_CACHE_MAX_BYTES", DEFAULT_MAX_BYTES
    )


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


def prune(
    root: Path,
    max_bytes: int = DEFAULT_MAX_BYTES,
    *,
    protect: Iterable[Path] = (),
) -> dict[str, Any]:
    """Evict least-recently-used entries until the store fits ``max_bytes``."""

    return prune_entries(
        root, ("source", "build"), max_bytes, protect=protect, schema=SCHEMA
    )
