"""Preserve historical cache ownership, dry-run, lock and worktree GC safety."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'west_commands'))
sys.path.insert(0, str(ROOT))
from test_runtime_cache import (
    BUILD_KIND, SOURCE_KIND, canonical_digest, entry_bytes, gc, locked_entry,
    marker_path, maintenance_root, prune, read_stats, write_marker,
)


def source_entry(root: Path, key: str) -> Path:
    return root / 'source' / key


def build_entry(root: Path, key: str) -> Path:
    return root / 'build' / key


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
    explicit = root / "explicit-runtime-cache"
    assert maintenance_root(root, {"WEST_RUNTIME_BUILD_CACHE_DIR": str(explicit)}) == explicit
    assert maintenance_root(
        root,
        {"WEST_RUNTIME_BUILD_CACHE": "off", "WEST_RUNTIME_BUILD_CACHE_DIR": str(explicit)},
    ) == explicit


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


        source = source_entry(store, '6' * 64)
        source.mkdir(parents=True)
        write_marker(source, kind=SOURCE_KIND, key=source.name)
        with locked_entry(source):
            result = gc(store)
            assert source.is_dir() and not result['evicted'], result
            assert any(row['path'] == str(source) and 'locked' in row['reason']
                       for row in result['skipped']), result
        assert gc(store)['evicted'] == [f'source/{source.name}']


def main() -> int:
    with tempfile.TemporaryDirectory(prefix='west-cache-maintenance-contract-') as temporary:
        root = Path(temporary)
        maintenance_contract(root)
        maintenance_cli_contract(root)
    print('PASS runtime-cache-contract')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
