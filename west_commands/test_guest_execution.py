"""Shared bounded execution for commands running inside a Darling prefix."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from shlex import quote
from typing import Callable

try:
    from .test_execution import ProcessResult, run_bounded
except ImportError:  # Loaded as a West extension module, not a package.
    from test_execution import ProcessResult, run_bounded


@dataclass(frozen=True)
class GuestExecution:
    """Resolved host inputs required to launch one Darling guest command."""

    prefix: str
    launcher: str


def resolve_guest_execution(
    *,
    name: str,
    env: dict[str, str],
    fallback_prefix: str | None,
    resolve_launcher: Callable[[str | None], str | None],
    die: Callable[[str], None],
) -> GuestExecution:
    """Resolve the prefix and launcher shared by metadata guest runners."""

    prefix = env.get("DPREFIX") or fallback_prefix
    if not prefix:
        die(f"{name}: guest fixture needs DPREFIX")
    launcher = env.get("DARLING_LAUNCHER") or env.get("DARLING") or resolve_launcher(prefix)
    if not launcher:
        die(f"{name}: guest fixture needs a Darling launcher")
    return GuestExecution(prefix=prefix, launcher=launcher)


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


def run_guest_command_fixture(
    invocation: dict,
    *,
    env: dict[str, str],
    prefix: str | None,
    resolve_launcher: Callable[[str | None], str | None],
    die: Callable[[str], None],
    err: Callable[[str], None],
    record_failure_phase: Callable[[dict, str], None],
) -> int:
    """Run a normalized guest command and validate its observable contract."""

    guest = resolve_guest_execution(
        name=invocation["name"],
        env=env,
        fallback_prefix=prefix,
        resolve_launcher=resolve_launcher,
        die=die,
    )

    guest_env_setup = "\n".join(
        f"export {key}={quote(value)}"
        for key, value in invocation.get("guest_env_vars", {}).items()
    ) or ":"
    guest_script = f"""set -u
{guest_env_setup}
{invocation["guest_command"]}
"""
    timeout_seconds = int(invocation.get("timeout_seconds", 600))
    with tempfile.TemporaryDirectory(prefix=f"west-guest-command-{invocation['name']}-") as temp:
        tempdir = Path(temp)
        stdout_path = tempdir / "stdout.log"
        stderr_path = tempdir / "stderr.log"
        with stdout_path.open("w", encoding="utf-8") as stdout_file, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_file:
            result = run_guest_shell(
                guest.launcher,
                guest.prefix,
                guest_script,
                cwd=Path(invocation["cwd"]),
                env=env,
                timeout_seconds=timeout_seconds,
                stdout=stdout_file,
                stderr=stderr_file,
            )
            stdout_file.flush()
            stderr_file.flush()

        output = stdout_path.read_text(errors="replace") + stderr_path.read_text(
            errors="replace"
        )
    if output:
        print(output, end="" if output.endswith("\n") else "\n")
    expect = invocation.get("expect") or {}
    if result.timed_out:
        if expect.get("returncode") == "timeout":
            for needle in expect.get("output-contains", []):
                if str(needle) not in output:
                    err(f"{invocation['name']}: output missing {needle!r}")
                    return 1
            return 0
        err(
            f"{invocation['name']}: guest command watchdog timed out after "
            f"{timeout_seconds}s"
        )
        record_failure_phase(invocation, "run")
        return result.returncode

    returncode = result.returncode
    rc_mode = expect.get("returncode", 0)
    if rc_mode == "timeout":
        err(f"{invocation['name']}: guest command returned before expected timeout")
        record_failure_phase(invocation, "run")
        return 1
    if rc_mode == "nonzero" and returncode == 0:
        err(f"{invocation['name']}: guest command succeeded unexpectedly")
        record_failure_phase(invocation, "run")
        return 1
    if rc_mode != "any" and rc_mode != "nonzero" and returncode != int(rc_mode):
        err(f"{invocation['name']}: guest command rc {returncode}, want {rc_mode}")
        record_failure_phase(invocation, "run")
        return 1
    for needle in expect.get("output-contains", []):
        if str(needle) not in output:
            err(f"{invocation['name']}: output missing {needle!r}")
            record_failure_phase(invocation, "run")
            return 1
    for needle in expect.get("output-lacks", []):
        if str(needle) in output:
            err(f"{invocation['name']}: output unexpectedly contains {needle!r}")
            record_failure_phase(invocation, "run")
            return 1
    return 0
