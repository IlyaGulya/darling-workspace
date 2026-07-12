"""Contract for bounded source search and helper cleanup."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SEARCH = ROOT / "scripts/west-search.py"

with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    source = root / "source"
    source.mkdir()
    (source / "fixture.c").write_text("DOMAIN_MARKER\n")
    found = subprocess.run(
        [sys.executable, str(SEARCH), "DOMAIN_MARKER", str(source)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert found.returncode == 0, found
    assert "fixture.c:1:DOMAIN_MARKER" in found.stdout, found.stdout

    child_pid = root / "child.pid"
    fake_rg = root / "fake-rg"
    fake_rg.write_text(
        "#!/bin/sh\n"
        "sleep 60 &\n"
        f"echo $! > {child_pid}\n"
        "wait\n"
    )
    fake_rg.chmod(0o755)
    started = time.monotonic()
    timed = subprocess.run(
        [
            sys.executable,
            str(SEARCH),
            "ignored",
            str(source),
            "--timeout-seconds",
            "1",
            "--rg",
            str(fake_rg),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert timed.returncode == 124, timed
    assert "process group reaped" in timed.stderr, timed.stderr
    assert time.monotonic() - started < 5
    pid = int(child_pid.read_text())
    for _ in range(20):
        if not Path(f"/proc/{pid}").exists():
            break
        time.sleep(0.05)
    assert not Path(f"/proc/{pid}").exists(), f"search helper survived: {pid}"

print("PASS source-search-contract")
