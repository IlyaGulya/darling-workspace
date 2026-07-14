import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_worktrees import (
    path_has_west_temp_component,
    prunable_west_temp_worktrees,
    prune_stale_west_temp_worktrees,
    remove_temporary_worktree,
)


assert path_has_west_temp_component(Path("/tmp/west-red-proof-source-abcd/darling"))
assert path_has_west_temp_component(Path("/tmp/west-green-proof-source-abcd/darling"))
assert path_has_west_temp_component(Path("/tmp/west-profile-homebrew-abcd/darling"))
assert path_has_west_temp_component(Path("/tmp/.inflight-abcd/source/darling"))
assert path_has_west_temp_component(Path("/tmp/runtime-evidence-abcd/source/darling"))
assert not path_has_west_temp_component(Path("/tmp/west-manual-micro-bad"))


calls = []
repo_a = Path("/repo/a")
repo_b = Path("/repo/b")


def fake_runner(args, **kwargs):
    calls.append((tuple(args), kwargs))
    if args[:4] == ["git", "-C", str(repo_a), "worktree"]:
        if args[4:] == ["list", "--porcelain"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=(
                    "worktree /repo/a\n"
                    "HEAD 123\n"
                    "\n"
                    "worktree /tmp/west-red-proof-source-deadbeef/darling\n"
                    "HEAD 456\n"
                    "prunable gitdir file points to non-existent location\n"
                    "\n"
                    "worktree /tmp/west-red-proof-source-live/darling\n"
                    "HEAD 789\n"
                    "\n"
                    "worktree /tmp/manual-gone\n"
                    "HEAD abc\n"
                    "prunable gitdir file points to non-existent location\n"
                    "\n"
                ),
                stderr="",
            )
        if args[4:] == ["prune"]:
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
    if args[:4] == ["git", "-C", str(repo_b), "worktree"]:
        if args[4:] == ["list", "--porcelain"]:
            return subprocess.CompletedProcess(
                args,
                0,
                stdout="worktree /repo/b\nHEAD 123\n\n",
                stderr="",
            )
    raise AssertionError(f"unexpected command: {args}")


assert prunable_west_temp_worktrees(repo_a, runner=fake_runner) == [
    Path("/tmp/west-red-proof-source-deadbeef/darling")
]
calls.clear()
assert prune_stale_west_temp_worktrees([repo_a, repo_b], runner=fake_runner) == [
    Path("/tmp/west-red-proof-source-deadbeef/darling")
]
assert any(call[0] == ("git", "-C", str(repo_a), "worktree", "prune") for call in calls)
assert not any(call[0] == ("git", "-C", str(repo_b), "worktree", "prune") for call in calls)


def successful_remove(args, **kwargs):
    assert args == ["git", "worktree", "remove", "--force", "/tmp/west-profile-homebrew-ok/darling"]
    assert kwargs["cwd"] == repo_a
    assert kwargs["capture_output"] and kwargs["text"] and not kwargs["check"]
    return subprocess.CompletedProcess(args, 0, stdout="", stderr="")


assert remove_temporary_worktree(
    repo_a,
    Path("/tmp/west-profile-homebrew-ok/darling"),
    runner=successful_remove,
) is None


def failed_remove(args, **kwargs):
    del kwargs
    return subprocess.CompletedProcess(args, 128, stdout="", stderr="worktree is locked\n")


failure = remove_temporary_worktree(
    repo_a,
    Path("/tmp/west-profile-homebrew-failed/darling"),
    runner=failed_remove,
)
assert failure == (
    "/tmp/west-profile-homebrew-failed/darling "
    "(rc=128): worktree is locked"
), failure

print("PASS west-test-worktree-cleanup-contract")
