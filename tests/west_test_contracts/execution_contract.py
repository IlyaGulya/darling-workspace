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

result = run_bounded(
    [sys.executable, "-c", "print('BOUNDED_OUTPUT_OK')"],
    cwd=Path.cwd(),
    env=None,
    timeout_seconds=1,
    capture_output=True,
)
assert result.returncode == 0 and not result.timed_out, result
assert result.stdout == "BOUNDED_OUTPUT_OK\n", result

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
