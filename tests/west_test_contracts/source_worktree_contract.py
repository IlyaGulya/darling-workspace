"""Behavioral contract for local, exact-revision source-worktree hydration."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.source_worktree import (
    cleanup_record,
    default_record_path,
    prepare_source_worktree,
    verify_record,
    write_record,
)


def git(repo: Path, *args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, text=True, capture_output=capture
    )
    return result.stdout.strip() if capture else ""


state_parent = Path(os.environ.get("DW_STATE_ROOT", str(ROOT.parent / ".west-state"))) / "contracts/source-worktree"
state_parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="west-source-worktree-contract-", dir=state_parent) as temp:
    root = Path(temp)
    child = root / "child"
    canonical = root / "canonical"
    source = root / "source"
    build = root / "build"

    child.mkdir()
    git(child, "init", "--quiet")
    git(child, "config", "user.email", "west-test@example.invalid")
    git(child, "config", "user.name", "West source-worktree test")
    (child / "child.txt").write_text("child\n")
    git(child, "add", "child.txt")
    git(child, "commit", "--quiet", "-m", "child")
    child_revision = git(child, "rev-parse", "HEAD", capture=True)

    canonical.mkdir()
    git(canonical, "init", "--quiet")
    git(canonical, "config", "user.email", "west-test@example.invalid")
    git(canonical, "config", "user.name", "West source-worktree test")
    (canonical / "container").mkdir()
    git(canonical, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", f"file://{child}", "container/nested")
    git(canonical, "commit", "--quiet", "-m", "nested")
    root_revision = git(canonical, "rev-parse", "HEAD", capture=True)
    git(canonical, "worktree", "add", "--quiet", "--detach", str(source), root_revision)
    try:
        assert not (source / "container" / "nested" / ".git").exists()
        entries = prepare_source_worktree(source, canonical)
        assert len(entries) == 1
        assert entries[0].relative_path == "container/nested"
        assert entries[0].revision == child_revision
        assert entries[0].created
        assert not (source / "container" / "nested").is_symlink()
        assert git(source / "container" / "nested", "rev-parse", "HEAD", capture=True) == child_revision

        record = default_record_path(source)
        write_record(record, source_root=source, canonical_root=canonical, entries=entries)
        assert verify_record(record)["source_revision"] == root_revision

        build.mkdir()
        (build / "CMakeCache.txt").write_text(
            f"CMAKE_HOME_DIRECTORY:INTERNAL={source}\n"
        )
        assert verify_record(record, build_dir=build)["gitlinks"][0]["revision"] == child_revision

        cleanup_record(record)
        assert not record.exists()
        assert not (source / "container" / "nested").exists()
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(source)],
            cwd=canonical,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # Exercise the active R1 consumer with real Git/source hydration and real CMake failure.
    # No guest executor is invoked: the host false binary is an unused argument, not a fake
    # successful guest/runtime. A successful runtime verdict still requires the real workload.
    (canonical / "obsolete.txt").write_text("baseline-only input\n")
    git(canonical, "add", "obsolete.txt")
    git(canonical, "commit", "--quiet", "-m", "r1 comparison baseline")
    baseline = git(canonical, "rev-parse", "HEAD", capture=True)
    (canonical / "obsolete.txt").unlink()
    (canonical / "committed-link").symlink_to("unavailable-resource")
    cmake_input = (
        "cmake_minimum_required(VERSION 3.16)\n"
        "project(r1_reproduction_negative NONE)\n"
        'message(FATAL_ERROR "R1_INTENTIONAL_CONFIGURE_FAILURE")\n'
    )
    (canonical / "CMakeLists.txt").write_text(cmake_input)
    git(canonical, "add", "obsolete.txt", "committed-link", "CMakeLists.txt")
    git(canonical, "commit", "--quiet", "-m", "r1 committed candidate")
    candidate = git(canonical, "rev-parse", "HEAD", capture=True)
    state = root / "r1-state"
    run = state / "run"
    false = shutil.which("false")
    assert false, "the host negative contract requires the ordinary false binary"
    command = [
        "bash", str(ROOT / "scripts/r1-clean-base-reproduction.sh"),
        "--source", str(canonical), "--revision", candidate, "--baseline", baseline,
        "--executor", false, "--workload", "comparison_on_ring_fd_lifetime",
        "--state-dir", str(run),
    ]
    environment = {**os.environ, "DW_STATE_ROOT": str(state)}
    environment.pop("WEST_JOB_STATE_DIR", None)
    environment.pop("WEST_JOB_ACTIVE", None)
    result = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                            text=True, timeout=120)
    assert result.returncode != 0, (result.stdout, result.stderr)
    assert int((run / "final.rc").read_text()) == result.returncode
    evidence = run / "evidence"
    assert int((evidence / "configure.rc").read_text()) != 0, (result.stdout, result.stderr)
    assert "R1_INTENTIONAL_CONFIGURE_FAILURE" in (evidence / "configure.stderr").read_text()
    assert (evidence / "cleanup.status").read_text().strip() == "NOT_LAUNCHED"
    assert not (evidence / "build-install.command").exists()
    assert not (evidence / "bootstrap.command").exists()
    assert not (evidence / "cleanup.command").exists()
    assert not (run / "runtime").exists()
    selection = json.loads((evidence / "selection.json").read_text())
    assert selection["revision"] == candidate
    assert selection["comparison_baseline"] == baseline
    assert selection["workloads"] == ["comparison_on_ring_fd_lifetime"]
    prefix = Path(selection["guest_prefix"])
    assert prefix.parent == state / "p" and prefix.is_dir()
    assert selection["runtime_root"] == str(run / "runtime")
    assert prefix != Path(selection["runtime_root"])
    reproduced = run / "source"
    assert git(reproduced, "rev-parse", "HEAD", capture=True) == candidate
    assert not (reproduced / "obsolete.txt").exists()
    assert (reproduced / "committed-link").is_symlink()
    assert os.readlink(reproduced / "committed-link") == "unavailable-resource"
    assert (reproduced / "CMakeLists.txt").read_text() == cmake_input
    assert git(reproduced / "container/nested", "rev-parse", "HEAD", capture=True) == child_revision
    record = json.loads((evidence / "source-record.json").read_text())
    assert record["source_revision"] == candidate
    assert record["gitlinks"][0]["revision"] == child_revision
    # An existing run cannot be republished, and uncommitted input cannot be silently omitted.
    before = (evidence / "selection.json").read_bytes()
    worktrees = git(canonical, "worktree", "list", "--porcelain", capture=True)
    repeated = subprocess.run(command, cwd=ROOT, env=environment, capture_output=True,
                              text=True, timeout=120)
    assert repeated.returncode != 0
    assert (evidence / "selection.json").read_bytes() == before
    assert git(canonical, "worktree", "list", "--porcelain", capture=True) == worktrees
    (canonical / "CMakeLists.txt").write_text(cmake_input + "# uncommitted override\n")
    dirty_command = [*command[:-1], str(state / "dirty-run")]
    dirty = subprocess.run(dirty_command, cwd=ROOT, env=environment, capture_output=True,
                           text=True, timeout=120)
    assert dirty.returncode != 0 and not (state / "dirty-run").exists()
    assert (evidence / "selection.json").read_bytes() == before

print("PASS west-source-worktree-contract")
