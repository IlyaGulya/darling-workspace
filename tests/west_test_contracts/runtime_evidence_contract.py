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
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == []
    assert active.directory.is_dir()
    active.discard()

    orphan = root / ".inflight-orphan"
    orphan_worktree = orphan / "source/darling"
    orphan_worktree.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "worktree", "add", "--quiet", "--detach", str(orphan_worktree), "HEAD"],
        cwd=repo,
        check=True,
    )
    (orphan / ".worktrees.json").write_text(
        json.dumps([{"repo": str(repo), "path": "source/darling"}]) + "\n"
    )
    before_dry_run = sorted(path.relative_to(orphan) for path in orphan.rglob("*"))
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=True) == [orphan]
    assert sorted(path.relative_to(orphan) for path in orphan.rglob("*")) == before_dry_run
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == [orphan]
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
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == [legacy_orphan]
    assert not legacy_orphan.exists()
    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert f"worktree {legacy_worktree}" not in worktree_listing, worktree_listing

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
    assert manifest["paths"] == {"source": "source/darling", "build": "build"}, manifest
    assert (entry / manifest["paths"]["source"] / "source.c").read_text() == "broken\n"
    assert (entry / manifest["paths"]["build"] / "build.ninja").is_file()
    assert manifest["worktrees"] == [{"repo": str(repo), "path": "source/darling"}], manifest
    assert manifest["diagnostics"] == [
        {
            "phase": "bootstrap",
            "summary": "E-UNION login shell did not reach a verdict",
            "returncode": 124,
            "command": ["darling", "shell", "/bin/bash", "--login", "-c", ":"],
            "output": "diagnostics/0-output.log",
            "artifacts": ["diagnostics/0-rootless-boot.trace"],
        }
    ], manifest
    assert (entry / "diagnostics/0-output.log").read_text() == (
        "semaphore_timedwait failed (internally): -111\n"
    )
    assert (entry / "diagnostics/0-rootless-boot.trace").read_text() == "dyld main-entry-ready\n"
    assert store.resolve(entry.name) == entry
    assert store.resolve(entry.name.rsplit("-", 1)[1]) == entry
    assert store.manifest(entry) == manifest
    replay = store.replay_report(entry.name.rsplit("-", 1)[1])
    assert replay["unit"] == entry.name, replay
    assert replay["diagnostics"] == manifest["diagnostics"], replay
    assert replay["attachments"] == [
        {"path": "diagnostics/0-output.log", "bytes": 46},
        {"path": "diagnostics/0-rootless-boot.trace", "bytes": 22},
    ], replay
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

    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == [stale_entry]
    assert not stale_entry.exists()

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / "evidence"
    store = RuntimeEvidenceStore(root)

    # Ownership evidence exists from birth, and retention records the state the
    # unit is actually in.
    probe = store.start("marker at birth", {"provider": "homebrew"})
    marker_path = probe.directory / ".unit.json"
    assert marker_path.is_file(), sorted(path.name for path in probe.directory.iterdir())
    assert json.loads(marker_path.read_text())["state"] == "inflight"
    retained = probe.retain(RuntimeError("marker after retention"))
    assert json.loads((retained / ".unit.json").read_text())["state"] == "retained"
    assert json.loads((retained / "manifest.json").read_text())["schema"] == 1
    store.gc(max_age_hours=0, keep_last=0, dry_run=False)

    # Interruption semantics: a retention that dies before its manifest exists
    # must publish nothing. The rename is the publish, so the manifest is written
    # while the unit is still hidden.
    doomed = store.start("interrupted retention", {"provider": "homebrew"})
    healthy_write = doomed._write_json

    def fail_manifest(name, value):
        if name == "manifest.json":
            raise RuntimeError("interrupted before the manifest was published")
        return healthy_write(name, value)

    doomed._write_json = fail_manifest
    try:
        doomed.retain(RuntimeError("interrupted"))
    except RuntimeError:
        pass
    else:
        raise AssertionError("injected manifest failure unexpectedly passed")
    assert [entry.name for entry in root.iterdir() if entry.name.startswith("runtime-evidence-")] == []
    assert doomed.directory.name.startswith(".inflight-"), doomed.directory
    doomed._write_json = healthy_write
    doomed.discard()

    # The bad model this replaced, kept as the reason the order matters: renaming
    # first and writing the manifest afterwards publishes a directory that
    # entries() ignores and GC cannot reclaim. That is how a multi-gigabyte
    # orphan appeared in the field, visible to ls and invisible to the tooling.
    bad_root = Path(temp) / "bad-model"
    bad_root.mkdir()
    bad_unit = Path(tempfile.mkdtemp(prefix=".inflight-", dir=bad_root))
    (bad_unit / "payload").write_text("multi-gigabyte in the field\n")
    bad_published = bad_root / "runtime-evidence-19700101T000000Z-deadbeef1234"
    bad_unit.rename(bad_published)
    bad_store = RuntimeEvidenceStore(bad_root)
    assert bad_store.entries() == [], bad_store.entries()
    assert bad_store.gc(max_age_hours=0, keep_last=0, dry_run=False) == []
    assert bad_published.is_dir()

    # A name is not ownership: the look-alike is reported, and never deleted.
    assert bad_store.unowned_units() == [bad_published], bad_store.unowned_units()
    assert bad_store.unit_kind(bad_published) == "unowned"

    lookalike = root / "runtime-evidence-lookalike-0000"
    lookalike.mkdir()
    (lookalike / "payload").write_text("not ours\n")
    assert store.unowned_units() == [lookalike], store.unowned_units()
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=True) == []
    store.gc(max_age_hours=0, keep_last=0, dry_run=False)
    assert lookalike.is_dir(), "GC deleted a directory it could not prove it owned"

    # Removing a unit is announced before and after, so a long deletion is not
    # indistinguishable from a hung one.
    with store.session("progress", {"provider": "homebrew"}) as session:
        session.preserve(RuntimeError("retain for progress"))
    lines: list[str] = []
    selected = store.gc(max_age_hours=0, keep_last=0, dry_run=False, progress=lines.append)
    assert len(selected) == 1, selected
    assert lines == [f"pruning {selected[0].name}", f"pruned {selected[0].name}"], lines

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp) / "evidence"
    store = RuntimeEvidenceStore(root)

    # A published unit that carries its marker but no manifest is structurally
    # owned and incomplete: the state an interrupted retention leaves behind. It
    # is selected by age like any other unit, not by its name.
    interrupted = root / "runtime-evidence-20260101T000000Z-aaaaaaaaaaaa"
    interrupted.mkdir(parents=True)
    (interrupted / ".unit.json").write_text(
        json.dumps({"schema": 1, "owner": "runtime-evidence", "state": "retained"}) + "\n"
    )
    (interrupted / "payload").write_text("kept source tree\n")
    assert store.unit_kind(interrupted) == "inflight", store.unit_kind(interrupted)
    assert store.entries() == [], store.entries()
    assert store.unowned_units() == [], store.unowned_units()
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=True) == [interrupted]
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == [interrupted]
    assert not interrupted.exists()

    # And the look-alike beside it is still refused, even now that an incomplete
    # unit is collectable: the difference is the marker, not the name.
    lookalike = root / "runtime-evidence-20260101T000001Z-bbbbbbbbbbbb"
    lookalike.mkdir(parents=True)
    (lookalike / "payload").write_text("someone else's\n")
    assert store.gc(max_age_hours=0, keep_last=0, dry_run=False) == []
    assert lookalike.is_dir(), "an unowned incomplete name-match was deleted"
    assert store.unowned_units() == [lookalike], store.unowned_units()

print("PASS runtime-evidence-contract")
