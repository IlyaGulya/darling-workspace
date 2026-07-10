"""Bounded process execution shared by ``west test`` runners.

CTest has a per-test timeout, but metadata runners launch commands directly.
This module gives every direct runner the same deadline and process-group
cleanup rule: a timed out parent must not leave its children behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


@dataclass(frozen=True)
class ProcessResult:
    """The observable result of one bounded command invocation."""

    returncode: int
    timed_out: bool = False
    stdout: str | bytes = ""
    stderr: str | bytes = ""


def run_bounded(
    args: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str] | None,
    timeout_seconds: int,
    stdout=None,
    stderr=None,
    text: bool | None = None,
    capture_output: bool = False,
    input_data: str | bytes | None = None,
) -> ProcessResult:
    """Run ``args`` with a deadline and terminate its entire process group.

    ``subprocess.run(timeout=...)`` only kills the immediate child. Test
    scripts regularly start helpers, compilers, and Darling launchers, so each
    command gets a new session and a timeout kills the complete group.
    """

    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("capture_output conflicts with stdout/stderr")
        stdout = subprocess.PIPE
        stderr = subprocess.PIPE
        if text is None:
            text = True

    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=env,
        stdout=stdout,
        stderr=stderr,
        stdin=subprocess.PIPE if input_data is not None else None,
        text=text,
        start_new_session=True,
    )
    try:
        if capture_output or input_data is not None:
            captured_stdout, captured_stderr = process.communicate(
                input=input_data,
                timeout=timeout_seconds,
            )
            return ProcessResult(
                process.returncode,
                stdout=captured_stdout or "",
                stderr=captured_stderr or "",
            )
        return ProcessResult(process.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if capture_output or input_data is not None:
            captured_stdout, captured_stderr = process.communicate()
            return ProcessResult(
                124,
                timed_out=True,
                stdout=captured_stdout or "",
                stderr=captured_stderr or "",
            )
        process.wait()
        return ProcessResult(124, timed_out=True)
