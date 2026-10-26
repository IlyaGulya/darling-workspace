#!/usr/bin/env python3
"""Measure inherited NOFILE limits in a real installed Darling, without source-text assertions.

Requires DARLING_RUNTIME_ROOT and an explicitly owned, prepared DW_NOFILE_PREFIX.
No selection is a formal SKIP, not host/runtime coverage. The FD-slope matrix is separate.
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import nullcontext
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from west_commands.runtime_install import resolve_runtime
from west_commands.test_prefix import prefix_lease, rootless_prefix_process_snapshot

SUPPORTED_LIMIT = 256
INSUFFICIENT_LIMIT = 32
GUEST_MARKER = "NOFILE-CONTRACT-GUEST-RAN"


def _watch_limits(prefix: Path, stop: threading.Event, seen: dict[str, set[int]]) -> None:
    while not stop.is_set():
        for row in rootless_prefix_process_snapshot(prefix):
            process = Path("/proc") / row.split(" ", 1)[0]
            try:
                role = Path(os.readlink(process / "exe").removesuffix(" (deleted)")).name
                limits = (process / "limits").read_text()
            except OSError:
                continue
            if role == "mldr":
                role = "guest"
            match = re.search(r"Max open files\s+(\d+)\s+(\d+)", limits)
            if match:
                seen.setdefault(role, set()).add(int(match.group(1)))
        stop.wait(0.1)


def _cleanup(runtime, prefix: str, environment: dict[str, str]) -> int:
    result = subprocess.run(
        [str(ROOT / "scripts/prefix-cleanup.sh"), "--runtime-root", str(runtime.root),
         "--prefix", prefix, "--settle", "1"],
        env=environment, capture_output=True, text=True, timeout=90, check=False,
    )
    print(result.stdout, end="")
    print(result.stderr, end="", file=sys.stderr)
    return result.returncode


def run_guest(runtime, prefix: str, limit: int) -> tuple[int, str, float, dict[str, set[int]], int]:
    environment = runtime.environment(prefix)
    environment["WEST_TEST_PREFIX_LEASE"] = prefix
    if _cleanup(runtime, prefix, environment):
        raise RuntimeError("initial prefix cleanup failed; refusing to launch")
    seen: dict[str, set[int]] = {}
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_limits, args=(Path(prefix), stop, seen), daemon=True)
    state = Path(os.environ.get("DW_STATE_ROOT", str(ROOT.parent / ".west-state"))) / "contracts/nofile"
    state.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=f"limit-{limit}-", suffix=".log", dir=state)
    output_path = Path(name)
    started = time.monotonic()
    cleanup_rc = 1
    watcher.start()
    try:
        with os.fdopen(descriptor, "wb") as sink:
            process = subprocess.Popen(
                ["/bin/sh", "-c",
                 f'ulimit -S -n {limit} || exit 99\nexec "$0" shell /bin/bash --login -c "$1"',
                 str(runtime.launcher), f"set -e; sleep 2; printf '%s\\n' {GUEST_MARKER}"],
                env=environment, stdout=sink, stderr=subprocess.STDOUT,
            )
            try:
                rc = process.wait(timeout=180)
            except subprocess.TimeoutExpired:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                rc = 124
    finally:
        stop.set()
        watcher.join(timeout=5)
        cleanup_rc = _cleanup(runtime, prefix, environment)
    elapsed = time.monotonic() - started
    output = output_path.read_text(errors="replace")
    print(output, end="" if output.endswith("\n") else "\n")
    if cleanup_rc == 0:
        output_path.unlink()
    else:
        print(f"retained failed-cleanup output: {output_path}", file=sys.stderr)
    return rc, output, elapsed, seen, cleanup_rc


def main() -> int:
    prefix = os.environ.get("DW_NOFILE_PREFIX")
    if not prefix:
        print("SKIP truthful NOFILE runtime: select DARLING_RUNTIME_ROOT and owned DW_NOFILE_PREFIX; no coverage executed")
        return 77
    try:
        runtime = resolve_runtime()
        runtime.environment(prefix)
        borrowed = os.environ.get("WEST_TEST_PREFIX_LEASE")
        if borrowed and borrowed != prefix:
            raise ValueError("borrowed prefix lease does not match DW_NOFILE_PREFIX")
        lease = nullcontext() if borrowed else prefix_lease(prefix)
        with lease:
            rc, output, elapsed, seen, cleanup_rc = run_guest(runtime, prefix, SUPPORTED_LIMIT)
            if rc or GUEST_MARKER not in output or cleanup_rc:
                print(f"FAIL supported inherited limit: rc={rc} cleanup_rc={cleanup_rc} elapsed={elapsed:.1f}s")
                return 1
            missing = {"darlingserver", "guest"} - seen.keys()
            changed = {role: sorted(values) for role, values in seen.items() if values != {SUPPORTED_LIMIT}}
            if missing or changed:
                print(f"FAIL inherited-limit measurement: missing_roles={sorted(missing)} differing_limits={changed}")
                return 1
            print(f"PASS inherited limit {SUPPORTED_LIMIT}: observed={seen}")
            rc, output, elapsed, _, cleanup_rc = run_guest(runtime, prefix, INSUFFICIENT_LIMIT)
            if rc == 0 or rc < 0 or 128 <= rc <= 192 or GUEST_MARKER in output or elapsed >= 25 or cleanup_rc:
                print(f"FAIL insufficient inherited limit: rc={rc} cleanup_rc={cleanup_rc} elapsed={elapsed:.1f}s")
                return 1
            print(f"PASS insufficient limit {INSUFFICIENT_LIMIT}: normal nonzero rc={rc}, no guest marker, elapsed={elapsed:.1f}s")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        print(f"FAIL truthful NOFILE runtime: {error}", file=sys.stderr)
        return 1
    print("TRUTHFUL-NOFILE-CONTRACT RUNTIME PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
