"""Focused contracts for bounded metadata-runner process execution."""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_execution import run_bounded


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    child_pid = tempdir / "child.pid"
    code = (
        "import pathlib, subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    result = run_bounded(
        [sys.executable, "-c", code, str(child_pid)],
        cwd=tempdir,
        env=None,
        timeout_seconds=1,
    )
    assert result.timed_out, result
    assert result.returncode == 124, result
    pid = int(child_pid.read_text())
    for _ in range(20):
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists(), f"timed out child survived: {pid}"


with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    escaped_pid = tempdir / "escaped.pid"
    code = (
        "import os, pathlib, sys, time; "
        "pid = os.fork(); "
        "exec('os.setsid(); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)') "
        "if pid == 0 else time.sleep(60)"
    )
    started = time.monotonic()
    result = run_bounded(
        [sys.executable, "-c", code, str(escaped_pid)],
        cwd=tempdir,
        env=None,
        timeout_seconds=1,
        capture_output=True,
    )
    assert result.timed_out and result.returncode == 124, result
    assert time.monotonic() - started < 5, "escaped stdout pipe made timeout unbounded"
    pid = int(escaped_pid.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(pid, 9)

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    escaped_pid = tempdir / "successful-escaped.pid"
    code = (
        "import os, pathlib, sys, time; "
        "pid = os.fork(); "
        "exec('os.setsid(); pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
        "print(\\\"child inherited output\\\", flush=True); time.sleep(60)') "
        "if pid == 0 else print('PARENT_OK', flush=True)"
    )
    started = time.monotonic()
    result = run_bounded(
        [sys.executable, "-c", code, str(escaped_pid)],
        cwd=tempdir,
        env=None,
        timeout_seconds=5,
        capture_output=True,
    )
    assert result.returncode == 0 and not result.timed_out, result
    assert time.monotonic() - started < 2, "escaped output fd delayed successful parent completion"
    assert "PARENT_OK" in result.stdout, result
    pid = int(escaped_pid.read_text())
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    else:
        os.kill(pid, 9)

result = run_bounded(
    [sys.executable, "-c", "print('BOUNDED_OUTPUT_OK')"],
    cwd=Path.cwd(),
    env=None,
    timeout_seconds=1,
    capture_output=True,
)
assert result.returncode == 0 and not result.timed_out, result
assert result.stdout == "BOUNDED_OUTPUT_OK\n", result

with tempfile.TemporaryDirectory() as temp:
    tempdir = Path(temp)
    seen = tempdir / "live-line-seen"
    code = (
        "import pathlib, time\n"
        "print('LIVE_BUILD_LINE', flush=True)\n"
        "deadline = time.monotonic() + 1\n"
        f"while not pathlib.Path({str(seen)!r}).exists():\n"
        "    assert time.monotonic() < deadline\n"
        "    time.sleep(0.01)\n"
        "print('BUILD_DONE', flush=True)\n"
    )
    lines = []

    def forward_line(stream, line):
        lines.append((stream, line))
        if line == "LIVE_BUILD_LINE":
            seen.touch()

    result = run_bounded(
        [sys.executable, "-c", code],
        cwd=tempdir,
        env=None,
        timeout_seconds=2,
        capture_output=True,
        output_line=forward_line,
    )
    assert result.returncode == 0 and not result.timed_out, result
    assert ("stdout", "LIVE_BUILD_LINE") in lines, lines
    assert ("stdout", "BUILD_DONE") in lines, lines
    assert result.stdout == "LIVE_BUILD_LINE\nBUILD_DONE\n", result

heartbeats = []
result = run_bounded(
    [sys.executable, "-c", "import time; time.sleep(0.15)"],
    cwd=Path.cwd(),
    env=None,
    timeout_seconds=1,
    heartbeat_seconds=0.03,
    heartbeat=heartbeats.append,
)
assert result.returncode == 0 and heartbeats, (result, heartbeats)

result = run_bounded(
    [
        sys.executable,
        "-c",
        "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()[::-1])",
    ],
    cwd=Path.cwd(),
    env=None,
    timeout_seconds=1,
    capture_output=True,
    text=False,
    input_data=b"guest-archive",
)
assert result.returncode == 0 and not result.timed_out, result
assert result.stdout == b"evihcra-tseug", result

print("PASS west-test-execution-contract")
