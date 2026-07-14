"""Temporary worktree cleanup helpers for ``west test``."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable, Iterable


WEST_TEMP_WORKTREE_PREFIXES = (
    "west-red-proof-source-",
    "west-green-proof-source-",
    "west-profile-",
    # Runtime evidence owns disposable source worktrees too. Their parent
    # directory is retained only while the evidence unit is live/inspectable.
    ".inflight-",
    "runtime-evidence-",
)


Runner = Callable[..., subprocess.CompletedProcess]


def path_has_west_temp_component(path: Path) -> bool:
    return any(
        part.startswith(prefix)
        for part in path.parts
        for prefix in WEST_TEMP_WORKTREE_PREFIXES
    )


def prunable_west_temp_worktrees(repo: Path, *, runner: Runner = subprocess.run) -> list[Path]:
    result = runner(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        return []

    paths: list[Path] = []
    current: Path | None = None
    prunable = False
    for line in [*result.stdout.splitlines(), ""]:
        if not line:
            if current is not None and prunable and path_has_west_temp_component(current):
                paths.append(current)
            current = None
            prunable = False
            continue
        if line.startswith("worktree "):
            current = Path(line.removeprefix("worktree "))
        elif line == "prunable" or line.startswith("prunable "):
            prunable = True
    return paths


def prune_stale_west_temp_worktrees(
    repos: Iterable[Path],
    *,
    runner: Runner = subprocess.run,
) -> list[Path]:
    pruned: list[Path] = []
    for repo in repos:
        stale = prunable_west_temp_worktrees(Path(repo), runner=runner)
        if not stale:
            continue
        runner(
            ["git", "-C", str(repo), "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        pruned.extend(stale)
    return pruned


def remove_temporary_worktree(
    repo: Path,
    target: Path,
    *,
    runner: Runner = subprocess.run,
) -> str | None:
    """Remove one disposable worktree or return its actionable Git diagnostic."""

    result = runner(
        ["git", "worktree", "remove", "--force", str(target)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0:
        return None
    detail = (result.stdout + result.stderr).strip()
    return f"{target} (rc={result.returncode}){': ' + detail if detail else ''}"
