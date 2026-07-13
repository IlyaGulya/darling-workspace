"""Bounded process execution shared by ``west test`` runners.

CTest has a per-test timeout, but metadata runners launch commands directly.
This module gives every direct runner the same deadline and process-group
cleanup rule: a timed out parent must not leave its children behind.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


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


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _run_with_live_capture(
    process: subprocess.Popen,
    capture_streams: tuple,
    *,
    timeout_seconds: int,
    text: bool | None,
    heartbeat_seconds: float | None,
    heartbeat: Callable[[float], None] | None,
    output_line: Callable[[str, str], None],
) -> ProcessResult:
    """Capture output while forwarding complete lines to a diagnostic sink."""

    selector = selectors.DefaultSelector()
    registered = set()
    pending = {"stdout": b"", "stderr": b""}
    streams = {
        "stdout": (process.stdout, capture_streams[0]),
        "stderr": (process.stderr, capture_streams[1]),
    }
    for name, (stream, _) in streams.items():
        assert stream is not None
        selector.register(stream, selectors.EVENT_READ, name)
        registered.add(stream)

    started_at = time.monotonic()
    next_heartbeat = heartbeat_seconds or timeout_seconds
    try:
        while selector.get_map():
            elapsed = time.monotonic() - started_at
            remaining = timeout_seconds - elapsed
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(process.args), timeout_seconds)
            if process.poll() is not None:
                for name, data in pending.items():
                    if data:
                        output_line(name, data.decode(errors="replace"))
                        pending[name] = b""
                for stream in list(registered):
                    selector.unregister(stream)
                    registered.discard(stream)
                    stream.close()
                break
            wait_for = min(remaining, 0.25, max(0.0, next_heartbeat - elapsed))
            events = selector.select(wait_for)
            if not events:
                if heartbeat is not None:
                    heartbeat(time.monotonic() - started_at)
                    next_heartbeat += heartbeat_seconds or timeout_seconds
                continue

            for key, _ in events:
                name = key.data
                stream, capture = streams[name]
                assert stream is not None
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    if pending[name]:
                        output_line(name, pending[name].decode(errors="replace"))
                        pending[name] = b""
                    selector.unregister(stream)
                    registered.discard(stream)
                    stream.close()
                    continue
                capture.write(chunk)
                pending[name] += chunk
                lines = pending[name].split(b"\n")
                pending[name] = lines.pop()
                for line in lines:
                    output_line(name, line.decode(errors="replace"))

            if heartbeat is not None and time.monotonic() - started_at >= next_heartbeat:
                heartbeat(time.monotonic() - started_at)
                next_heartbeat += heartbeat_seconds or timeout_seconds

            # Do not wait for an escaped descendant to close inherited pipes after
            # the supervised command has already produced its exit status.
            if process.poll() is not None:
                for name, data in pending.items():
                    if data:
                        output_line(name, data.decode(errors="replace"))
                        pending[name] = b""
                for stream in list(registered):
                    selector.unregister(stream)
                    registered.discard(stream)
                    stream.close()
                break

        return ProcessResult(
            process.wait(),
            stdout=_read_capture(capture_streams[0], text=text),
            stderr=_read_capture(capture_streams[1], text=text),
        )
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        return ProcessResult(
            124,
            timed_out=True,
            stdout=_read_capture(capture_streams[0], text=text),
            stderr=_read_capture(capture_streams[1], text=text),
        )
    except BaseException:
        _kill_process_group(process)
        process.wait()
        raise
    finally:
        selector.close()


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
    heartbeat_seconds: float | None = None,
    heartbeat: Callable[[float], None] | None = None,
    output_line: Callable[[str, str], None] | None = None,
) -> ProcessResult:
    """Run ``args`` with a deadline and terminate its entire process group.

    ``subprocess.run(timeout=...)`` only kills the immediate child. Test
    scripts regularly start helpers, compilers, and Darling launchers, so each
    command gets a new session and a timeout kills the complete group.
    """

    if heartbeat is not None and (heartbeat_seconds is None or heartbeat_seconds <= 0):
        raise ValueError("heartbeat_seconds must be positive when heartbeat is set")
    if heartbeat is not None and input_data is not None:
        raise ValueError("heartbeat cannot be combined with input_data")
    if output_line is not None and not capture_output:
        raise ValueError("output_line requires capture_output")
    if output_line is not None and input_data is not None:
        raise ValueError("output_line cannot be combined with input_data")

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
        stdout=subprocess.PIPE if output_line is not None else stdout,
        stderr=subprocess.PIPE if output_line is not None else stderr,
        stdin=subprocess.PIPE if input_data is not None else None,
        text=False if output_line is not None else text,
        start_new_session=True,
    )
    try:
        if output_line is not None:
            assert capture_streams is not None
            return _run_with_live_capture(
                process,
                capture_streams,
                timeout_seconds=timeout_seconds,
                text=text,
                heartbeat_seconds=heartbeat_seconds,
                heartbeat=heartbeat,
                output_line=output_line,
            )
        if capture_output or input_data is not None:
            if heartbeat is None:
                process.communicate(
                    input=input_data,
                    timeout=timeout_seconds,
                )
            else:
                started_at = time.monotonic()
                deadline = started_at + timeout_seconds
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(list(args), timeout_seconds)
                    try:
                        process.communicate(timeout=min(remaining, heartbeat_seconds))
                        break
                    except subprocess.TimeoutExpired:
                        heartbeat(time.monotonic() - started_at)
            return ProcessResult(
                process.returncode,
                stdout=_read_capture(capture_streams[0], text=text) if capture_streams else "",
                stderr=_read_capture(capture_streams[1], text=text) if capture_streams else "",
            )
        if heartbeat is None:
            return ProcessResult(process.wait(timeout=timeout_seconds))
        started_at = time.monotonic()
        deadline = started_at + timeout_seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(list(args), timeout_seconds)
            try:
                return ProcessResult(process.wait(timeout=min(remaining, heartbeat_seconds)))
            except subprocess.TimeoutExpired:
                heartbeat(time.monotonic() - started_at)
    except subprocess.TimeoutExpired:
        _kill_process_group(process)
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
