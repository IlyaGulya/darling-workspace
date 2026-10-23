"""Prove runtime reuse keys on content identity and that the store is bounded.

The reuse claim rests on two behaviours: an unchanged identity must produce the
same key from a different per-run location, and a changed identity must never
reuse an entry. Both are asserted here, together with the store bound and its
accounting, because a cache that cannot state its own hit and miss rate cannot
be trusted in an acceptance run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))
sys.path.insert(0, str(ROOT))

from test_runtime_cache import (  # noqa: E402
    BUILD_KIND,
    DEFAULT_MAX_BYTES,
    SOURCE_KIND,
    CacheStats,
    RuntimeReusePlan,
    build_entry,
    build_key,
    cache_root,
    canonical_digest,
    entry_bytes,
    entry_reusable,
    gc,
    locked_entry,
    marker_path,
    maintenance_root,
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


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    ).stdout


def repository(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "cache contract")
    git(path, "config", "user.email", "cache@example.invalid")
    (path / "value").write_text("reproducible\n")
    git(path, "add", "value")
    git(path, "commit", "-qm", "base")
    return path


def forest(store: Path, key: str, repo: Path, nested_repo: Path | None = None) -> Path:
    entry = source_entry(store, key)
    entry.mkdir(parents=True)
    target = entry / "darling"
    git(repo, "worktree", "add", "--quiet", "--detach", str(target), "HEAD")
    if nested_repo is not None:
        git(nested_repo, "worktree", "add", "--quiet", "--detach", str(target / "nested"), "HEAD")
    write_marker(
        entry, kind=SOURCE_KIND, key=key,
        darling=git(target, "rev-parse", "HEAD").strip(),
    )
    return entry


def snapshot(root: Path) -> dict:
    result = {}
    for path in [root, *root.rglob("*")]:
        metadata = path.lstat()
        contents = (
            os.readlink(path) if path.is_symlink()
            else path.read_bytes() if path.is_file()
            else None
        )
        result[str(path.relative_to(root))] = (
            metadata.st_mode, metadata.st_mtime_ns, contents,
        )
    return result


def maintenance_contract(root: Path) -> None:
    repo = repository(root / "cache-repo")
    nested_repo = repository(root / "cache-nested-repo")
    store = root / "maintenance"
    source = forest(store, "a" * 64, repo, nested_repo)
    locked = source_entry(store, "b" * 64)
    locked.mkdir()
    write_marker(locked, kind=SOURCE_KIND, key=locked.name)
    git_locked = forest(store, "c" * 64, repo)
    git(repo, "worktree", "lock", "--reason", "contract", str(git_locked / "darling"))
    unknown = source_entry(store, "d" * 64)
    unknown.mkdir()
    (unknown / "payload").write_text("not ours")
    malformed = source_entry(store, "e" * 64)
    malformed.mkdir()
    marker_path(malformed).write_text('{"schema":1,"kind":"west-runtime-source","key":"wrong"}')
    outside = root / "outside-cache"
    outside.mkdir()
    (outside / "payload").write_text("keep outside")
    link = source_entry(store, "f" * 64)
    link.symlink_to(outside, target_is_directory=True)
    dirty = forest(store, "7" * 64, repo, nested_repo)
    (dirty / "darling" / "nested" / "value").write_text("user's tracked edit\n")
    untracked = forest(store, "8" * 64, repo)
    (untracked / "darling" / "unique-source.txt").write_text("user's untracked source\n")
    moved = forest(store, "6" * 64, repo)
    git(moved / "darling", "commit", "--allow-empty", "-qm", "user's unique commit")

    identity = {
        "schema_version": 1, "key": "1" * 64, "proof": {},
        "prefix": str(root / "prefix"), "targets": ["payload"],
        "configure_args": ["-G", "Ninja"],
    }
    legacy = store / canonical_digest(identity)
    (legacy / "build").mkdir(parents=True)
    (legacy / "build" / "payload").write_bytes(b"x" * 4096)
    receipt = {
        "schema_version": 1, "kind": BUILD_KIND, "identity": identity,
        "artifacts": [{"path": "payload", "bytes": 4096, "mode": 420, "sha256": "2" * 64}],
    }
    (legacy / "cache-index.json").write_text(json.dumps(receipt))
    fake_legacy = store / ("0" * 64)
    (fake_legacy / "build").mkdir(parents=True)
    (fake_legacy / "cache-index.json").write_text(json.dumps(receipt))
    current_build = build_entry(store, "9" * 64)
    current_build.mkdir(parents=True)
    write_marker(current_build, kind=BUILD_KIND, key=current_build.name)
    owned = [source, legacy, current_build]
    owned_bytes = sum(entry_bytes(entry) for entry in owned)
    lines: list[str] = []
    with locked_entry(locked):
        before = snapshot(root)
        planned = gc(store, dry_run=True, progress=lines.append, environ={})
        assert snapshot(root) == before, "dry-run mutated cache or Git registrations"
        assert set(planned["planned"]) == {str(entry.relative_to(store)) for entry in owned}, planned
        assert planned["planned_bytes"] == owned_bytes, planned
        assert planned["evicted_bytes"] == 0 and planned["evicted"] == [], planned
        assert source.is_dir() and legacy.is_dir()
        assert str(source / "darling") in git(repo, "worktree", "list", "--porcelain")
        assert str(source / "darling" / "nested") in git(nested_repo, "worktree", "list", "--porcelain")
        assert any(row["path"] == str(locked) and "locked" in row["reason"] for row in planned["skipped"])
        assert any(row["path"] == str(git_locked) and "locked" in row["reason"] for row in planned["skipped"])
        for entry in (dirty, untracked):
            assert any(row["path"] == str(entry) and "tracked or untracked" in row["reason"]
                       for row in planned["skipped"]), planned
        assert any(row["path"] == str(moved) and "HEAD differs" in row["reason"]
                   for row in planned["skipped"]), planned

        lines.clear()
        removed = gc(store, progress=lines.append, environ={})
        assert not removed["errors"], removed
        assert set(removed["evicted"]) == set(planned["planned"]), removed
        assert removed["evicted_bytes"] == owned_bytes, removed
        for entry in owned:
            assert not entry.exists(), entry
            assert any(line.startswith("pruning runtime build cache: ") and str(entry) in line for line in lines)
            assert any(line.startswith("pruned runtime build cache: ") and str(entry) in line for line in lines)
        assert str(source / "darling") not in git(repo, "worktree", "list", "--porcelain")
        assert str(source / "darling" / "nested") not in git(nested_repo, "worktree", "list", "--porcelain")
        for entry in (locked, git_locked, unknown, malformed, fake_legacy, outside, dirty, untracked, moved):
            assert entry.is_dir(), entry
        assert link.is_symlink()
        for entry in (dirty, untracked, moved):
            assert str(entry / "darling") in git(repo, "worktree", "list", "--porcelain")
        assert (dirty / "darling" / "nested" / "value").read_text() == "user's tracked edit\n"
        assert str(dirty / "darling" / "nested") in git(nested_repo, "worktree", "list", "--porcelain")
        assert (untracked / "darling" / "unique-source.txt").read_text() == "user's untracked source\n"
        assert git(moved / "darling", "log", "-1", "--format=%s").strip() == "user's unique commit"

    # Bounded automatic eviction uses the identical nested-worktree disposal.
    bounded = root / "bounded-worktrees"
    old = forest(bounded, "3" * 64, repo, nested_repo)
    result = prune(bounded, 1, environ={})
    assert result["evicted"] == [f"source/{old.name}"] and not result["errors"], result
    assert str(old / "darling") not in git(repo, "worktree", "list", "--porcelain")
    assert str(old / "darling" / "nested") not in git(nested_repo, "worktree", "list", "--porcelain")

    # A removal error is visible and must never be counted as reclaimed bytes.
    failed_store = root / "failed-disposal"
    failed = forest(failed_store, "4" * 64, repo)
    with patch("test_runtime_cache.remove_temporary_worktree", return_value="deliberate refusal"):
        result = gc(failed_store, environ={})
    assert result["errors"] and result["evicted"] == [] and result["evicted_bytes"] == 0, result
    assert read_stats(failed_store).value("entries_evicted") == 0
    assert failed.is_dir() and str(failed / "darling") in git(repo, "worktree", "list", "--porcelain")
    gc(failed_store, environ={})
    before = snapshot(root)
    try:
        gc(store, dry_run=True, environ={"DW_STATE_ROOT": str(outside)})
    except ValueError as error:
        assert "DW_STATE_ROOT" in str(error), error
    else:
        raise AssertionError("GC escaped the declared state root")
    assert snapshot(root) == before
    assert maintenance_root(root, {"WEST_RUNTIME_BUILD_CACHE": "off"}) == root / ".west-test/runtime-build-cache"


def maintenance_cli_contract(root: Path) -> None:
    west_module = types.ModuleType("west")
    west_commands_module = types.ModuleType("west.commands")
    west_commands_module.WestCommand = type("WestCommand", (), {})
    sys.modules.setdefault("west", west_module)
    sys.modules.setdefault("west.commands", west_commands_module)
    from west_commands.test import DarlingTest

    class ParserAdder:
        def add_parser(self, name, **kwargs):
            return argparse.ArgumentParser(prog=name, description=kwargs.get("description"))

    command = DarlingTest.__new__(DarlingTest)
    command.name = "test"
    command.description = "cache maintenance contract"
    command.manifest = types.SimpleNamespace(repo_abspath=str(root), projects=[])
    command._resolve_prefix = lambda _args: None
    command._resolve_executor = lambda _arg: None
    command.die = lambda message: (_ for _ in ()).throw(SystemExit(message))
    lines: list[str] = []
    command.inf = lines.append
    command._gc_bundles = lambda *_args, **_kwargs: None
    command._gc_runtime_proof_scratch = lambda *_args, **_kwargs: None
    command._gc_west_temp_worktree_registrations = lambda **_kwargs: None
    command._gc_guest_runner_output = lambda *_args, **_kwargs: None
    parser = command.do_add_parser(ParserAdder())
    state = root / "cli-state"
    state.mkdir()
    store = state / "cache"
    entry = build_entry(store, "5" * 64)
    entry.mkdir(parents=True)
    write_marker(entry, kind=BUILD_KIND, key=entry.name)
    flags = ["--gc", "--gc-runtime-build-cache", "--runtime-build-cache-root", str(store)]
    with patch.dict(os.environ, {"DW_STATE_ROOT": str(state)}):
        before = snapshot(state)
        command._do_run(parser.parse_args([*flags, "--dry-run"]), [])
        assert snapshot(state) == before and entry.is_dir()
        assert any(line.startswith("would prune runtime build cache: ") for line in lines), lines
        command._do_run(parser.parse_args(["--gc"]), [])
        assert entry.is_dir(), "ordinary GC implicitly opted into cache deletion"
        command._do_run(parser.parse_args(flags), [])
        assert not entry.exists()
        for argv in (
            ["--gc-runtime-build-cache"],
            ["--gc", "--runtime-build-cache-root", str(store)],
            ["--gc", "--gc-runtime-build-cache", "--runtime-build-cache-root", str(root / "maintenance")],
        ):
            try:
                command._do_run(parser.parse_args(argv), [])
            except SystemExit as error:
                assert "requires" in str(error) or "outside" in str(error), error
            else:
                raise AssertionError(f"unsafe maintenance arguments accepted: {argv}")

        # The public source context keeps the producer lock for the whole yield,
        # so an otherwise idle completed source forest is protected during use.
        source = source_entry(store, "6" * 64)
        source.mkdir(parents=True)
        write_marker(source, kind=SOURCE_KIND, key=source.name)

        @contextmanager
        def source_context(*_args, **_kwargs):
            yield source / "darling"

        command._runtime_source_materializer = lambda: types.SimpleNamespace(
            guest_runtime_source_forest=source_context,
        )
        with command._guest_runtime_source_forest(
            {}, {}, omit_patch=False, root=source, reuse_key=source.name,
        ):
            result = gc(store)
            assert source.is_dir() and result["evicted"] == [], result
            assert any(row["path"] == str(source) and "locked" in row["reason"]
                       for row in result["skipped"]), result
        assert gc(store)["evicted"] == [f"source/{source.name}"]


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
            write_marker(
                target,
                kind=BUILD_KIND if target == keep else SOURCE_KIND,
                key=target.name,
            )
        old_size = entry_bytes(old)
        retained_size = entry_bytes(new) + entry_bytes(keep)
        now = time.time()
        os.utime(old, (now - 600, now - 600))
        os.utime(new, (now - 300, now - 300))
        os.utime(keep, (now - 900, now - 900))
        result = prune(store, retained_size, protect=[keep], environ={})
        assert result["evicted"] == [f"source/{old.name}"], result
        assert old.exists() is False and new.is_dir() and keep.is_dir()
        assert result["evicted_bytes"] == old_size and result["bytes"] == retained_size, result
        assert read_stats(store).value("entries_evicted") == 1
        again = prune(store, DEFAULT_MAX_BYTES, protect=[keep], environ={})
        assert again["evicted"] == [] and again["bytes"] == retained_size, again
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
            write_marker(target, kind=SOURCE_KIND, key=target.name)
        stamp = time.time()
        os.utime(cold, (stamp - 900, stamp - 900))
        os.utime(warm, (stamp - 900, stamp - 900))
        touch_entry(warm)
        result = prune(recency, entry_bytes(warm), environ={})
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

        maintenance_contract(root)
        maintenance_cli_contract(root)

    print("PASS runtime-cache-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
