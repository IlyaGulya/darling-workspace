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
    """Collect a capture file WITHOUT touching the file position it shares.

    The child has the same open file description, so `seek(0)+read()` would move
    the offset the writer uses -- MEASURED on this host: TemporaryFile with "abcdefgh",
    a dup of the writer, the writer lseek'd to 2, then this function, and the
    writer's offset came back 8 instead of 2. `pread` reads at an absolute offset
    and leaves that shared position exactly where the child left it.
    """

    descriptor = stream.fileno()
    size = os.fstat(descriptor).st_size
    chunks = []
    offset = 0
    while offset < size:
        chunk = os.pread(descriptor, min(65536, size - offset), offset)
        if not chunk:
            break
        chunks.append(chunk)
        offset += len(chunk)
    value = b"".join(chunks)
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
    """Forward live output from FILE-BACKED captures, never from a pipe.

    A supervised command can leave a descendant behind, and that descendant
    inherits whatever the leader was given for stdout/stderr. With pipes, the
    reader has to be closed when the leader exits -- and then the descendant's
    next write is EPIPE/SIGPIPE. MEASURED on this host with a real descendant:
    leader rc 0, descendant killed by signal 13, on the output_line path only.

    A file has no reader to close. The descendant keeps writing into a file
    nobody is reading any more, which is harmless, and ``pread`` reads new bytes
    by offset without touching a file position the child also holds. The
    deadline and the heartbeat are unchanged, and the call still returns as soon
    as the leader has produced its status.
    """

    streams = (("stdout", capture_streams[0]), ("stderr", capture_streams[1]))
    offsets = {"stdout": 0, "stderr": 0}
    pending = {"stdout": b"", "stderr": b""}
    descriptors = {name: stream.fileno() for name, stream in streams}

    def pump(final: bool) -> None:
        """Forward at most one fixed budget, so the caller gets control back.

        A capture is a live file that a descendant may keep growing at any rate.
        A drain bounded only by the size seen at entry still never returns: one
        call can copy gigabytes and the deadline lives in the loop that cannot
        run. MEASURED (this exact edge test, before the budget): one pump kept a
        7.6 GiB unlinked capture growing while the caller never reached its own
        timeout. The budget below is per call; the loop re-checks the deadline
        between calls. A read error is a COLLECTION failure -- re-raised, never
        reported as "this stream had no output" inside a successful result.
        """

        budget = 256 * 1024
        for name, _ in streams:
            try:
                limit = os.fstat(descriptors[name]).st_size
            except OSError:
                raise
            while budget > 0 and offsets[name] < limit:
                chunk = os.pread(
                    descriptors[name],
                    min(65536, limit - offsets[name], budget),
                    offsets[name],
                )
                if not chunk:
                    break
                budget -= len(chunk)
                offsets[name] += len(chunk)
                pending[name] += chunk
                lines = pending[name].split(b"\n")
                pending[name] = lines.pop()
                for line in lines:
                    output_line(name, line.decode(errors="replace"))
                # A writer that never emits a newline must not make this buffer grow
                # without bound (each split of it is linear in its size).
                if len(pending[name]) >= 65536:
                    output_line(name, pending[name].decode(errors="replace"))
                    pending[name] = b""
        if final:
            for name in pending:
                if pending[name]:
                    output_line(name, pending[name].decode(errors="replace"))
                    pending[name] = b""

    started_at = time.monotonic()
    next_heartbeat = heartbeat_seconds or timeout_seconds
    while True:
        elapsed = time.monotonic() - started_at
        remaining = timeout_seconds - elapsed
        if remaining <= 0:
            raise subprocess.TimeoutExpired(list(process.args), timeout_seconds)
        if process.poll() is not None:
            # Prompt return: the leader is done. One last pump takes what it wrote;
            # anything a descendant writes later stays in the file.
            pump(final=True)
            break
        time.sleep(min(remaining, 0.25))
        pump(final=False)
        elapsed = time.monotonic() - started_at
        if elapsed >= timeout_seconds:
            # A pump can consume real time on a large capture; the deadline wins.
            raise subprocess.TimeoutExpired(list(process.args), timeout_seconds)
        if heartbeat is not None and elapsed >= next_heartbeat:
            heartbeat(elapsed)
            next_heartbeat += heartbeat_seconds or timeout_seconds

    return ProcessResult(
        process.wait(),
        stdout=_read_capture(capture_streams[0], text=text),
        stderr=_read_capture(capture_streams[1], text=text),
    )


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
    pass_fds: Sequence[int] = (),
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
    # Lease capabilities belong only to explicitly selected controllers. A
    # launcher/compiler/arbitrary helper must not inherit a marker after
    # subprocess has closed its backing descriptor.
    selected_env = os.environ if env is None else env
    lease_marker = selected_env.get("WEST_TEST_PREFIX_LEASE")
    lease_fd_value = selected_env.get("WEST_TEST_PREFIX_LEASE_FD", "8")
    carries_lease = (
        bool(lease_marker) and lease_fd_value.isdecimal()
        and int(lease_fd_value) in pass_fds
    )
    if not carries_lease and (
        "WEST_TEST_PREFIX_LEASE" in selected_env
        or "WEST_TEST_PREFIX_LEASE_FD" in selected_env
    ):
        env = dict(selected_env)
        env.pop("WEST_TEST_PREFIX_LEASE", None)
        env.pop("WEST_TEST_PREFIX_LEASE_FD", None)

    capture_streams = None
    if capture_output:
        if stdout is not None or stderr is not None:
            raise ValueError("capture_output conflicts with stdout/stderr")
        if text is None:
            text = True
        capture_streams = (tempfile.TemporaryFile(), tempfile.TemporaryFile())
        stdout, stderr = capture_streams

    if output_line is not None:
        # FILE-BACKED, not piped: the descriptor a surviving descendant inherits
        # must not have a reader this process can close (see _run_with_live_capture).
        assert capture_streams is not None
        child_stdout, child_stderr = capture_streams
    else:
        child_stdout, child_stderr = stdout, stderr
    process = subprocess.Popen(
        list(args),
        cwd=cwd,
        env=env,
        stdout=child_stdout,
        stderr=child_stderr,
        stdin=subprocess.PIPE if input_data is not None else None,
        text=False if output_line is not None else text,
        start_new_session=True,
        pass_fds=pass_fds,
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
    except KeyboardInterrupt:
        # A guarded child owns ordered resource cleanup. Give it the signal
        # before unwinding the caller's prefix context or escalating to SIGKILL.
        process.send_signal(signal.SIGINT)
        try:
            # Match the guarded runner's outer cleanup allowance: shutdown can
            # take five seconds, followed by a five-second managed-child wait.
            process.communicate(timeout=15)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            process.communicate()
        raise
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
