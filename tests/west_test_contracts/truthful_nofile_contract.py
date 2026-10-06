#!/usr/bin/env python3
"""Contract for the truthful-no-raise RLIMIT_NOFILE architecture (dar-dar6x4-perf-5dq.34).

The architecture decision is that DarlingServer keeps the RLIMIT_NOFILE it inherited and never raises it, so
the inherited limit is authoritative for the server and every descendant.

Two layers, and each says which one it ran:

  AUDIT    always runnable, no prefix needed. The server must not raise the limit (no setrlimit on
           RLIMIT_NOFILE, no /proc/sys/fs/nr_open read), the requirement it checks must be DERIVED from
           measured role peaks rather than pasted from the observed boot wall, and the launcher must be able
           to notice a dead server (a reaped child, not kill(pid, 0), which succeeds for a zombie).

  RUNTIME  runs only when DW_NOFILE_PREFIX is set, because it needs a real bootable prefix. It proves the
           behavior the audit can only read:
             - a supported limit boots and the guest runs;
             - every prefix-owned process, including the guest command, holds EXACTLY the inherited value
               (this is the measurement that distinguishes a raised server from a truthful one, and it is
               why the limit is lowered rather than assumed);
             - an insufficient limit gives one deterministic diagnostic (inherited value, required value,
               reason), a normal non-zero exit, no signal, no guest process, and fails fast instead of
               waiting out the shellspawn timeout.

  MATRIX   the FD-slope matrix is measured separately (scripts/darling-fd-slope.sh) because it needs the
           workload asset staged inside the prefix; this contract does not stand in for it.

Exits 0 when every layer it ran passed, non-zero otherwise.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

FAILURES: list[str] = []
RAN: list[str] = []

SUPPORTED_LIMIT = 256      # above the derived requirement, and low enough that a raise would be visible
INSUFFICIENT_LIMIT = 32    # below the derived requirement
GUEST_MARKER = "NOFILE-CONTRACT-GUEST-RAN"


def fail(message: str) -> None:
    FAILURES.append(message)
    print(f"FAIL {message}")


def ok(message: str) -> None:
    print(f"ok   {message}")


def skip(message: str) -> None:
    print(f"SKIP {message}")


def _strip_comments(text: str) -> str:
    """Drop C/C++ comments so an audit matches CODE, not prose that explains what was removed."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def source_tree() -> Path | None:
    """The West topdir is the manifest repository's parent."""
    manifest_root = Path(__file__).resolve().parents[2]
    darling = manifest_root.parent / "darling"
    return darling if (darling / "src").is_dir() else None


def audit_layer() -> None:
    darling = source_tree()
    if darling is None:
        skip("audit: no darling source tree beside the manifest repository")
        return
    RAN.append("audit")

    server = darling / "src/external/darlingserver/src/darlingserver.cpp"
    launcher = darling / "src/startup/darling.c"
    for path in (server, launcher):
        if not path.is_file():
            fail(f"audit: {path} is missing")
            return
    server_text = _strip_comments(server.read_text())
    launcher_text = _strip_comments(launcher.read_text())

    # 1. The server must not raise the limit, by any route.
    if re.search(r"setrlimit\s*\(\s*RLIMIT_NOFILE", server_text):
        fail("audit: darlingserver still calls setrlimit(RLIMIT_NOFILE)")
    else:
        ok("audit: darlingserver never calls setrlimit(RLIMIT_NOFILE)")
    if "nr_open" in server_text:
        fail("audit: darlingserver still reads /proc/sys/fs/nr_open")
    else:
        ok("audit: darlingserver does not read /proc/sys/fs/nr_open")

    # 2. The requirement must be derived from measured role peaks, not the observed boot wall.
    if "kMinimumInheritedNofile" not in server_text:
        fail("audit: the server has no inherited-limit requirement constant")
    elif "kLargestRoleDescriptorPeak" not in server_text:
        fail("audit: the requirement constant is not derived from a role-peak constant")
    else:
        ok("audit: the requirement is derived from a role-peak constant")
    if re.search(r"kMinimumInheritedNofile\s*=\s*(59|64)\s*;", server_text):
        fail("audit: the requirement is a pasted literal instead of a derivation")
    else:
        ok("audit: the requirement is not a pasted literal")

    # 3. The launcher must be able to see a dead server: kill(pid, 0) succeeds for a zombie.
    if "waitpid" not in launcher_text:
        fail("audit: the launcher liveness check does not reap, so a dead server looks alive")
    else:
        ok("audit: the launcher reaps the server child before judging liveness")
    if "shellspawn did not become ready" not in launcher_text:
        fail("audit: the launcher readiness message changed; review this contract")
    else:
        ok("audit: the launcher still reports a shellspawn readiness timeout (unchanged path)")


def _watch_limits(prefix: Path, stop: threading.Event, seen: dict[str, set[int]]) -> None:
    """Record every soft RLIMIT_NOFILE observed in a prefix-owned process, per role."""
    prefix_text = str(prefix)
    while not stop.is_set():
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                exe = os.readlink(entry / "exe")
            except OSError:
                continue
            if not exe.startswith(prefix_text):
                continue
            role = Path(exe).name
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                cmdline = ""
            if "ring_mach_msg_test" in cmdline or "/bin/bash" in cmdline or "darling" == role:
                role = role if role != "mldr" else "guest"
            try:
                limits_text = (entry / "limits").read_text()
            except OSError:
                continue
            match = re.search(r"Max open files\s+(\d+)\s+(\d+)", limits_text)
            if match:
                seen.setdefault(role, set()).add(int(match.group(1)))
        stop.wait(0.1)


