"""Identity-keyed reuse of metadata test verdicts.

The acceptance tests are re-run from scratch whenever they are selected, even
when nothing that decides their verdict changed: the same test asset bytes, the
same declaration, the same runtime identity and the same guest stack. A verdict
is a function of an enumerable identity, so it can be reused the way the source
forest, the build tree and the stock stack already are - provided the identity is
conservative and a reused verdict is never presented as a fresh execution.

The identity is derived from what already exists instead of inventing a second
notion of freshness: the test asset bytes as the framework resolves them, a
normalised digest of the test declaration, the runner identity including args,
timeout and expected markers, the runtime identity (profile definition, patchset
digests and source revisions, the same inputs ``runtime_identity`` feeds the
runtime reuse cache), the stock stack request when the test consumes that
resource, the workload parameters that decide how much work the test does, the
guest toolchain identity and the host architecture.

Rules this module enforces, because a cache that only claims them is worthless:

* only a zero verdict is published - a failure is an observation about one run,
  not a property of the identity, so it is never written;
* a recorded non-zero verdict (a foreign or tampered entry) is never reused;
* an entry without a completion marker, or with a marker that does not describe
  its own identity, is a miss;
* reuse is announced with the identity digest and the original run time;
* the store is bounded by least-recent use and reports hits, misses, unkeyed
  runs and evictions;
* ``WEST_TEST_VERDICT_CACHE=off`` is the kill switch: a test whose flake history
  makes a cached pass worthless has to be executable for real, and a run with
  the switch on says so.

A cached verdict cannot detect flake. It also cannot describe state that no
identity input covers, which is why a caller must pass ``None`` from
``verdict_identity`` - never a partial identity - whenever an input it cannot
resolve. The store mechanics (markers, atomic publication, bounds, accounting)
live in ``test_store``; this module supplies only the identity, the entry layout
and the reuse decision.
"""

from __future__ import annotations

import hashlib
import platform
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from test_store import (
    CacheStats,
    canonical_digest,
    entry_reusable,
    marker_path as _marker_path,
    parse_bound,
    prune_entries,
    read_marker as _read_marker,
    read_stats as _read_stats,
    record_event as _record_event,
    store_disabled,
    store_root,
    touch_entry,
    write_marker as _write_marker,
)

SCHEMA = 1
KIND = "west-test-verdict"
MARKER_NAME = ".west-test-verdict.json"
ENTRY_DIRECTORY = "verdict"
STORE_SWITCH = "WEST_TEST_VERDICT_CACHE"
STORE_BOUND = "WEST_TEST_VERDICT_CACHE_MAX_BYTES"
# Verdict entries are one small JSON marker each, so the bound is about keeping
# the store from growing without limit rather than about disk budget.
DEFAULT_MAX_BYTES = 256 * 1024**2
# The resource whose guest stack state a verdict consumes. A test that declares
# it is keyed on the stock stack identity as well.
STACK_RESOURCE = "homebrew-lz4"
# Environment variables that change how much work a test does, or whether it
# executes at all. They are read from the effective environment rather than from
# the declaration, because an operator can shorten or split a run without
# editing metadata - and a shortened run must never reuse the acceptance verdict.
WORKLOAD_VARIABLES = (
    "WEST_STOCK_WGET_ITERATIONS",
    "WEST_STOCK_REPLAY_PHASE",
    "WEST_GUEST_C_FIXTURE_PREPARE_ONLY",
    "WEST_GUEST_C_FIXTURE_RUN_ONLY",
)
# Invocation keys that name a file the framework already resolved on the host,
# plus the declared name that stands in for it in the identity. The declared
# name keeps the identity content-addressed: the same asset under another run
# root is the same asset.
ASSET_KEYS = ("script_path", "source_file")


def cache_root(manifest_repo: Path, environ: Mapping[str, str]) -> Path | None:
    """Return the verdict store root, or ``None`` when reuse is switched off."""

    return store_root(
        manifest_repo,
        environ,
        switch=STORE_SWITCH,
        directory=".west-test",
        default_name="test-verdict-cache",
    )


def reuse_disabled(environ: Mapping[str, str]) -> bool:
    """Return whether the environment forces every test to execute for real."""

    return store_disabled(environ, STORE_SWITCH)


def max_bytes(environ: Mapping[str, str]) -> int:
    """Return the configured store bound in bytes."""

    return parse_bound(environ, STORE_BOUND, DEFAULT_MAX_BYTES)


