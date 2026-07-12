"""Bounded process execution shared by ``west test`` runners.

CTest has a per-test timeout, but metadata runners launch commands directly.
This module gives every direct runner the same deadline and process-group
cleanup rule: a timed out parent must not leave its children behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
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


def process_output_text(result: ProcessResult) -> str:
    """Return captured process output as readable text, including timeout bytes."""

    def decode(stream: str | bytes) -> str:
        return stream.decode(errors="replace") if isinstance(stream, bytes) else stream

    return decode(result.stdout) + decode(result.stderr)


def _read_capture(stream, *, text: bool | None) -> str | bytes:
    stream.flush()
    stream.seek(0)
    value = stream.read()
    return value.decode(errors="replace") if text is not False else value


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

    capture_streams = None
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("capture_output conflicts with stdout/stderr")
        if text is None:
            text = True
        capture_streams = (tempfile.TemporaryFile(), tempfile.TemporaryFile())
        stdout, stderr = capture_streams

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
            process.communicate(
                input=input_data,
                timeout=timeout_seconds,
            )
            return ProcessResult(
                process.returncode,
                stdout=_read_capture(capture_streams[0], text=text) if capture_streams else "",
                stderr=_read_capture(capture_streams[1], text=text) if capture_streams else "",
            )
        return ProcessResult(process.wait(timeout=timeout_seconds))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if capture_output or input_data is not None:
            try:
                process.communicate(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return ProcessResult(
                124,
                timed_out=True,
                stdout=_read_capture(capture_streams[0], text=text) if capture_streams else "",
                stderr=_read_capture(capture_streams[1], text=text) if capture_streams else "",
            )
        process.wait()
        return ProcessResult(124, timed_out=True)
    finally:
        if capture_streams is not None:
            capture_streams[0].close()
            capture_streams[1].close()
