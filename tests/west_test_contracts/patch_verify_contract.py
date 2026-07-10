"""Behavioral contracts for disposable Git worktrees used by ``west patch``."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

west_module = types.ModuleType("west")
west_commands_module = types.ModuleType("west.commands")


class WestCommand:
    pass


west_commands_module.WestCommand = WestCommand
sys.modules.setdefault("west", west_module)
sys.modules.setdefault("west.commands", west_commands_module)

from west_commands.patch import (
    TEMPORARY_PATCH_GIT_OPTIONS,
    git_for_temporary_patch_application,
)


assert TEMPORARY_PATCH_GIT_OPTIONS == (
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, stdout=subprocess.DEVNULL)


with tempfile.TemporaryDirectory(prefix="west-patch-verify-contract-") as temp:
    repo = Path(temp)
    git(repo, "init", "--quiet")
    git(repo, "config", "user.email", "west-test@example.invalid")
    git(repo, "config", "user.name", "West patch test")
    # Force a repository state where a normal ``git am`` would consider auto
    # maintenance. The temporary verifier must leave such housekeeping alone.
    git(repo, "config", "gc.auto", "1")
    (repo / "fixture.txt").write_text("base\n")
    git(repo, "add", "fixture.txt")
    git(repo, "commit", "--quiet", "-m", "base")
    for index in range(32):
        blob = repo / f"blob-{index}"
        blob.write_text(f"{index}\n")
        git(repo, "hash-object", "-w", blob.name)

    (repo / "fixture.txt").write_text("patched\n")
    git(repo, "commit", "--quiet", "-am", "patched")
    patch = repo / "fixture.patch"
    with patch.open("wb") as output:
        subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=repo,
            check=True,
            stdout=output,
        )
    git(repo, "reset", "--hard", "--quiet", "HEAD~")

    trace = repo / "trace.json"
    previous_trace = os.environ.get("GIT_TRACE2_EVENT")
    os.environ["GIT_TRACE2_EVENT"] = str(trace)
    try:
        git_for_temporary_patch_application(repo, "am", "--3way", str(patch))
    finally:
        if previous_trace is None:
            del os.environ["GIT_TRACE2_EVENT"]
        else:
            os.environ["GIT_TRACE2_EVENT"] = previous_trace

    assert (repo / "fixture.txt").read_text() == "patched\n"
    assert not (repo / ".git" / "gc.log").exists()
    assert "maintenance run --auto" not in trace.read_text()


print("PASS west-patch-verify-contract")
