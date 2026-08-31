"""Contract for durable, explicitly collected runtime failure evidence."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_runtime_evidence import RuntimeEvidenceStore
from west_commands.owned_scratch import discard_exact


relative_store = RuntimeEvidenceStore(Path("relative-runtime-evidence"))
assert relative_store.root.is_absolute(), relative_store.root


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / "evidence"
    store = RuntimeEvidenceStore(root)

    with store.session("ordinary guest provider", {"provider": "homebrew"}) as session:
        (session.source_root / "darling").mkdir(parents=True)
        (session.build_root / "build.ninja").parent.mkdir(parents=True)
        (session.build_root / "build.ninja").write_text("build all: phony\n")

    assert not root.exists() or not list(root.iterdir()), list(root.glob("*"))

    with store.session("reported guest failure", {"provider": "homebrew"}) as session:
        (session.source_root / "darling").mkdir(parents=True)
        session.build_root.mkdir(parents=True)
        session.preserve(RuntimeError("guest verdict was non-zero"))

    reported_entries = store.entries()
    assert len(reported_entries) == 1, reported_entries
    reported_manifest = json.loads((reported_entries[0] / "manifest.json").read_text())
    assert reported_manifest["failure"]["message"] == "guest verdict was non-zero", reported_manifest
    store.gc(max_age_hours=0, keep_last=0, dry_run=False)

    repo = Path(temp) / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "west test"], cwd=repo, check=True)
    (repo / "source.c").write_text("base\n")
    subprocess.run(["git", "add", "source.c"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, check=True)

    active = store.start("active materialization", {"provider": "homebrew"})
    active_outcome = store.gc(max_age_hours=0, keep_last=0, dry_run=False)
    assert active_outcome.removed == [] and active_outcome.retained == []
    assert active.directory.is_dir()
    active.discard()

    orphan_session = store.start("orphan", {"provider": "homebrew"})
    orphan = orphan_session.directory
    orphan_worktree = orphan / "source/darling"
    orphan_worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(orphan_worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    orphan_session.record_worktrees([(repo, orphan_worktree)])
    orphan_session._release_lock()
    orphan_session._scratch.close()
    before_dry_run = sorted(path.relative_to(orphan) for path in orphan.rglob("*"))
    dry_outcome = store.gc(max_age_hours=0, keep_last=0, dry_run=True)
    assert dry_outcome.removed == [orphan] and dry_outcome.retained == []
    assert sorted(path.relative_to(orphan) for path in orphan.rglob("*")) == before_dry_run
    orphan_outcome = store.gc(max_age_hours=0, keep_last=0, dry_run=False)
    assert orphan_outcome.removed == [orphan] and orphan_outcome.retained == []
    assert not orphan.exists()
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {orphan_worktree}" not in worktree_listing, worktree_listing

    legacy_orphan = root / ".inflight-legacy"
    legacy_worktree = legacy_orphan / "source/darling"
    legacy_worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(legacy_worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    before_legacy = sorted(path.relative_to(legacy_orphan) for path in legacy_orphan.rglob("*"))
    legacy_outcome = store.gc(max_age_hours=0, keep_last=0, dry_run=False)
    assert legacy_outcome.removed == []
    assert [path for path, _reason in legacy_outcome.retained] == [legacy_orphan]
    assert legacy_orphan.exists()
    assert sorted(path.relative_to(legacy_orphan) for path in legacy_orphan.rglob("*")) == before_legacy
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {legacy_worktree}" in worktree_listing, worktree_listing
    subprocess.run(["git", "worktree", "remove", "--force", str(legacy_worktree)], cwd=repo, check=True)
    shutil.rmtree(legacy_orphan)

    try:
        with store.session("rootless bootstrap", {"provider": "homebrew-rootless-no-mount"}) as session:
            source_worktree = session.source_root / "darling"
            source_worktree.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(source_worktree), "HEAD"],
                cwd=repo,
                check=True,
            )
            session.record_worktrees([(repo, source_worktree)])
            (source_worktree / "source.c").write_text("broken\n")
            session.build_root.mkdir(parents=True)
            (session.build_root / "build.ninja").write_text("build all: phony\n")
            trace = Path(temp) / "rootless-boot.trace"
            trace.write_text("dyld main-entry-ready\n")
            session.record_failure_detail(
                phase="bootstrap",
                summary="E-UNION login shell did not reach a verdict",
                returncode=124,
                command=["darling", "shell", "/bin/bash", "--login", "-c", ":"],
                output="semaphore_timedwait failed (internally): -111\n",
                artifacts=[trace],
            )
            raise RuntimeError("shellspawn readiness did not complete")
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced runtime failure unexpectedly passed")

    entries = store.entries()
    assert len(entries) == 1, entries
    entry = entries[0]
    manifest = json.loads((entry / "manifest.json").read_text())
    assert manifest["schema"] == 1, manifest
    assert manifest["status"] == "failed", manifest
    assert manifest["context"] == {"provider": "homebrew-rootless-no-mount"}, manifest
    assert manifest["failure"]["type"] == "RuntimeError", manifest
    assert "shellspawn readiness" in manifest["failure"]["message"], manifest
    assert manifest["paths"] == {"source": "source/darling"}, manifest
    assert (entry / manifest["paths"]["source"] / "source.c").read_text() == "broken\n"
    assert not (entry / "build").exists()
    assert manifest["worktrees"] == [{"repo": str(repo), "path": "source/darling"}], manifest
    assert manifest["diagnostics"] == [
        {
            "phase": "bootstrap",
            "summary": "E-UNION login shell did not reach a verdict",
            "returncode": 124,
            "command": ["darling", "shell", "/bin/bash", "--login", "-c", ":"],
            "output": "failure.raw.log",
        }
    ], manifest
    assert (entry / "failure.raw.log").read_text() == (
        "semaphore_timedwait failed (internally): -111\n"
    )
    assert not (entry / "diagnostics").exists()
    assert store.resolve(entry.name) == entry
    assert store.resolve(entry.name.rsplit("-", 1)[1]) == entry
    assert store.manifest(entry) == manifest
    replay = store.replay_report(entry.name.rsplit("-", 1)[1])
    assert replay["unit"] == entry.name, replay
    assert replay["diagnostics"] == manifest["diagnostics"], replay
    assert replay["attachments"] == [
        {"path": "failure.raw.log", "bytes": 46},
    ], replay
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert f"worktree {entry / 'source/darling'}" in worktree_listing, worktree_listing

    dirty_dry = store.gc(max_age_hours=0, keep_last=0, dry_run=True)
    assert dirty_dry.removed == [entry] and dirty_dry.retained == []
    assert entry.is_dir()
    dirty_outcome = store.gc(max_age_hours=0, keep_last=0, dry_run=False)
    assert dirty_outcome.removed == []
    assert [path for path, _reason in dirty_outcome.retained] == [entry]
    assert entry.exists()
    discard_exact(root, entry, force_dirty=True)
    assert not entry.exists()
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert f"worktree {entry / 'source/darling'}" not in worktree_listing, worktree_listing

    oversized = Path(temp) / "oversized.trace"
    with oversized.open("wb") as handle:
        handle.truncate(64 * 1024 * 1024 + 1)
    with store.session("bounded diagnostics", {"provider": "homebrew"}) as session:
        session.record_failure_detail(
            phase="bootstrap",
            summary="bounded artifact retention",
            artifacts=[oversized],
        )
        session.preserve(RuntimeError("retain bounded diagnostics"))
    bounded_entry = store.entries()[0]
    bounded_manifest = json.loads((bounded_entry / "manifest.json").read_text())
    assert "artifacts" not in bounded_manifest["diagnostics"][0], bounded_manifest
    store.gc(max_age_hours=0, keep_last=0, dry_run=False)

    try:
        with store.session("already pruned worktree", {"provider": "homebrew"}) as session:
            source_worktree = session.source_root / "darling"
            source_worktree.parent.mkdir(parents=True)
            subprocess.run(
                ["git", "worktree", "add", "--quiet", "--detach", str(source_worktree), "HEAD"],
                cwd=repo,
                check=True,
            )
            session.record_worktrees([(repo, source_worktree)])
            raise RuntimeError("retain evidence for stale-registration GC")
    except RuntimeError:
        pass
    else:
        raise AssertionError("forced stale-registration failure unexpectedly passed")

    stale_entry = store.entries()[0]
    stale_worktree = stale_entry / "source/darling"
    parked_worktree = Path(temp) / "parked-worktree"
    stale_worktree.rename(parked_worktree)
    subprocess.run(["git", "worktree", "prune"], cwd=repo, check=True)
    parked_worktree.rename(stale_worktree)
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert f"worktree {stale_worktree}" not in worktree_listing, worktree_listing

    stale_outcome = store.gc(max_age_hours=0, keep_last=0, dry_run=False)
    assert stale_outcome.removed == [stale_entry] and stale_outcome.retained == []
    assert not stale_entry.exists()

print("PASS runtime-evidence-contract")
