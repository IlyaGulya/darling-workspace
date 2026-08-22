#!/usr/bin/env python3
"""Bound one CI command with kernel process authority; semantics stay in Rust."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import select
import selectors
import signal
import shutil
import stat
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

PROC_EVENT_FORK = 0x00000001
PROC_EVENT_EXIT = 0x80000000
PROC_CN_MCAST_LISTEN = 1


def process_identity(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").rsplit(") ", 1)[1].split()
        return int(fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, IndexError, ValueError):
        return None


def process_rss_kib(pid: int) -> int:
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        pass
    return 0


@dataclass
class OwnedProcess:
    starttime: int
    pidfd: int


@dataclass(frozen=True)
class LineageNode:
    generation: int


class ProcessOwnership:
    """Own every process TGID descended from the registered command root."""

    def __init__(self) -> None:
        self.socket = socket.socket(socket.AF_NETLINK, socket.SOCK_DGRAM, 11)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16 * 1024 * 1024)
        self.socket.bind((os.getpid(), 1))
        payload = struct.pack("=IIIIHHI", 1, 1, 0, 0, 4, 0, PROC_CN_MCAST_LISTEN)
        header = struct.pack("=IHHII", 16 + len(payload), 3, 0, 0, os.getpid())
        self.socket.send(header + payload)
        self.socket.settimeout(0.1)
        self.identities: dict[int, OwnedProcess] = {}
        self.lineage: dict[int, LineageNode] = {}
        self.next_generation = 1
        self.lost = False
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.reader = threading.Thread(target=self._read_events, name="lifecycle-lab-proc-events", daemon=True)
        self.reader_started = False

    def add_root(self, pid: int) -> None:
        self._register_process(pid, require_pidfd=True)
        # The connector was subscribed before spawn. Starting its reader only
        # after root registration preserves queued fork events and closes the
        # spawn -> registration race.
        self.reader.start()
        self.reader_started = True

    def _begin_lineage(self, tgid: int) -> LineageNode:
        with self.lock:
            if tgid in self.lineage:
                self.lost = True
                return self.lineage[tgid]
            node = LineageNode(self.next_generation)
            self.next_generation += 1
            self.lineage[tgid] = node
            return node

    def _register_process(self, tgid: int, *, require_pidfd: bool = False) -> None:
        # Fork events are ordered. Record the generation-bound ancestry before
        # any fallible /proc or pidfd acquisition so a reaped intermediate can
        # still authorize already queued descendant FORK events until its EXIT
        # event is consumed. Such a node is never signalable without a pidfd.
        self._begin_lineage(tgid)
        before = process_identity(tgid)
        if before is None:
            if require_pidfd:
                self.lost = True
            return
        try:
            pidfd = os.pidfd_open(tgid, 0)
        except OSError:
            # A short-lived child may exit between the proc identity read and
            # pidfd_open. That is a proven terminal race, not event loss.
            if process_identity(tgid) is not None:
                self.lost = True
            elif require_pidfd:
                self.lost = True
            return
        after = process_identity(tgid)
        if after != before:
            os.close(pidfd)
            if after is not None:
                self.lost = True
            return
        with self.lock:
            if tgid in self.identities:
                os.close(pidfd)
            else:
                self.identities[tgid] = OwnedProcess(before, pidfd)

    def _forget_process(self, tgid: int) -> None:
        with self.lock:
            owned = self.identities.pop(tgid, None)
            self.lineage.pop(tgid, None)
        if owned is not None:
            os.close(owned.pidfd)

    @staticmethod
    def _pidfd_exited(pidfd: int) -> bool:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN | select.POLLHUP | select.POLLERR)
        return bool(poller.poll(0))

    def _read_events(self) -> None:
        while not self.stopping.is_set():
            try:
                packet = self.socket.recv(65536)
            except TimeoutError:
                continue
            except OSError:
                if not self.stopping.is_set():
                    self.lost = True
                return
            offset = 0
            while offset + 16 <= len(packet):
                length, message_type, _, _, _ = struct.unpack_from("=IHHII", packet, offset)
                if length < 16 or offset + length > len(packet):
                    self.lost = True
                    break
                if message_type == 2:  # NLMSG_ERROR; zero is the subscription ACK.
                    if length < 20 or struct.unpack_from("=i", packet, offset + 16)[0] != 0:
                        self.lost = True
                elif length >= 68:
                    what = struct.unpack_from("=I", packet, offset + 36)[0]
                    if what == PROC_EVENT_FORK:
                        parent_pid, parent_tgid, child_pid, child_tgid = struct.unpack_from("=IIII", packet, offset + 52)
                        with self.lock:
                            parent_owned = parent_tgid in self.lineage
                        # A thread clone has child_pid != child_tgid and remains
                        # owned by its existing process TGID. Only register a
                        # new process leader.
                        if parent_owned and child_pid == child_tgid:
                            self._register_process(child_tgid)
                    elif what == PROC_EVENT_EXIT:
                        process_pid, process_tgid = struct.unpack_from("=II", packet, offset + 52)
                        with self.lock:
                            owned = self.identities.get(process_tgid)
                        # Thread and leader-TID exits do not imply that the
                        # process is gone. A pidfd becomes readable only after
                        # the complete thread group exits.
                        # An unsignalable short-lived intermediate remains a
                        # lineage node until its ordered leader EXIT is
                        # consumed. For an owned process, pidfd readiness avoids
                        # confusing leader-thread exit with process exit.
                        if (owned is None and process_pid == process_tgid) or (
                            owned is not None and self._pidfd_exited(owned.pidfd)
                        ):
                            self._forget_process(process_tgid)
                offset += (length + 3) & ~3

    def live(self) -> dict[int, int]:
        result: dict[int, int] = {}
        with self.lock:
            snapshot = list(self.identities.items())
        for pid, owned in snapshot:
            if self._pidfd_exited(owned.pidfd):
                continue
            current = process_identity(pid)
            if current == owned.starttime:
                result[pid] = owned.starttime
            elif current is not None and current != owned.starttime:
                self.lost = True
        return result

    def rss_kib(self) -> int:
        return sum(process_rss_kib(pid) for pid in self.live())

    def _signal_process(self, pid: int, owned: OwnedProcess, signal_number: int) -> None:
        if process_identity(pid) != owned.starttime:
            self.lost = True
            return
        try:
            signal.pidfd_send_signal(owned.pidfd, signal_number)
        except ProcessLookupError:
            pass
        except OSError:
            self.lost = True

    def terminate(self, timeout: float) -> tuple[int, bool]:
        initial = len(self.live())
        for signal_number in (15, 9):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                live = self.live()
                with self.lock:
                    lineage_empty = not self.lineage
                if not live and lineage_empty:
                    return initial, not self.lost
                with self.lock:
                    snapshot = [(pid, self.identities.get(pid)) for pid in live]
                for pid, owned in snapshot:
                    if owned is None:
                        continue
                    # Revalidate the numeric identity immediately before the
                    # pidfd syscall. The pidfd itself pins the kernel process,
                    # so PID reuse can neither redirect nor revive authority.
                    self._signal_process(pid, owned, signal_number)
                time.sleep(0.01)
        with self.lock:
            lineage_empty = not self.lineage
        return initial, not self.live() and lineage_empty and not self.lost

    def close(self) -> None:
        self.stopping.set()
        self.socket.close()
        if self.reader_started:
            self.reader.join(timeout=1)
        with self.lock:
            owned = list(self.identities.values())
            self.identities.clear()
            self.lineage.clear()
        for process in owned:
            os.close(process.pidfd)


def residue(root: Path) -> tuple[int, int]:
    sockets = 0
    for directory, names, files in os.walk(root, followlinks=False):
        for name in names + files:
            try:
                sockets += int(stat.S_ISSOCK(os.lstat(Path(directory) / name).st_mode))
            except FileNotFoundError:
                pass
    mounts = 0
    prefix = str(root) + os.sep
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        fields = line.split()
        if len(fields) > 4 and (fields[4] == str(root) or fields[4].startswith(prefix)):
            mounts += 1
    return sockets, mounts


def copy_failure_artifacts(source: Path, destination: Path, count_limit: int, byte_limit: int) -> None:
    if not source.exists():
        return
    count = 0
    size = 0
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.is_symlink():
            continue
        next_size = path.stat().st_size
        if count + 1 > count_limit or size + next_size > byte_limit:
            raise RuntimeError("failure artifact budget exceeded")
        shutil.copyfile(path, destination / path.name)
        count += 1
        size += next_size


def write_result(path: Path | None, result: dict[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--rss-mib", type=int, default=4096)
    parser.add_argument("--output-bytes", type=int, default=4_194_304)
    parser.add_argument("--cleanup-seconds", type=int, default=30)
    parser.add_argument("--artifact-count", type=int, default=32)
    parser.add_argument("--artifact-bytes", type=int, default=67_108_864)
    parser.add_argument("--result", type=Path)
    parser.add_argument("--failure-artifacts", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    limits = (args.timeout_seconds, args.rss_mib, args.output_bytes, args.cleanup_seconds, args.artifact_count, args.artifact_bytes)
    if not command or any(value <= 0 for value in limits):
        parser.error("a command and positive budgets are required")

    task_root = Path(tempfile.mkdtemp(prefix="lifecycle-lab-", dir=os.environ.get("TMPDIR", "/tmp"))).resolve()
    staging = task_root / "artifacts"
    environment = os.environ.copy()
    environment.update({
        "LIFECYCLE_LAB_TASK_ROOT": str(task_root),
        "LIFECYCLE_LAB_ARTIFACT_STAGING": str(staging),
    })
    started = time.monotonic()
    ownership: ProcessOwnership | None = None
    try:
        ownership = ProcessOwnership()
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except OSError as error:
        if ownership is not None:
            ownership.close()
        shutil.rmtree(task_root, ignore_errors=True)
        result = {
            "status": "FAIL", "reason": "process-authority", "exit_code": 125,
            "elapsed_ms": int((time.monotonic() - started) * 1000), "peak_rss_kib": 0,
            "cleanup": {"processes": 0, "ledger_empty": False, "sockets": 0, "mounts": 0, "root": int(task_root.exists())},
        }
        write_result(args.result, result)
        print(f"lifecycle lab process authority unavailable: {error}", file=sys.stderr)
        print("LIFECYCLE_LAB_RESULT status=FAIL reason=process-authority exit_code=125")
        return 125
    ownership.add_root(process.pid)
    assert process.stdout is not None
    os.set_blocking(process.stdout.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    output = bytearray()
    reason = "command"
    peak_rss = 0
    try:
        while process.poll() is None or process.stdout.fileno() in selector.get_map():
            peak_rss = max(peak_rss, ownership.rss_kib())
            if peak_rss > args.rss_mib * 1024:
                reason = "rss"
                break
            if time.monotonic() - started > args.timeout_seconds:
                reason = "timeout"
                break
            events = selector.select(0.1) if selector.get_map() else []
            if not events and process.poll() is not None:
                if selector.get_map():
                    chunk = process.stdout.read()
                    if chunk:
                        output.extend(chunk)
                break
            for key, _ in events:
                chunk = key.fileobj.read(65536)
                if not chunk:
                    selector.unregister(key.fileobj)
                else:
                    output.extend(chunk)
            if len(output) > args.output_bytes:
                del output[args.output_bytes:]
                reason = "output"
                break
    except KeyboardInterrupt:
        reason = "interrupted"
    finally:
        selector.close()
        process.stdout.close()

    peak_rss = max(peak_rss, ownership.rss_kib())
    cleanup_started = time.monotonic()
    cleaned, cleanup_ok = ownership.terminate(float(args.cleanup_seconds) / 2)
    ownership.close()
    try:
        remaining = max(0.01, float(args.cleanup_seconds) - (time.monotonic() - cleanup_started))
        process.wait(timeout=remaining)
    except subprocess.TimeoutExpired:
        cleanup_ok = False
    sockets, mounts = residue(task_root)
    command_rc = process.returncode
    status = "PASS" if reason == "command" and command_rc == 0 and cleanup_ok and not sockets and not mounts else "FAIL"
    exit_code = command_rc if reason == "command" and command_rc is not None else 124
    if exit_code is not None and exit_code < 0:
        exit_code = 128 - exit_code
    if not cleanup_ok or sockets or mounts:
        exit_code, status, reason = 125, "FAIL", "cleanup"

    if status == "FAIL" and args.failure_artifacts:
        args.failure_artifacts.mkdir(parents=True, exist_ok=True)
        (args.failure_artifacts / "command.log").write_bytes(output)
        try:
            copy_failure_artifacts(staging, args.failure_artifacts, args.artifact_count - 1, args.artifact_bytes - len(output))
        except RuntimeError:
            exit_code, reason = 125, "artifact-budget"
    try:
        shutil.rmtree(task_root)
    except OSError:
        exit_code, status, reason = 125, "FAIL", "cleanup"

    write_result(args.result, {
        "status": status, "reason": reason, "exit_code": exit_code,
        "elapsed_ms": int((time.monotonic() - started) * 1000), "peak_rss_kib": peak_rss,
        "cleanup": {"processes": cleaned, "ledger_empty": cleanup_ok, "sockets": sockets, "mounts": mounts, "root": int(task_root.exists())},
    })
    sys.stdout.buffer.write(output)
    print(f"LIFECYCLE_LAB_RESULT status={status} reason={reason} exit_code={exit_code}")
    return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
