"""Git operations shared by persistent and disposable patch workflows."""

from __future__ import annotations

import subprocess
from pathlib import Path


# Patch application may create loose objects. Keep Git housekeeping explicit so
# it cannot add gc.log residue or obscure an applicability failure.
PATCH_APPLICATION_GIT_OPTIONS = (
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)

# Disposable worktrees may run on a clean CI runner with no user config. This
# identity is intentionally limited to commits created in those worktrees.
TEMPORARY_PATCH_GIT_OPTIONS = (
    *PATCH_APPLICATION_GIT_OPTIONS,
    "-c",
    "user.name=West Test",
    "-c",
    "user.email=west-test@example.invalid",
)


def run(
    repo: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    result = subprocess.run(
        args,
        cwd=repo,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        env=env,
    )
    return result.stdout.strip() if capture else ""


def git(
    repo: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    return run(repo, "git", *args, capture=capture, check=check, env=env)


def git_for_patch_application(
    repo: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
) -> str:
    """Run a patch application without background Git maintenance."""
    return git(
        repo,
        *PATCH_APPLICATION_GIT_OPTIONS,
        *args,
        capture=capture,
        check=check,
    )


def git_for_temporary_patch_application(
    repo: Path,
    *args: str,
    capture: bool = False,
    check: bool = True,
) -> str:
    """Apply a patch in a disposable worktree with deterministic identity."""
    return git(
        repo,
        *TEMPORARY_PATCH_GIT_OPTIONS,
        *args,
        capture=capture,
        check=check,
    )
