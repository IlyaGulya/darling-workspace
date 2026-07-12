"""Contract for durable, explicitly collected runtime failure evidence."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_runtime_evidence import RuntimeEvidenceStore


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
    assert manifest["paths"] == {"source": "source/darling", "build": "build"}, manifest
    assert (entry / manifest["paths"]["source"] / "source.c").read_text() == "broken\n"
    assert (entry / manifest["paths"]["build"] / "build.ninja").is_file()
    assert manifest["worktrees"] == [{"repo": str(repo), "path": "source/darling"}], manifest
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert f"worktree {entry / 'source/darling'}" in worktree_listing, worktree_listing

    assert store.gc(max_age_hours=0, keep_last=0, dry_run=True) == [entry]
    assert entry.is_dir()
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == [entry]
    assert not entry.exists()
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout
    assert f"worktree {entry / 'source/darling'}" not in worktree_listing, worktree_listing

print("PASS runtime-evidence-contract")
