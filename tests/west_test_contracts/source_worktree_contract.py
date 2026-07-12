"""Behavioral contract for local, exact-revision source-worktree hydration."""

from __future__ import annotations

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


with tempfile.TemporaryDirectory(prefix="west-source-worktree-contract-") as temp:
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

print("PASS west-source-worktree-contract")
