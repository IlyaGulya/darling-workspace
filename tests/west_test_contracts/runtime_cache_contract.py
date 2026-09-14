"""Prove runtime reuse keys on content identity and that the store is bounded.

The reuse claim rests on two behaviours: an unchanged identity must produce the
same key from a different per-run location, and a changed identity must never
reuse an entry. Both are asserted here, together with the store bound and its
accounting, because a cache that cannot state its own hit and miss rate cannot
be trusted in an acceptance run.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_runtime_cache import (  # noqa: E402
    BUILD_KIND,
    DEFAULT_MAX_BYTES,
    SOURCE_KIND,
    CacheStats,
    RuntimeReusePlan,
    build_entry,
    build_key,
    cache_root,
    entry_reusable,
    marker_path,
    max_bytes,
    prune,
    read_marker,
    read_stats,
    record_event,
    source_entry,
    source_identity,
    source_key,
    touch_entry,
    write_marker,
)

IDENTITY = {
    "schema": 2,
    "profile": "homebrew-ring-on",
    "source-profile": "ring-comparison",
    "source-lock-sha256": "a" * 64,
    "source-commits": {"darling": "b" * 40, "darlingserver": "c" * 40},
    "patchsets": [
        {
            "profile": "ring-comparison",
            "sha256": "d" * 64,
            "patches": [
                {
                    "module": "darlingserver",
                    "path": "ring-comparison/darlingserver/ring-comparison-server.patch",
                    "source-base": "e" * 40,
                    "source-commit": "f" * 40,
                    "sha256sum": "0" * 64,
                }
            ],
        }
    ],
    "runtime-manifest-sha256": "1" * 64,
    "runtime-profile-definition-sha256": "2" * 64,
    "launcher-sha256": "3" * 64,
}

BUILD_IDENTITY = {
    "targets": ["darlingserver"],
    "proof": {"runtime-artifacts": [{"build-targets": ["darlingserver"]}]},
    "configure_args": ["-DCMAKE_BUILD_TYPE=Release", "-DDARLING_RING_TRANSPORT=ON"],
    "compiler_fingerprint": "4" * 64,
    "prefix": "/tmp/darling-rootless-acceptance",
}

REQUESTS = {"omit_patch": False, "patch_path": "darling/homebrew-prefix-tooling.patch", "bad_profile": None}


def reduced(identity: dict, **overrides) -> dict:
    request = dict(REQUESTS)
    request.update(overrides)
    return source_identity(identity, **request)


def main() -> int:
    # A source key covers source content, not the per-run launcher or location.
    base = source_key(reduced(IDENTITY))
    moved = dict(IDENTITY)
    moved["launcher-sha256"] = "9" * 64
    assert source_key(reduced(moved)) == base, (
        "source reuse must not depend on the deployed launcher, which changes "
        "with every prefix"
    )
    assert "launcher-sha256" not in reduced(IDENTITY)

    # Strict invalidation: every declared input must move the key.
    mutations = {
        "source commit": {"source-commits": {"darling": "z" * 40, "darlingserver": "c" * 40}},
        "source lock": {"source-lock-sha256": "y" * 64},
        "runtime manifest": {"runtime-manifest-sha256": "x" * 64},
        "profile definition": {"runtime-profile-definition-sha256": "w" * 64},
    }
    for label, mutation in mutations.items():
        changed = dict(IDENTITY)
        changed.update(mutation)
        assert source_key(reduced(changed)) != base, f"{label} must invalidate the source key"
    patchset_mutation = json.loads(json.dumps(IDENTITY))
    patchset_mutation["patchsets"][0]["patches"][0]["source-commit"] = "v" * 40
    assert source_key(reduced(patchset_mutation)) != base, (
        "a patch record change must invalidate the source key"
    )
    patchset_sha = json.loads(json.dumps(IDENTITY))
    patchset_sha["patchsets"][0]["sha256"] = "u" * 64
    assert source_key(reduced(patchset_sha)) != base, "a patchset digest change must invalidate"
    for label, request in (
        ("omit-patch", {"omit_patch": True}),
        ("patch path", {"patch_path": "xnu/vchroot-fdless-path-rpc.patch"}),
        ("bad profile", {"bad_profile": "current-minus-patch"}),
    ):
        assert source_key(reduced(IDENTITY, **request)) != base, f"{label} must invalidate"
    # A RED arm builds from a revision the profile identity does not name, so
    # two different bad revisions must not share a source forest.
    red_a = source_key(reduced(IDENTITY, omit_patch=True, bad_revision="a" * 40))
    red_b = source_key(reduced(IDENTITY, omit_patch=True, bad_revision="b" * 40))
    assert red_a != red_b, "a different bad revision must invalidate the source key"
    assert red_a != base, "a RED arm must not reuse the GREEN source forest"
    assert source_key(reduced(IDENTITY, bad_revision="a" * 40)) == base, (
        "a bad revision is irrelevant when the arm is not a RED arm"
    )
    # A change to how forests are materialized invalidates older entries.
    import test_runtime_cache

    original_materializer = test_runtime_cache.MATERIALIZER_IDENTITY
    try:
        test_runtime_cache.MATERIALIZER_IDENTITY = "west-runtime-source-forest-v2"
        assert source_key(reduced(IDENTITY)) != base, (
            "a materializer change must invalidate the source key"
        )
    finally:
        test_runtime_cache.MATERIALIZER_IDENTITY = original_materializer
    assert source_key(reduced(IDENTITY)) == base

    # Build keys extend the source key and react to build inputs.
    build = build_key(base, BUILD_IDENTITY)
    assert build_key(base, BUILD_IDENTITY) == build
    assert build_key(source_key(reduced(moved)), BUILD_IDENTITY) == build, (
        "the build key must inherit source stability"
    )
    ring_off = dict(BUILD_IDENTITY)
    ring_off["configure_args"] = ["-DDARLING_RING_TRANSPORT=OFF"]
    assert build_key(base, ring_off) != build, "compile defines must invalidate the build key"
    other_targets = dict(BUILD_IDENTITY)
    other_targets["targets"] = ["darlingserver", "system_kernel"]
    assert build_key(base, other_targets) != build, "target selection must invalidate"
    other_compiler = dict(BUILD_IDENTITY)
    other_compiler["compiler_fingerprint"] = "5" * 64
    assert build_key(base, other_compiler) != build, "toolchain identity must invalidate"
    other_prefix = dict(BUILD_IDENTITY)
    other_prefix["prefix"] = "/tmp/darling-rootless-other"
    assert build_key(base, other_prefix) != build, (
        "the deploy prefix must invalidate until artifacts are proven prefix-free"
    )

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        manifest_repo = root / "manifest"
        manifest_repo.mkdir()

        # Root resolution: a state root by default, explicit configuration wins,
        # and a disabled store stays disabled.
        default_root = cache_root(manifest_repo, {})
        assert default_root == manifest_repo / ".west-test" / "runtime-build-cache"
        assert not default_root.exists(), "resolving the store must not create it"
        explicit = root / "elsewhere"
        assert cache_root(manifest_repo, {"WEST_RUNTIME_BUILD_CACHE_DIR": str(explicit)}) == explicit
        assert cache_root(
            manifest_repo,
            {"WEST_RUNTIME_BUILD_CACHE_DIR": str(explicit), "WEST_RUNTIME_BUILD_CACHE": "off"},
        ) is None
        assert cache_root(manifest_repo, {"WEST_RUNTIME_BUILD_CACHE": "0"}) is None
        for bad in ({"WEST_RUNTIME_BUILD_CACHE_DIR": "relative/path"},):
            try:
                cache_root(manifest_repo, bad)
            except ValueError:
                pass
            else:
                raise AssertionError("a relative store root must be rejected")

        # Reuse requires a completed entry for exactly this kind and key.
        entry = source_entry(default_root, base)
        entry.mkdir(parents=True)
        (entry / "darling").mkdir()
        assert not entry_reusable(entry, kind=SOURCE_KIND, key=base), (
            "an entry without a completion marker must not be reused"
        )
        write_marker(entry, kind=SOURCE_KIND, key=base, revision="b" * 40)
        assert entry_reusable(entry, kind=SOURCE_KIND, key=base)
        assert not entry_reusable(entry, kind=SOURCE_KIND, key="f" * 64), (
            "a different key must not reuse this entry"
        )
        assert not entry_reusable(entry, kind=BUILD_KIND, key=base), (
            "a different entry kind must not reuse this entry"
        )
        assert not entry_reusable(
            entry,
            kind=SOURCE_KIND,
            key=base,
            validate=lambda marker: marker.get("revision") == "9" * 40,
        ), "a validator veto must prevent reuse"
        assert read_marker(entry, kind=SOURCE_KIND, key=base)["revision"] == "b" * 40
        marker_path(entry).write_text("{ not json")
        assert not entry_reusable(entry, kind=SOURCE_KIND, key=base), (
            "a corrupt marker must not be reused"
        )
        marker_path(entry).unlink()
        assert read_marker(entry, kind=SOURCE_KIND, key=base) is None

        # Accounting accumulates and survives a corrupt file without raising.
        assert read_stats(default_root).counters == {}
        record_event(default_root, "source_hits")
        record_event(default_root, "source_hits")
        record_event(default_root, "source_misses")
        stats = read_stats(default_root)
        assert stats.value("source_hits") == 2 and stats.value("source_misses") == 1
        assert stats.rate("source_hits", "source_misses") == 66.7, stats.counters
        assert CacheStats().rate("source_hits", "source_misses") == 100.0
        (default_root / "stats.json").write_text("{ broken")
        assert read_stats(default_root).counters == {}

        # Pruning is bounded, least-recently-used first, and never evicts the
        # entry the caller is about to use.
        store = root / "store"
        old = source_entry(store, "1" * 64)
        new = source_entry(store, "2" * 64)
        keep = build_entry(store, "3" * 64)
        for target, size in ((old, 4096), (new, 4096), (keep, 4096)):
            target.mkdir(parents=True)
            (target / "payload.bin").write_bytes(b"x" * size)
        now = time.time()
        os.utime(old, (now - 600, now - 600))
        os.utime(new, (now - 300, now - 300))
        os.utime(keep, (now - 900, now - 900))
        result = prune(store, 8192, protect=[keep])
        assert result["evicted"] == [f"source/{old.name}"], result
        assert old.exists() is False and new.is_dir() and keep.is_dir()
        assert result["evicted_bytes"] == 4096 and result["bytes"] <= 8192, result
        assert read_stats(store).value("entries_evicted") == 1
        again = prune(store, DEFAULT_MAX_BYTES, protect=[keep])
        assert again["evicted"] == [] and again["bytes"] == 8192, again
        try:
            prune(store, 0)
        except ValueError:
            pass
        else:
            raise AssertionError("a non-positive store bound must be rejected")

        # The bound is explicit and a reused entry counts as recently used, so
        # a bounded store evicts cold entries instead of the one in use.
        assert max_bytes({}) == DEFAULT_MAX_BYTES
        assert max_bytes({"WEST_RUNTIME_BUILD_CACHE_MAX_BYTES": "1024"}) == 1024
        for bad in ("lots", "0", "-5"):
            try:
                max_bytes({"WEST_RUNTIME_BUILD_CACHE_MAX_BYTES": bad})
            except ValueError:
                continue
            raise AssertionError(f"a bound of {bad!r} must be rejected")
        assert max_bytes({"WEST_RUNTIME_BUILD_CACHE_MAX_BYTES": "  "}) == DEFAULT_MAX_BYTES, (
            "an empty bound means the configured default, not zero"
        )
        recency = root / "recency"
        cold = source_entry(recency, "4" * 64)
        warm = source_entry(recency, "5" * 64)
        for target in (cold, warm):
            target.mkdir(parents=True)
            (target / "payload.bin").write_bytes(b"y" * 4096)
        stamp = time.time()
        os.utime(cold, (stamp - 900, stamp - 900))
        os.utime(warm, (stamp - 900, stamp - 900))
        touch_entry(warm)
        result = prune(recency, 4096)
        assert result["evicted"] == [f"source/{cold.name}"], result
        assert warm.is_dir(), "the most recently used entry must survive"

        # The plan carries the locations and the accounting the run reports.
        plan = RuntimeReusePlan(store=default_root, source_key=base, build_key="f" * 64)
        assert plan.source_entry == source_entry(default_root, base)
        assert plan.build_cache == (default_root, "f" * 64)
        plan.record_build(False)
        plan.record_build(True)
        plan.record_build(True)
        reported = plan.report()
        assert "build=2hit/1miss" in reported, reported
        assert str(default_root) in reported

    print("PASS runtime-cache-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