def digest_file(path: Path) -> str | None:
    """Return the SHA-256 of one file, or ``None`` when it cannot be read."""

    try:
        if path.is_symlink() or not path.is_file():
            return None
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _jsonable(value: Any) -> Any:
    """Return ``value`` as JSON-compatible data, preserving structure."""

    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(str(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return str(value)


def declaration_digest(test: Mapping[str, Any]) -> str:
    """Return the normalised digest of one test declaration.

    ``_``-prefixed keys are resolved per run rather than declared - the CTest
    selection carries a build directory - so they are excluded; everything the
    metadata declares, including the expected markers and the environment the
    test asks for, is part of the digest.
    """

    declared = {
        str(key): value for key, value in test.items() if not str(key).startswith("_")
    }
    return canonical_digest(_jsonable(declared))


def expected_markers(invocation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the markers the invocation must observe to pass."""

    markers = {}
    for name in ("ok_marker", "expect", "cases", "symbol_checks", "host_trace_oracle"):
        value = invocation.get(name)
        if value in (None, "", [], {}):
            continue
        markers[name] = _jsonable(value)
    return markers


def runner_identity(invocation: Mapping[str, Any]) -> dict[str, Any]:
    """Return the args, timeout and markers that decide what one run executes."""

    return {
        "key": str(invocation.get("key") or ""),
        "runner": str(
            invocation.get("runner") or ("shell" if invocation.get("shell") else "script")
        ),
        "args": [str(arg) for arg in invocation.get("args") or ()],
        "timeout-seconds": int(invocation.get("timeout_seconds") or 0),
        "diag": str(invocation.get("diag") or ""),
        "requires-resources": sorted(
            str(name) for name in invocation.get("requires_resources") or ()
        ),
        "markers": expected_markers(invocation),
    }


def asset_records(invocation: Mapping[str, Any]) -> list[dict[str, str]] | None:
    """Return the digests of the test asset bytes, or ``None`` when unkeyable.

    The asset is whatever the framework already resolved for this invocation:
    the script, the guest C source, the fixture sources or the corpus entries.
    An invocation with no resolvable asset (a CTest binding, a guest command
    typed into the metadata, a source script that lives in a materialized source
    forest) is unkeyable, and an unreadable asset is unkeyable too - a partial
    asset list would let two different tests share one key.
    """

    cwd = Path(invocation.get("cwd") or ".")
    root = Path(invocation.get("repo_root") or cwd)
    candidates: list[tuple[str, Path]] = []
    for key in ASSET_KEYS:
        resolved = invocation.get(key)
        if resolved:
            name = str(invocation.get("script") or invocation.get("source_file") or resolved)
            candidates.append((f"{key}:{name}", Path(resolved)))
    for declared in invocation.get("source_files") or ():
        candidates.append((f"source-files:{declared}", cwd / str(declared)))
    for key in ("corpus", "fixture"):
        declared = invocation.get(key)
        if declared:
            candidates.append((f"{key}:{declared}", root / str(declared)))
    if not candidates:
        return None
    records = []
    for name, path in sorted(candidates):
        digest = digest_file(path)
        if digest is None:
            return None
        records.append({"name": name, "sha256": digest})
    return records


def workload_parameters(
    environ: Mapping[str, str], invocation: Mapping[str, Any]
) -> dict[str, str | None]:
    """Return the workload values that decide how much work the test does."""

    effective = invocation.get("env")
    values: dict[str, str | None] = {}
    for name in WORKLOAD_VARIABLES:
        raw = None
        if isinstance(effective, Mapping):
            raw = effective.get(name)
        if raw is None:
            raw = environ.get(name)
        values[name] = None if raw is None else str(raw)
    return values


def verdict_identity(
    *,
    test: Mapping[str, Any],
    invocation: Mapping[str, Any],
    runtime: Any = None,
    stack: Any = None,
    toolchain: Any = None,
    environ: Mapping[str, str] | None = None,
    arch: str | None = None,
) -> dict[str, Any] | None:
    """Return the identity document that decides whether a verdict may be reused.

    ``None`` means the identity cannot be computed, and the caller must execute
    the test for real: an input that is unknown is never silently treated as
    unchanged. ``runtime``, ``stack`` and ``toolchain`` are the identities the
    caller resolved - the runtime of the profile the test deploys, the stock
    stack request the resource consumes and the guest toolchain the framework
    verified - and are ``None`` only for a test that cannot consume them.
    """

    asset = asset_records(invocation)
    if asset is None:
        return None
    return {
        "schema": SCHEMA,
        "asset": asset,
        "declaration": declaration_digest(test),
        "runner": runner_identity(invocation),
        "runtime": None if runtime is None else _jsonable(runtime),
        "stack": None if stack is None else _jsonable(stack),
        "workload": workload_parameters(environ or {}, invocation),
        "toolchain": None if toolchain is None else _jsonable(toolchain),
        "arch": str(arch or platform.machine()),
    }


def identity_key(identity: Mapping[str, Any]) -> str:
    """Return the cache key for one verdict identity."""

    return canonical_digest({"schema": SCHEMA, "kind": KIND, **_jsonable(identity)})


def entry_path(store: Path, key: str) -> Path:
    return store / ENTRY_DIRECTORY / key


def marker_path(entry: Path) -> Path:
    return _marker_path(entry, name=MARKER_NAME)


def write_entry(
    store: Path,
    key: str,
    *,
    identity: Mapping[str, Any],
    verdict: int,
    duration_seconds: float,
    recorded_at: str | None = None,
    ok_marker: str | None = None,
    bundle: str | None = None,
    guest_stdout_sha256: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> Path:
    """Publish one completed entry for ``key``.

    This is the low-level writer: it records whatever verdict it is given so a
    test can construct a foreign or failing entry, and it refuses an identity
    that does not hash to ``key``, which is the only way an entry could later be
    read back under an identity it does not describe.
    """

    computed = identity_key(identity)
    if computed != key:
        raise ValueError(
            f"verdict identity hashes to {computed}, not the entry key {key}"
        )
    entry = entry_path(store, key)
    _write_marker(
        entry,
        kind=KIND,
        key=key,
        name=MARKER_NAME,
        schema=SCHEMA,
        **{
            "identity-digest": key,
            "identity": _jsonable(identity),
            "verdict": int(verdict),
            "recorded-at": str(recorded_at or timestamp()),
            "duration-seconds": round(float(duration_seconds), 3),
            "ok-marker": None if ok_marker is None else str(ok_marker),
            "bundle": None if bundle is None else str(bundle),
            "guest-stdout-sha256": (
                None if guest_stdout_sha256 is None else str(guest_stdout_sha256)
            ),
            "provenance": _jsonable(provenance or {}),
        },
    )
    return entry


def record_verdict(
    store: Path,
    key: str,
    *,
    identity: Mapping[str, Any],
    duration_seconds: float,
    recorded_at: str | None = None,
    ok_marker: str | None = None,
    bundle: str | None = None,
    guest_stdout_sha256: str | None = None,
    provenance: Mapping[str, Any] | None = None,
) -> Path:
    """Publish a passing verdict for ``key``.

    Only a zero verdict has this entry point: a run that failed is not evidence
    that the identity fails, so a failure is never published for reuse.
    """

    return write_entry(
        store,
        key,
        identity=identity,
        verdict=0,
        duration_seconds=duration_seconds,
        recorded_at=recorded_at,
        ok_marker=ok_marker,
        bundle=bundle,
        guest_stdout_sha256=guest_stdout_sha256,
        provenance=provenance,
    )


def _entry_valid(marker: Mapping[str, Any]) -> bool:
    if marker.get("verdict") != 0:
        return False
    if marker.get("identity-digest") != marker.get("key"):
        return False
    identity = marker.get("identity")
    if not isinstance(identity, dict) or not identity:
        return False
    if not isinstance(marker.get("recorded-at"), str) or not marker["recorded-at"]:
        return False
    if not isinstance(marker.get("duration-seconds"), (int, float)):
        return False
    if not isinstance(marker.get("ok-marker"), (str, type(None))):
        return False
    if not isinstance(marker.get("bundle"), (str, type(None))):
        return False
    stdout = marker.get("guest-stdout-sha256")
    if stdout is not None and not (isinstance(stdout, str) and len(stdout) == 64):
        return False
    return isinstance(marker.get("provenance"), dict)


def read_verdict(store: Path, key: str) -> dict[str, Any] | None:
    """Return the reusable verdict for ``key``, or ``None``.

    Reuse requires a completed entry for exactly this identity, a marker that
    still describes its own identity, and a zero verdict. Anything else - a
    half-written entry, a corrupt marker, a nonzero verdict, a legacy entry -
    is a miss, and a miss executes the test.
    """

    entry = entry_path(store, key)
    if not entry_reusable(
        entry, kind=KIND, key=key, name=MARKER_NAME, schema=SCHEMA, validate=_entry_valid
    ):
        return None
    marker = _read_marker(entry, kind=KIND, key=key, name=MARKER_NAME, schema=SCHEMA)
    if marker is None:
        return None
    touch_entry(entry)
    return marker


def record_event(store: Path, event: str, amount: int = 1) -> CacheStats:
    return _record_event(store, event, amount, schema=SCHEMA)


def read_stats(store: Path) -> CacheStats:
    return _read_stats(store, schema=SCHEMA)


def prune(
    store: Path, max_bytes: int = DEFAULT_MAX_BYTES, *, protect: Iterable[Path] = ()
) -> dict[str, Any]:
    """Evict least-recently-used verdicts until the store fits ``max_bytes``."""

    return prune_entries(
        store, (ENTRY_DIRECTORY,), max_bytes, protect=protect, schema=SCHEMA
    )


def report(store: Path) -> str:
    """Return the run-output accounting line for one verdict store."""

    stats = read_stats(store)
    return (
        f"{stats.value('verdict_hits')}hit/{stats.value('verdict_misses')}miss "
        f"unkeyed={stats.value('verdict_unkeyed')} "
        f"evicted={stats.value('entries_evicted')} store={store}"
    )


def timestamp(now: float | None = None) -> str:
    """Return the UTC recorded-at stamp for one entry."""

    return time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())
    )


def bundle_stdout_digest(bundle: Path | None) -> str | None:
    """Return the guest stdout digest of one debug bundle, if it has one."""

    if bundle is None:
        return None
    return digest_file(Path(bundle) / "stdout.log")
