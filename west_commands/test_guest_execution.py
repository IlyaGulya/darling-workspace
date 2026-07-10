"""Shared bounded execution for commands running inside a Darling prefix."""

from __future__ import annotations

import subprocess
from pathlib import Path

from test_execution import ProcessResult, run_bounded


def run_guest_shell(
    launcher: str,
    prefix: str | Path,
    script: str,
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: int,
    stdout=None,
    stderr=None,
) -> ProcessResult:
    """Run one guest shell command with prefix identity and group cleanup."""

    prefix_text = str(prefix)
    return run_bounded(
        [
            "env",
            f"DPREFIX={prefix_text}",
            f"DARLING_PREFIX={prefix_text}",
            launcher,
            "shell",
            "/bin/bash",
            "--login",
            "-c",
            script,
        ],
        cwd=cwd,
        env=env,
        timeout_seconds=timeout_seconds,
        stdout=stdout,
        stderr=stderr,
        text=True,
    )


def shutdown_guest_prefix(
    launcher: str,
    prefix: str | Path,
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: int,
) -> ProcessResult:
    """Stop a prefix without allowing cleanup to stall the enclosing test run."""

    prefix_text = str(prefix)
    shutdown_env = dict(env or {})
    shutdown_env.update({"DPREFIX": prefix_text, "DARLING_PREFIX": prefix_text})
    return run_bounded(
        [launcher, "shutdown"],
        cwd=cwd,
        env=shutdown_env,
        timeout_seconds=timeout_seconds,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
