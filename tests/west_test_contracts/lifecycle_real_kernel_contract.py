"""Hermetic real-Linux lifecycle lane for dar-4ush.5.

This is a host/kernel contract, not a product-consumer route.  The six
accepted .4 traces are loaded and bound to every report.  Filesystem and
session fixtures run as the invoking user; only the cgroup-v2 fixture uses a
separate bounded sudo helper when the host does not delegate the root cgroup.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import os
import platform
import select
import signal
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
TRACE_ROOT = ROOT / "tests" / "fixtures" / "lifecycle-traces" / "v1"
TRACE_NAMES = (
    "shared-session-signal-gone",
    "shared-session-signal-rejected",
    "shared-session-retained-holder-timeout",
    "shared-session-pid-reuse",
    "shared-session-late-fork",
    "session-root-exit-before-snapshot",
)
MAX_OUTPUT = 64 * 1024
MAX_CYCLES = 3
MAX_CHURN = 8
PIDFD_POLL_TIMEOUT_MS = 5000
READINESS_TIMEOUT_S = 15.0
PROCESS_DRAIN_TIMEOUT_S = 5.0
MAX_LINE_BYTES = 4096
MAX_TRACE_CASES = len(TRACE_NAMES)
MAX_BASELINE_BYTES = 8 * 1024 * 1024

LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.syscall.restype = ctypes.c_long
MACHINE = platform.machine()
if MACHINE == "x86_64":
    SYS_RENAMEAT2 = 316
    SYS_PIDFD_SEND_SIGNAL = 424
elif MACHINE in {"aarch64", "riscv64"}:
    SYS_RENAMEAT2 = 276
    SYS_PIDFD_SEND_SIGNAL = 424
else:
    SYS_RENAMEAT2 = None
    SYS_PIDFD_SEND_SIGNAL = None
RENAME_NOREPLACE = 1
AT_FDCWD = -100


def fail(message: str) -> "NoReturn":
    raise AssertionError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def invoke_syscall(number: int, *args: Any) -> int:
    result = LIBC.syscall(number, *args)
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return int(result)


def renameat2(old_dir_fd: int, old: str, new_dir_fd: int, new: str, flags: int) -> None:
    require(SYS_RENAMEAT2 is not None, "renameat2 unsupported on this architecture")
    invoke_syscall(
        SYS_RENAMEAT2,
        ctypes.c_int(old_dir_fd),
        ctypes.c_char_p(os.fsencode(old)),
        ctypes.c_int(new_dir_fd),
        ctypes.c_char_p(os.fsencode(new)),
        ctypes.c_uint(flags),
    )


def pidfd_send_signal(pidfd: int, sig: int) -> None:
    require(SYS_PIDFD_SEND_SIGNAL is not None, "pidfd_send_signal unsupported")
    invoke_syscall(
        SYS_PIDFD_SEND_SIGNAL,
        ctypes.c_int(pidfd),
        ctypes.c_int(sig),
        ctypes.c_void_p(0),
        ctypes.c_uint(0),
    )


def wait_pidfd(pidfd: int, timeout_ms: int = PIDFD_POLL_TIMEOUT_MS) -> None:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
    events = poller.poll(timeout_ms)
    require(events, "pidfd did not reach a terminal state before deadline")


def identity_from_stat(value: os.stat_result) -> dict[str, int]:
    return {"dev": value.st_dev, "ino": value.st_ino, "nlink": value.st_nlink}


def process_starttime(pid: int) -> int:
    """Read Linux /proc starttime (field 22) for PID identity evidence."""
    value = read_text(Path(f"/proc/{pid}/stat"))
    _, rest = value.rsplit(") ", 1)
    fields = rest.split()
    require(len(fields) > 19, f"/proc/{pid}/stat is truncated")
    return int(fields[19])


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="strict")


def read_line_bounded(
    stream: Any,
    timeout_s: float = READINESS_TIMEOUT_S,
    label: str = "child",
) -> str:
    """Read one line without allowing a child to block the lane forever."""
    deadline = time.monotonic() + timeout_s
    poller = select.poll()
    poller.register(stream.fileno(), select.POLLIN | select.POLLHUP | select.POLLERR)
    data = bytearray()
    while len(data) < MAX_LINE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"{label}: bounded readiness read timed out")
        events = poller.poll(max(1, int(remaining * 1000)))
        if not events:
            raise TimeoutError(f"{label}: bounded readiness read timed out")
        # Read one byte at a time so a readiness and terminal marker arriving
        # in the same pipe packet cannot be discarded between bounded reads.
        chunk = os.read(stream.fileno(), 1)
        if not chunk:
            raise EOFError(f"{label}: child closed output before a complete line")
        data.extend(chunk)
        if b"\n" in data:
            line, _, _ = bytes(data).partition(b"\n")
            return line.decode("utf-8", errors="strict")
    raise ValueError(f"{label}: readiness line exceeded bounded output")


def process_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_process_group(child: subprocess.Popen[Any]) -> dict[str, Any]:
    """Drain a test process session, escalating only after a bounded wait."""
    pgid = child.pid
    term_sent = False
    kill_sent = False
    if process_group_exists(pgid):
        try:
            os.killpg(pgid, signal.SIGTERM)
            term_sent = True
        except ProcessLookupError:
            pass
        try:
            child.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
                kill_sent = True
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"process group {pgid} did not drain") from error
    deadline = time.monotonic() + PROCESS_DRAIN_TIMEOUT_S
    while process_group_exists(pgid) and time.monotonic() < deadline:
        time.sleep(0.01)
    require(not process_group_exists(pgid), f"process group {pgid} survived teardown")
    return {"pgid": pgid, "sigterm": term_sent, "sigkill": kill_sent, "drained": True}


def pidfd_signal_result(pidfd: int, sig: int) -> str:
    try:
        pidfd_send_signal(pidfd, sig)
    except OSError as error:
        if error.errno in {errno.ESRCH, errno.EBADF}:
            return "GONE" if error.errno == errno.ESRCH else "REJECTED"
        raise
    return "SENT"


def trace_hex(name: str, fuzz_bin: Path) -> tuple[str, dict[str, Any]]:
    fixture = TRACE_ROOT / f"{name}.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    require(payload["schema_version"] == 1, f"{name}: unsupported trace schema")
    require(payload["kind"] == "lifecycle-replay-trace", f"{name}: wrong trace kind")
    require(payload["trace_id"] == name, f"{name}: trace id mismatch")
    require(payload["scenario"] == name, f"{name}: scenario mismatch")
    require(payload["events"][-1]["kind"] == "terminal", f"{name}: terminal is not last")
    require(payload["expected"]["outcome"], f"{name}: expected outcome missing")
    result = subprocess.run(
        [str(fuzz_bin), "--corpus-hex", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
        env={**os.environ, "TMPDIR": os.environ["TMPDIR"]},
    )
    require(result.returncode == 0, f"{name}: corpus producer failed: {result.stderr}")
    encoded = result.stdout.strip()
    require(encoded and len(encoded) <= 8192, f"{name}: invalid encoded trace")
    int(encoded, 16)
    oracle = subprocess.run(
        [str(fuzz_bin), "--replay-hex", encoded],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        check=False,
        env={**os.environ, "TMPDIR": os.environ["TMPDIR"]},
    )
    require(oracle.returncode == 0, f"{name}: Rust oracle failed: {oracle.stderr[-2048:]}")
    try:
        oracle_report = json.loads(oracle.stdout)
    except json.JSONDecodeError as error:
        raise AssertionError(f"{name}: Rust oracle did not return JSON") from error
    require(oracle_report.get("status") == "ACCEPTED", f"{name}: Rust oracle rejected trace")
    require(
        oracle_report.get("terminal") == payload["expected"]["outcome"],
        f"{name}: Rust oracle outcome mismatch",
    )
    payload["_rust_oracle"] = {
        "status": oracle_report["status"],
        "terminal": oracle_report.get("terminal"),
        "event_count": oracle_report.get("event_count"),
        "recovery_steps": oracle_report.get("recovery_steps"),
        "coverage_hash": oracle_report.get("coverage_hash"),
        "source_identity": {
            key: oracle_report["source_identity"][key]
            for key in (
                "workspace_head",
                "semantic_closure_sha256",
                "fuzz_source_sha256",
                "fuzz_target_sha256",
                "bytecode_version",
            )
        },
    }
    return encoded, payload


def trace_plan(payload: dict[str, Any]) -> dict[str, Any]:
    """Derive the kernel action plan from the typed trace events.

    The host layer is deliberately a transport/execution adapter: it does not
    invent a second lifecycle model.  Every mode below is selected from the
    accepted event data and the Rust replay report is attached to the returned
    observation.
    """
    events = payload["events"]
    kinds = [event["kind"] for event in events]
    signals = [event["data"] for event in events if event["kind"] == "signal_sent"]
    identities = [event["data"] for event in events if event["kind"] == "identity_revalidated"]
    faults = [event["data"] for event in events if event["kind"] == "fault_injected"]
    members = [event["data"] for event in events if event["kind"] == "member_observed"]
    signal_result = signals[0].get("result") if signals else None
    identity_result = identities[0].get("result") if identities else None
    fault = faults[0].get("fault") if faults else None
    if signal_result == "GONE":
        mode = "signal-gone"
    elif signal_result == "REJECTED":
        mode = "signal-rejected"
    elif fault == "LEASE_HOLDER":
        mode = "retained-holder-timeout"
    elif identity_result == "MISMATCH":
        mode = "pid-reuse"
    elif members and members[0].get("origin") == "LATE_FORK":
        mode = "late-fork"
    elif identity_result == "GONE":
        mode = "root-exit-before-snapshot"
    else:
        raise AssertionError(f"unsupported trace action plan: {kinds}")
    return {
        "mode": mode,
        "event_kinds": kinds,
        "signal_result": signal_result,
        "identity_result": identity_result,
        "fault": fault,
        "member_origin": members[0].get("origin") if members else None,
        "terminal": payload["expected"]["outcome"],
    }


def kernel_observation_payload(observation: dict[str, Any]) -> dict[str, Any]:
    """Project host diagnostics into Rust's closed mode-tagged envelope."""
    common = {
        "trace_id": observation["trace_id"],
        "event_kinds": observation["event_kinds"],
        "outcome": observation["outcome"],
        "filesystem_clean": observation["filesystem_clean"],
        "pidfd_retained": observation["pidfd_retained"],
    }
    mode = observation["mode"]
    if mode == "signal-gone":
        return {
            "mode": mode,
            "common": common,
            "signal": observation["signal"],
            "post_exit_signal": observation["post_exit_signal"],
        }
    if mode == "signal-rejected":
        return {
            "mode": mode,
            "common": common,
            "signal": observation["signal"],
            "rejection_errno": observation["rejection_errno"],
        }
    if mode == "retained-holder-timeout":
        return {
            "mode": mode,
            "common": common,
            "deadline": observation["deadline"],
            "recovery_completed": observation["recovery"]["drained"],
        }
    if mode == "pid-reuse":
        return {
            "mode": mode,
            "common": common,
            "identity": observation["identity"],
            "pid_reuse_classification": observation["pid_reuse_classification"],
            "original_pid": observation["pid"],
            "original_starttime": observation["original_starttime"],
            "replacement_pid": observation["replacement_pid"],
            "replacement_starttime": observation["replacement_starttime"],
            "retired_pidfd_signal": observation["retired_pidfd_signal"],
            "replacement_alive": observation["replacement_alive"],
        }
    if mode == "late-fork":
        return {
            "mode": mode,
            "common": common,
            "late_fork": observation["late_fork"],
            "late_pid": observation["late_pid"],
            "late_starttime": observation["late_starttime"],
            "late_fork_observed_by_controller": observation["late_fork_observed_by_controller"],
            "late_fork_acknowledged": observation["late_fork_acknowledged"],
            "late_fork_post_drain": observation["late_fork_post_drain"],
        }
    if mode == "root-exit-before-snapshot":
        return {
            "mode": mode,
            "common": common,
            "identity": observation["identity"],
            "children_traversed": observation["children_traversed"],
        }
    fail(f"unsupported kernel observation mode {mode}")