def run_guest(prefix: Path, limit: int, timeout: float) -> tuple[int, str, float, dict[str, set[int]]]:
    """Boot through the launcher with the inherited `limit`, and witness the limits it propagates.

    The limit is lowered by a wrapper shell before exec, so it applies to the launcher -- and therefore to
    everything the launcher spawns -- without touching this contract's own limit. A shell wrapper is used
    deliberately instead of preexec_fn: this process has a sampling thread running, and Python documents
    preexec_fn as deadlock-prone in the presence of threads (measured here as a 900s hang with no output).
    """
    cleanup = prefix.parent / "darling-workspace/scripts/prefix-cleanup.sh"
    if cleanup.is_file():
        subprocess.run(
            ["bash", str(cleanup), "--prefix", str(prefix), "--settle", "2"],
            capture_output=True, check=False, timeout=180,
        )
    env = dict(os.environ)
    env.update({
        "DPREFIX": str(prefix),
        "DARLING_PREFIX": str(prefix),
        "DARLING_LAUNCHER": str(prefix / "bin/darling"),
        "DARLING_ROOTLESS": "1",
        "DARLING_NOOVERLAYFS": "1",
        "DARLING_EUNION": "1",
        "DARLING_ROOTLESS_SHELLSPAWN_READY_TIMEOUT_MS": "30000",
        "DARLING_SERVER_MODE": "balanced",
    })

    seen: dict[str, set[int]] = {}
    stop = threading.Event()
    watcher = threading.Thread(target=_watch_limits, args=(prefix, stop, seen), daemon=True)
    watcher.start()

    started = time.monotonic()
    proc = subprocess.Popen(
        [
            "/bin/sh", "-c",
            f'ulimit -S -n {limit} 2>/dev/null || exit 99\nexec "$0" shell /bin/bash --login -c "$1"',
            str(prefix / "bin/darling"),
            f"echo {GUEST_MARKER}",
        ],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
        rc = 124
    elapsed = time.monotonic() - started
    stop.set()
    watcher.join(timeout=5)
    return rc, (out or b"").decode("utf-8", "replace"), elapsed, seen


def runtime_layer() -> None:
    prefix_env = os.environ.get("DW_NOFILE_PREFIX")
    if not prefix_env:
        skip("runtime: set DW_NOFILE_PREFIX to run the behavior layer")
        return
    prefix = Path(prefix_env).resolve()
    if not (prefix / "bin/darling").is_file():
        fail(f"runtime: {prefix}/bin/darling is missing; bootstrap the prefix first")
        return
    RAN.append("runtime")

    # Supported limit: the guest must run, and every process must hold EXACTLY the inherited value.
    rc, out, elapsed, seen = run_guest(prefix, SUPPORTED_LIMIT, 300.0)
    if rc != 0 or GUEST_MARKER not in out:
        fail(f"runtime: supported-limit boot failed (rc={rc}, {elapsed:.1f}s)")
    else:
        ok(f"runtime: supported-limit boot runs the guest (rc=0, {elapsed:.1f}s)")
    if not seen:
        fail("runtime: the witness observed no prefix-owned process; the measurement proves nothing")
    else:
        raised = {role: sorted(v) for role, v in seen.items() if v != {SUPPORTED_LIMIT}}
        if raised:
            fail(f"runtime: some prefix-owned process did not hold the inherited limit: {raised}")
        else:
            roles = ", ".join(f"{role}={sorted(v)[0]}" for role, v in sorted(seen.items()))
            ok(f"runtime: every observed process held the inherited limit ({roles})")

    # Insufficient limit: one diagnostic, non-zero exit, no signal, no guest, and fast.
    rc, out, elapsed, _ = run_guest(prefix, INSUFFICIENT_LIMIT, 300.0)
    if rc == 0:
        fail("runtime: insufficient limit still booted")
    elif rc in (-signal.SIGILL, -signal.SIGSEGV, 132, 139):
        fail(f"runtime: insufficient limit died through a signal (rc={rc})")
    else:
        ok(f"runtime: insufficient limit exits normally with rc={rc}")
    for needle, why in (
        ("Insufficient RLIMIT_NOFILE", "prints the refusal"),
        (f"the inherited soft limit is {INSUFFICIENT_LIMIT}", "names the inherited soft limit"),
        ("the bootstrap closure needs", "names the required value"),
        ("largest role peak", "names the reason"),
    ):
        if needle not in out:
            fail(f"runtime: the diagnostic does not {why}")
        else:
            ok(f"runtime: the diagnostic {why}")
    if GUEST_MARKER in out:
        fail("runtime: a guest command ran despite the refusal")
    else:
        ok("runtime: no guest process ran")
    if elapsed >= 25.0:
        fail(f"runtime: the refusal took {elapsed:.1f}s, i.e. it waited out the shellspawn timeout")
    else:
        ok(f"runtime: the refusal surfaces promptly ({elapsed:.1f}s)")


def main() -> int:
    audit_layer()
    runtime_layer()
    print(f"TRUTHFUL-NOFILE-CONTRACT layers={','.join(RAN) or 'none'} failures={len(FAILURES)}")
    if not RAN and not FAILURES:
        print("TRUTHFUL-NOFILE-CONTRACT nothing was executed")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