def run_rename_and_permissions(root: Path) -> dict[str, Any]:
    require(SYS_RENAMEAT2 is not None, "renameat2 is unavailable")
    fixture = root / "rename-permissions"
    fixture.mkdir(mode=0o700)
    parent_fd = os.open(fixture, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        source = fixture / "source"
        source.write_bytes(b"original")
        source_identity = identity_from_stat(source.stat())
        renameat2(parent_fd, "source", parent_fd, "quarantine", RENAME_NOREPLACE)
        quarantine = fixture / "quarantine"
        quarantine_identity = identity_from_stat(quarantine.stat())
        require(quarantine_identity == source_identity, "renameat2 changed inode identity")

        # ABA replacement: a foreign object can reclaim the old name, but
        # RENAME_NOREPLACE must refuse to overwrite it.
        replacement = fixture / "source"
        replacement.write_bytes(b"replacement")
        replacement_identity = identity_from_stat(replacement.stat())
        try:
            renameat2(parent_fd, "quarantine", parent_fd, "source", RENAME_NOREPLACE)
        except OSError as error:
            require(error.errno == errno.EEXIST, f"ABA returned {error.errno}, not EEXIST")
        else:
            fail("renameat2 ABA replacement was accepted")
        require(identity_from_stat(replacement.stat()) == replacement_identity, "ABA replacement changed")
        quarantine.unlink()
        replacement.unlink()

        permission_parent = fixture / "permission-parent"
        permission_parent.mkdir(mode=0o700)
        permission_fd = os.open(permission_parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            unreadable = permission_parent / "mode-000"
            unreadable.write_bytes(b"unlink does not read")
            unreadable.chmod(0)
            os.unlink("mode-000", dir_fd=permission_fd)
            require(not unreadable.exists(), "mode-000 unlink did not remove leaf")

            empty = permission_parent / "empty-mode-000"
            empty.mkdir(mode=0o700)
            empty.chmod(0)
            os.rmdir("empty-mode-000", dir_fd=permission_fd)
            require(not empty.exists(), "mode-000 rmdir did not remove directory")
        finally:
            os.close(permission_fd)
        return {
            "renameat2": True,
            "rename_noreplace_aba": "EEXIST",
            "source_identity": source_identity,
            "quarantine_identity": quarantine_identity,
            "replacement_identity": replacement_identity,
            "permission_unlink_mode_000": True,
            "permission_rmdir_mode_000": True,
        }
    finally:
        os.close(parent_fd)


def run_af_unix(root: Path) -> dict[str, Any]:
    socket_dir = root / "af-unix"
    socket_dir.mkdir(mode=0o700)
    path = socket_dir / "runtime.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    client = None
    accepted = None
    try:
        server.bind(str(path))
        server.listen(1)
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        client.connect(str(path))
        accepted, _ = server.accept()
        accepted.settimeout(2)
        client.sendall(b"kernel-ready")
        require(accepted.recv(32) == b"kernel-ready", "AF_UNIX payload mismatch")
    finally:
        for descriptor in (accepted, client, server):
            if descriptor is not None:
                descriptor.close()
        if path.exists():
            path.unlink()
    require(not path.exists(), "AF_UNIX endpoint survived cleanup")
    return {"socket_created": True, "socket_removed": True, "path": str(path)}


SESSION_CHILD = r'''
import os, signal, socket, sys, time
root, mode, token = sys.argv[1:]
path = os.path.join(root, "session.sock")

def idle(_signum, _frame):
    pass

if mode == "root-exit-before-snapshot":
    print("REAL_KERNEL_TRACE_READY mode=" + mode, flush=True)
    os._exit(0)

worker = os.fork()
if worker == 0:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
    while True:
        time.sleep(0.05)

server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
if mode == "retained-holder-timeout":
    signal.signal(signal.SIGTERM, idle)
if mode == "session-cycle":
    print("REAL_KERNEL_SESSION_READY cycle=" + token, flush=True)
else:
    print("REAL_KERNEL_TRACE_READY mode=" + mode, flush=True)
conn, _ = server.accept()
message = conn.recv(32)
if message != b"SHUTDOWN":
    os._exit(31)

if mode == "retained-holder-timeout":
    # The controller must hit its bounded deadline and then own the
    # process-group escalation.  This process intentionally does not drain.
    while True:
        time.sleep(0.05)

if mode == "late-fork":
    late = os.fork()
    if late == 0:
        signal.signal(signal.SIGTERM, lambda *_: os._exit(0))
        time.sleep(30)
    with open("/proc/%d/stat" % late, "r", encoding="ascii") as stream:
        stat_line = stream.read()
    starttime = stat_line.rsplit(") ", 1)[1].split()[19]
    print("REAL_KERNEL_LATE_FORK pid=%d starttime=%s" % (late, starttime), flush=True)
    if conn.recv(32) != b"LATE_ACK":
        os._exit(33)
    os.kill(late, signal.SIGTERM)
    os.waitpid(late, 0)

conn.close()

os.kill(worker, signal.SIGTERM)
_, status = os.waitpid(worker, 0)
if status != 0:
    os._exit(32)
server.close()
if os.path.exists(path):
    os.unlink(path)
if mode == "session-cycle":
    print("REAL_KERNEL_SESSION_DRAINED cycle=" + token, flush=True)
else:
    print("REAL_KERNEL_TRACE_DRAINED mode=" + mode, flush=True)
'''


def run_session_cycles(root: Path, traces: list[tuple[str, str]]) -> list[dict[str, Any]]:
    session_root = root / "reuse-session"
    session_root.mkdir(mode=0o700)
    cycles: list[dict[str, Any]] = []
    for index in range(MAX_CYCLES):
        child = subprocess.Popen(
            [sys.executable, "-c", SESSION_CHILD, str(session_root), "session-cycle", str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            start_new_session=True,
        )
        pidfd = os.pidfd_open(child.pid, 0)
        try:
            try:
                ready = read_line_bounded(child.stdout, label=f"cycle-{index}-ready") if child.stdout is not None else ""
            except (TimeoutError, EOFError, ValueError) as error:
                terminate_process_group(child)
                stderr = child.stderr.read(4096).decode("utf-8", errors="replace") if child.stderr else ""
                raise RuntimeError(f"cycle {index} readiness failed rc={child.returncode}: {stderr[-1024:]}") from error
            require(ready == f"REAL_KERNEL_SESSION_READY cycle={index}", f"cycle {index}: not ready")
            endpoint = session_root / "session.sock"
            require(endpoint.exists(), f"cycle {index}: endpoint missing")
            client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
            try:
                client.connect(str(endpoint))
                client.sendall(b"SHUTDOWN")
            finally:
                client.close()
            return_code = child.wait(timeout=5)
            wait_pidfd(pidfd)
            try:
                output = read_line_bounded(child.stdout, label=f"cycle-{index}-drained") if child.stdout is not None else ""
            except (TimeoutError, EOFError, ValueError) as error:
                stderr = child.stderr.read(4096).decode("utf-8", errors="replace") if child.stderr else ""
                raise RuntimeError(
                    f"cycle {index} drain marker missing rc={return_code}: {stderr[-1024:]}"
                ) from error
            require(return_code == 0, f"cycle {index}: child exited {return_code}")
            require(output == f"REAL_KERNEL_SESSION_DRAINED cycle={index}", f"cycle {index}: not drained")
            require(not endpoint.exists(), f"cycle {index}: stale endpoint")
            cycles.append(
                {
                    "cycle": index + 1,
                    "pid": child.pid,
                    "trace": TRACE_NAMES[index],
                    "trace_bytecode_hex": traces[index][0],
                    "ready": ready,
                    "drained": output,
                    "pidfd_terminal": True,
                    "reuse_root_preserved": True,
                }
            )
        finally:
            os.close(pidfd)
            if child.poll() is None or process_group_exists(child.pid):
                terminate_process_group(child)
    require(not list(session_root.iterdir()), "reuse root contains stale endpoint")
    return cycles


def run_trace_kernel_case(
    root: Path,
    encoded: str,
    payload: dict[str, Any],
    fuzz_bin: Path,
) -> dict[str, Any]:
    """Execute one accepted .4 trace against real pidfd/process/socket state."""
    plan = trace_plan(payload)
    mode = plan["mode"]
    # AF_UNIX pathname limits are part of the real-kernel contract.  Keep the
    # disposable directory short while retaining the full trace_id in JSON.
    case_index = TRACE_NAMES.index(payload["trace_id"])
    case_root = root / "tc" / f"t{case_index:02d}"
    case_root.mkdir(mode=0o700, parents=True)
    child = subprocess.Popen(
        [sys.executable, "-c", SESSION_CHILD, str(case_root), mode, payload["trace_id"]],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    pidfd = os.pidfd_open(child.pid, 0)
    endpoint = case_root / "session.sock"
    observations: dict[str, Any] = {
        "trace_id": payload["trace_id"],
        "mode": mode,
        "event_kinds": plan["event_kinds"],
        "pid": child.pid,
        "pidfd_retained": True,
        "rust_oracle": payload["_rust_oracle"],
    }
    try:
        try:
            ready = read_line_bounded(child.stdout, label=f"trace-{mode}-ready") if child.stdout is not None else ""
        except (TimeoutError, EOFError, ValueError) as error:
            terminate_process_group(child)
            stderr = child.stderr.read(4096).decode("utf-8", errors="replace") if child.stderr else ""
            raise RuntimeError(f"trace {mode} readiness failed rc={child.returncode}: {stderr[-1024:]}") from error
        require(ready == f"REAL_KERNEL_TRACE_READY mode={mode}", f"{mode}: readiness mismatch")
        observations["ready"] = ready
        if mode == "root-exit-before-snapshot":
            wait_pidfd(pidfd)
            child.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
            require(not endpoint.exists(), "root-exit left a runtime endpoint")
            observations.update({"identity": "GONE", "children_traversed": False, "endpoint": "ABSENT"})
        else:
            require(endpoint.exists(), f"{mode}: endpoint missing")
            if mode == "signal-gone":
                require(pidfd_signal_result(pidfd, signal.SIGTERM) == "SENT", "pidfd TERM failed")
                wait_pidfd(pidfd)
                child.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
                try:
                    pidfd_send_signal(pidfd, signal.SIGTERM)
                except OSError as error:
                    require(error.errno == errno.ESRCH, f"post-exit pidfd errno={error.errno}")
                    observations["post_exit_signal"] = "ESRCH"
                else:
                    fail("post-exit pidfd signal was accepted")
                observations["signal"] = "GONE"
            elif mode == "signal-rejected":
                try:
                    pidfd_send_signal(pidfd, 65)
                except OSError as error:
                    require(error.errno == errno.EINVAL, f"rejected signal errno={error.errno}")
                    observations["signal"] = "REJECTED"
                    observations["rejection_errno"] = "EINVAL"
                else:
                    fail("invalid signal was accepted")
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
                try:
                    client.connect(str(endpoint))
                    client.sendall(b"SHUTDOWN")
                finally:
                    client.close()
                child.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
                os.close(pidfd)
                pidfd = -1
            elif mode == "retained-holder-timeout":
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
                try:
                    client.connect(str(endpoint))
                    client.sendall(b"SHUTDOWN")
                finally:
                    client.close()
                require(pidfd_signal_result(pidfd, signal.SIGTERM) == "SENT", "timeout signal failed")
                try:
                    wait_pidfd(pidfd, timeout_ms=500)
                except AssertionError:
                    observations["deadline"] = "EXPIRED"
                    observations["recovery"] = terminate_process_group(child)
                else:
                    fail("retained holder exited before bounded deadline")
            else:
                client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
                client.connect(str(endpoint))
                client.sendall(b"SHUTDOWN")
                if mode != "late-fork":
                    client.close()
                original_starttime = process_starttime(child.pid) if mode == "pid-reuse" else None
                observations["signal"] = "GONE" if mode == "signal-gone" else "SENT"
                if mode == "late-fork":
                    late_line = read_line_bounded(child.stdout, label="late-fork-observation")
                    require(late_line.startswith("REAL_KERNEL_LATE_FORK pid="), "late fork marker missing")
                    late_pid_text, late_start_text = late_line.removeprefix("REAL_KERNEL_LATE_FORK pid=").split(
                        " starttime=", 1
                    )
                    late_pid = int(late_pid_text)
                    late_starttime = int(late_start_text)
                    require(Path(f"/proc/{late_pid}").exists(), "controller missed live late fork")
                    require(process_starttime(late_pid) == late_starttime, "late fork identity changed")
                    client.sendall(b"LATE_ACK")
                    client.close()
                    observations["late_fork"] = "OBSERVED_AND_REAPED"
                    observations["late_pid"] = late_pid
                    observations["late_starttime"] = late_starttime
                    observations["late_fork_observed_by_controller"] = True
                    child.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
                    wait_pidfd(pidfd)
                    require(not Path(f"/proc/{late_pid}").exists(), "late fork survived drain")
                    observations["late_fork_acknowledged"] = True
                    observations["late_fork_post_drain"] = "GONE"
                else:
                    child.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
                    wait_pidfd(pidfd)
                if mode == "pid-reuse":
                    replacement = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
                    try:
                        replacement_starttime = process_starttime(replacement.pid)
                        try:
                            pidfd_send_signal(pidfd, signal.SIGTERM)
                        except OSError as error:
                            require(error.errno == errno.ESRCH, f"old pidfd errno={error.errno}")
                        else:
                            fail("old pidfd signalled a replacement")
                        observations["identity"] = "MISMATCH"
                        observations["original_starttime"] = original_starttime
                        observations["replacement_pid"] = replacement.pid
                        observations["replacement_starttime"] = replacement_starttime
                        observations["retired_pidfd_signal"] = "ESRCH"
                        observations["replacement_alive"] = replacement.poll() is None
                        require(observations["replacement_alive"], "replacement exited before witness capture")
                        observations["pid_reuse_classification"] = (
                            "NUMERIC_PID_REUSE_STARTTIME_MISMATCH"
                            if replacement.pid == child.pid
                            else "RETIRED_PIDFD_REPLACEMENT_NOT_TARGETED"
                        )
                    finally:
                        replacement.terminate()
                        replacement.wait(timeout=PROCESS_DRAIN_TIMEOUT_S)
                require(not endpoint.exists(), f"{mode}: stale endpoint")
            observations.setdefault("endpoint", "ABSENT")
        observations["outcome"] = payload["expected"]["outcome"]
        if endpoint.exists():
            endpoint.unlink()
        leftovers = sorted(path.name for path in case_root.iterdir())
        observations["filesystem_clean"] = not leftovers
        observations["leftovers"] = leftovers
        require(observations["filesystem_clean"], f"{mode}: trace root not clean: {leftovers}")
        kernel_oracle = subprocess.run(
            [str(fuzz_bin), "--verify-kernel-observation", encoded],
            input=json.dumps(kernel_observation_payload(observations)),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "TMPDIR": os.environ["TMPDIR"]},
        )
        require(
            kernel_oracle.returncode == 0,
            f"{mode}: Rust kernel observation oracle rejected: {kernel_oracle.stderr[-2048:]}",
        )
        observations["rust_kernel_verdict"] = json.loads(kernel_oracle.stdout)
        return observations
    finally:
        if pidfd >= 0:
            os.close(pidfd)
        if child.poll() is None or process_group_exists(child.pid):
            terminate_process_group(child)
        # The lane owns this disposable namespace.  A timeout fixture is
        # intentionally non-cooperative, so remove its endpoint only after
        # the process-group drain has completed and record the clean result.
        if endpoint.exists():
            endpoint.unlink()


def run_failure_teardown_fixture(root: Path) -> dict[str, Any]:
    """Exercise bounded readiness and SIGINT-style process-tree unwind."""
    failure_root = root / "failure-teardown"
    failure_root.mkdir(mode=0o700)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    bounded_timeout = False
    try:
        try:
            read_line_bounded(child.stdout, timeout_s=0.25, label="silent-failure-fixture")
        except (TimeoutError, ValueError):
            bounded_timeout = True
        else:
            fail("silent child escaped bounded readiness fixture")
    finally:
        unwind = terminate_process_group(child)
    require(bounded_timeout, "readiness timeout was not observed")
    require(not process_group_exists(child.pid), "failure fixture process group survived")
    require(not list(failure_root.iterdir()), "failure fixture namespace is not empty")
    return {"pid": child.pid, "bounded_readiness_timeout": bounded_timeout, "unwind": unwind, "clean": True}


def run_kernel_observation_negative_contract(
    fuzz_bin: Path,
    encoded: str,
    observation: dict[str, Any],
    pid_reuse_encoded: str,
    pid_reuse_observation: dict[str, Any],
) -> dict[str, Any]:
    """Rust must reject every forged trace/observation cross-pair."""
    base = kernel_observation_payload(observation)
    mutations: list[tuple[str, dict[str, Any]]] = []
    wrong_id = json.loads(json.dumps(base))
    wrong_id["common"]["trace_id"] = "forged-unrelated-trace"
    mutations.append((encoded, wrong_id))
    wrong_mode = json.loads(json.dumps(base))
    wrong_mode["mode"] = "pid-reuse"
    mutations.append((encoded, wrong_mode))
    reordered = json.loads(json.dumps(base))
    reordered["common"]["event_kinds"] = list(reversed(reordered["common"]["event_kinds"]))
    mutations.append((encoded, reordered))
    missing = json.loads(json.dumps(base))
    missing["common"]["event_kinds"] = missing["common"]["event_kinds"][1:]
    mutations.append((encoded, missing))
    extra = json.loads(json.dumps(base))
    extra["common"]["event_kinds"] = [*extra["common"]["event_kinds"], "fabricated"]
    mutations.append((encoded, extra))
    unknown = json.loads(json.dumps(base))
    unknown["forged_kernel_fact"] = True
    mutations.append((encoded, unknown))
    incomplete = json.loads(json.dumps(base))
    incomplete.pop("post_exit_signal", None)
    mutations.append((encoded, incomplete))
    pid_reuse_base = kernel_observation_payload(pid_reuse_observation)
    incomplete_pid_reuse = json.loads(json.dumps(pid_reuse_base))
    incomplete_pid_reuse.pop("original_starttime", None)
    mutations.append((pid_reuse_encoded, incomplete_pid_reuse))
    oversized = json.loads(json.dumps(base))
    oversized["forged_kernel_fact"] = "x" * (16 * 1024)
    mutations.append((encoded, oversized))
    for index, (candidate_encoded, candidate) in enumerate(mutations):
        result = subprocess.run(
            [str(fuzz_bin), "--verify-kernel-observation", candidate_encoded],
            input=json.dumps(candidate),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
            env={**os.environ, "TMPDIR": os.environ["TMPDIR"]},
        )
        require(result.returncode != 0, f"observation tamper case {index} was accepted")
    return {"cases": len(mutations), "rejected": len(mutations), "kinds": [
        "wrong-trace-id",
        "wrong-mode",
        "reordered-events",
        "missing-event",
        "extra-event",
        "unknown-field",
        "incomplete-signal-witness",
        "incomplete-pid-reuse-witness",
        "oversized-input",
    ]}


def run_wrapper_interruption_fixture(root: Path) -> dict[str, Any]:
    """Interrupt a nested session and prove process-group/socket unwind."""
    interruption_root = root / "interrupt"
    interruption_root.mkdir(mode=0o700)
    child = subprocess.Popen(
        [sys.executable, "-c", SESSION_CHILD, str(interruption_root), "session-cycle", "INT"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
        start_new_session=True,
    )
    endpoint = interruption_root / "session.sock"
    try:
        ready = read_line_bounded(child.stdout, label="interrupt-ready")
        require(ready == "REAL_KERNEL_SESSION_READY cycle=INT", "interrupt fixture was not ready")
        require(endpoint.exists(), "interrupt fixture endpoint missing")
        os.killpg(child.pid, signal.SIGINT)
        unwind = terminate_process_group(child)
        if endpoint.exists():
            endpoint.unlink()
        require(not process_group_exists(child.pid), "SIGINT process group survived")
        require(not list(interruption_root.iterdir()), "SIGINT left endpoint artifacts")
        return {"signal": "SIGINT", "ready": ready, "unwind": unwind, "clean": True}
    finally:
        if child.poll() is None or process_group_exists(child.pid):
            terminate_process_group(child)
        if endpoint.exists():
            endpoint.unlink()


def cgroup_helper(root: Path) -> None:
    """Run only the privileged cgroup-v2 fixture and print bounded JSON."""
    parent = Path("/sys/fs/cgroup")
    require((parent / "cgroup.controllers").is_file(), "cgroup v2 controllers unavailable")
    name = f"darling-lifecycle-real-kernel-{os.getpid()}-{time.monotonic_ns()}"
    group = parent / name
    member = None
    foreign = None
    churn_processes: list[subprocess.Popen[Any]] = []
    group_fd = -1
    result: dict[str, Any] = {"backend": "CGROUP_V2_PIDFD", "name": name}
    try:
        group.mkdir(mode=0o700)
        group_fd = os.open(group, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        before = identity_from_stat(os.fstat(group_fd))
        result["identity_before"] = before
        result["controllers"] = read_text(parent / "cgroup.controllers").split()
        require((group / "cgroup.procs").is_file(), "cgroup.procs unavailable")
        require((group / "cgroup.events").is_file(), "cgroup.events unavailable")

        member = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        foreign = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        (group / "cgroup.procs").write_text(f"{member.pid}\n", encoding="ascii")
        member_pidfd = os.pidfd_open(member.pid, 0)
        foreign_pidfd = os.pidfd_open(foreign.pid, 0)
        try:
            listed = [int(line) for line in read_text(group / "cgroup.procs").split()]
            require(member.pid in listed, "member was not observed in cgroup")
            pidfd_send_signal(member_pidfd, signal.SIGTERM)
            wait_pidfd(member_pidfd)
            member.wait(timeout=5)
            require(foreign.poll() is None, "unrelated process was affected")

            for _ in range(MAX_CHURN):
                process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
                churn_processes.append(process)
                (group / "cgroup.procs").write_text(f"{process.pid}\n", encoding="ascii")
                descriptor = os.pidfd_open(process.pid, 0)
                try:
                    pidfd_send_signal(descriptor, signal.SIGTERM)
                    wait_pidfd(descriptor)
                finally:
                    os.close(descriptor)
                process.wait(timeout=5)
            events = read_text(group / "cgroup.events")
            require("populated 0" in events, f"cgroup did not drain: {events!r}")
            result.update(
                {
                    "member_pidfd": True,
                    "member_pid": member.pid,
                    "foreign_pid": foreign.pid,
                    "foreign_pidfd": True,
                    "unrelated_process_survived": True,
                    "process_churn": len(churn_processes),
                    "churn_pids": [process.pid for process in churn_processes],
                    "churn_reaped": sum(process.poll() is not None for process in churn_processes),
                    "member_count_after_drain": len(read_text(group / "cgroup.procs").split()),
                    "events_after_drain": events.splitlines(),
                }
            )
        finally:
            os.close(member_pidfd)
            os.close(foreign_pidfd)
            if foreign.poll() is None:
                foreign.terminate()
                foreign.wait(timeout=5)
    finally:
        for process in churn_processes:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired as error:
                raise RuntimeError(f"churn process {process.pid} survived cleanup") from error
        if member is not None and member.poll() is None:
            member.kill()
            member.wait(timeout=5)
        if foreign is not None and foreign.poll() is None:
            foreign.kill()
            foreign.wait(timeout=5)
        if group_fd >= 0:
            after = identity_from_stat(os.fstat(group_fd))
            result["identity_after"] = after
            require(after == result["identity_before"], "cgroup fd identity changed")
            os.close(group_fd)
        if group.exists():
            # A populated cgroup must never be hidden by forceful cleanup.
            require("populated 0" in read_text(group / "cgroup.events"), "cgroup still populated")
            group.rmdir()
        result["cgroup_removed"] = not group.exists()
    print(json.dumps(result, sort_keys=True))


def run_cgroup(root: Path) -> dict[str, Any]:
    helper = [sys.executable, str(Path(__file__).resolve()), "--cgroup-helper", str(root)]
    delegation_probe = Path("/sys/fs/cgroup") / f"darling-delegation-probe-{os.getpid()}"
    delegated = False
    try:
        delegation_probe.mkdir(mode=0o700)
        delegated = True
    except OSError:
        delegated = False
    finally:
        if delegation_probe.exists():
            delegation_probe.rmdir()
    command = helper if delegated else ["sudo", "-n", *helper]
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=45,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "TMPDIR": os.environ["TMPDIR"],
            "PYTHONDONTWRITEBYTECODE": "1",
        },
    )
    if result.returncode != 0:
        raise RuntimeError(f"cgroup helper failed rc={result.returncode}: {result.stderr[-4096:]}")
    payload = json.loads(result.stdout)
    require(payload["backend"] == "CGROUP_V2_PIDFD", "wrong cgroup backend")
    require(payload["cgroup_removed"] is True, "cgroup cleanup was not proven")
    require(payload["process_churn"] == MAX_CHURN, "process churn budget mismatch")
    require(payload["unrelated_process_survived"] is True, "foreign process proof missing")
    payload["privilege"] = "unprivileged-delegated" if delegated else "sudo-root"
    return payload


def fd_census() -> int:
    return len(list(Path("/proc/self/fd").iterdir()))


def mount_snapshot(root: Path) -> list[str]:
    """Return only mounts whose mountpoint is inside the owned lane root."""
    encoded_root = str(root).replace("\\", "\\\\").replace(" ", "\\040")
    matches: list[str] = []
    for line in read_text(Path("/proc/self/mountinfo")).splitlines():
        fields = line.split(" - ", 1)[0].split()
        if len(fields) >= 5 and (fields[4] == encoded_root or fields[4].startswith(encoded_root + "/")):
            matches.append(fields[4])
    return sorted(matches)


def tracked_process_snapshot(pids: set[int]) -> list[int]:
    return sorted(pid for pid in pids if Path(f"/proc/{pid}").exists())


def path_census(root: Path) -> dict[str, list[str]]:
    sockets = sorted(str(path.relative_to(root)) for path in root.glob("**/*.sock") if path.is_socket())
    transactions = sorted(
        str(path.relative_to(root))
        for path in root.glob("**/*")
        if "transaction" in path.name.lower() or "txn" in path.name.lower()
    )
    return {"socket_paths": sockets, "transaction_paths": transactions}


def baseline_digest(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    entries = 0
    total_bytes = 0
    if path.is_file():
        require(path.stat().st_size <= MAX_BASELINE_BYTES, "baseline file exceeds bounded byte limit")
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
        return digest.hexdigest()
    for child in sorted(path.rglob("*")):
        entries += 1
        require(entries <= 4096, "baseline manifest exceeds bounded entry limit")
        relative = str(child.relative_to(path)).encode()
        digest.update(relative)
        try:
            digest.update(str(child.stat().st_mode).encode())
            if child.is_file():
                size = child.stat().st_size
                total_bytes += size
                require(total_bytes <= MAX_BASELINE_BYTES, "baseline files exceed bounded byte limit")
                digest.update(child.read_bytes())
        except FileNotFoundError:
            digest.update(b"<vanished>")
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cgroup-helper", type=Path)
    args = parser.parse_args()
    if args.cgroup_helper is not None:
        cgroup_helper(args.cgroup_helper)
        return 0

    root = Path(os.environ["DARLING_LIFECYCLE_REAL_KERNEL_ROOT"]).resolve()
    fuzz_bin = Path(os.environ["DARLING_LIFECYCLE_FUZZ_BIN"]).resolve()
    require(root.is_dir(), "lane root is not owned")
    require(fuzz_bin.is_file(), ".4 trace producer is missing")
    before_fds = fd_census()
    mounts_before = mount_snapshot(root)
    baseline_path = os.environ.get("DARLING_LIFECYCLE_BASELINE")
    baseline_scope = "NONE"
    baseline_before = None
    if baseline_path:
        baseline = Path(baseline_path).resolve()
        require(baseline.exists(), "declared baseline does not exist")
        baseline_scope = str(baseline)
        baseline_before = baseline_digest(baseline)
    traces = [trace_hex(name, fuzz_bin) for name in TRACE_NAMES]
    require(len(traces) == MAX_TRACE_CASES, "not all golden traces were loaded")
    kernel = run_cgroup(root)
    rename = run_rename_and_permissions(root)
    sockets = run_af_unix(root)
    trace_observations = [
        run_trace_kernel_case(root, encoded, payload, fuzz_bin)
        for encoded, payload in traces
    ]
    observation_negative = run_kernel_observation_negative_contract(
        fuzz_bin,
        traces[0][0],
        trace_observations[0],
        traces[3][0],
        trace_observations[3],
    )
    interruption = run_wrapper_interruption_fixture(root)
    failure_teardown = run_failure_teardown_fixture(root)
    cycles = run_session_cycles(root, traces)
    require(len(cycles) == MAX_CYCLES, "three reuse cycles were not completed")
    require(not list((root / "reuse-session").iterdir()), "reuse-session has stale entries")
    mounts_after = mount_snapshot(root)
    paths_after = path_census(root)
    after_fds = fd_census()
    tracked_pids = {
        int(kernel[key])
        for key in ("member_pid", "foreign_pid")
        if key in kernel
    }
    tracked_pids.update(int(pid) for pid in kernel.get("churn_pids", []))
    tracked_pids.update(int(item["pid"]) for item in trace_observations if "pid" in item)
    tracked_pids.update(int(item["pid"]) for item in cycles if "pid" in item)
    tracked_pids.add(int(failure_teardown["pid"]))
    tracked_after = tracked_process_snapshot(tracked_pids)
    require(not tracked_after, f"tracked processes survived: {tracked_after}")
    baseline_after = baseline_digest(Path(baseline_path).resolve()) if baseline_path else None
    baseline_mutated = None if baseline_scope == "NONE" else baseline_before != baseline_after
    require(not mounts_after, f"owned mounts survived: {mounts_after}")
    require(not paths_after["socket_paths"], f"socket artifact survived: {paths_after}")
    require(not paths_after["transaction_paths"], f"transaction artifact survived: {paths_after}")
    require(after_fds <= before_fds + 2, f"fd census grew: before={before_fds} after={after_fds}")
    output = {
        "status": "REAL_KERNEL_LANE_5_1_VALID",
        "lane": "dar-4ush.5.1",
        "guest_ready_lane": "dar-4ush.5.2-deferred",
        "kernel": platform.release(),
        "architecture": MACHINE,
        "accepted_trace_count": len(traces),
        "accepted_traces": [
            {"name": name, "bytecode_hex": encoded, "expected": payload["expected"]}
            for name, (encoded, payload) in zip(TRACE_NAMES, traces)
        ],
        "trace_kernel_observations": trace_observations,
        "trace_observation_negative": observation_negative,
        "wrapper_interruption": interruption,
        "failure_teardown": failure_teardown,
        "cgroup": kernel,
        "rename_permissions": rename,
        "af_unix": sockets,
        "cycles": cycles,
        "teardown": {
            "fd_count_before": before_fds,
            "fd_count_after": after_fds,
            "fd_growth": after_fds - before_fds,
            "fd_growth_bounded": after_fds <= before_fds + 2,
            "socket_paths": paths_after["socket_paths"],
            "transaction_paths": paths_after["transaction_paths"],
            "mounts_before": mounts_before,
            "mounts_after": mounts_after,
            "mounts_created": sorted(set(mounts_after) - set(mounts_before)),
            "tracked_processes_before": sorted(tracked_pids),
            "tracked_processes_after": tracked_after,
            "baseline_scope": baseline_scope,
            "baseline_digest_before": baseline_before,
            "baseline_digest_after": baseline_after,
            "baseline_mutated": baseline_mutated,
            "product_routing": "deferred_to_dar-4ush.7",
        },
    }
    encoded_output = json.dumps(output, sort_keys=True)
    require(len(encoded_output.encode()) <= MAX_OUTPUT, "lane output exceeds bound")
    print(encoded_output)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AssertionError, EOFError, OSError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f"REAL_KERNEL_LANE_FAILED {error}", file=sys.stderr)
        raise SystemExit(1)
