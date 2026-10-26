#!/usr/bin/env python3
"""Launch and sample one exclusively owned Darling guest (diagnostic, not acceptance).

Interface: --runtime-root R --prefix RAW_P [--wait SECONDS] [--interval SECONDS]
[--comm NAME]... [--env K=V]... [--cmd COMMAND] [--log PATH]
[--page HEX | --page-file PATH] [--page-offsets 0,4,8,16,24].

R is an ordinary installation, P is disjoint writable guest state. The raw prefix
is validated before any normalization. Default evidence is a unique directory in
DW_STATE_ROOT/diagnostics (otherwise the workspace .west-state/diagnostics), never
/tmp. --log selects a new guest-output file; adjacent .samples.jsonl and .report
files preserve full observations and lifecycle evidence. Sampling may miss an
entire short-lived image; absent data is not proof that it never loaded. Exit zero
means the diagnostic command and normal cleanup completed, not workload PASS.
"""

import argparse
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE / "west_commands"))
from runtime_install import resolve_runtime
from test_prefix import PrefixLifecycleOwner
from prefix_repair import cleanup_prefix_mounts, darling_init_pid_is_usable

DESCRIBE = WORKSPACE / "scripts" / "darling-describe-artifact.sh"
IDENTITY_KEYS = {"DPREFIX", "DARLING_PREFIX", "DARLING_RUNTIME_ROOT", "DARLING_LAUNCHER", "DARLING"}


def read(path, binary=False):
    """Keep unavailable /proc fields distinct from an observed empty field."""
    try:
        if binary:
            return Path(path).read_bytes().replace(b"\0", b" ").decode("utf8", "replace").strip()
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return None


def memory(pid, address, size):
    try:
        with open(f"/proc/{pid}/mem", "rb", buffering=0) as stream:
            stream.seek(address)
            raw = stream.read(size)
        return {"address": hex(address), "requested": size, "bytes": len(raw),
                "hex": raw.hex(), "error": None if len(raw) == size else "short read"}
    except (OSError, ValueError, OverflowError) as error:
        return {"address": hex(address), "requested": size, "bytes": 0, "hex": "", "error": str(error)}


def syscall_info(pid, path, maps_raw):
    info = {"raw": None, "pc": None, "sp": None, "pc_where": None}
    try:
        raw = Path(path).read_text().strip()
    except OSError as error:
        info["unavailable"] = str(error)
        return info
    info["raw"] = raw
    parts = raw.split()
    if len(parts) < 9:
        return info
    try:
        info["pc"], info["sp"] = int(parts[-1], 0), int(parts[-2], 0)
        for line in (maps_raw or "").splitlines():
            fields = line.split(None, 5)
            lo, hi = (int(value, 16) for value in fields[0].split("-"))
            if lo <= info["pc"] < hi:
                info["pc_where"] = {"map": line, "file_offset": int(fields[2], 16) + info["pc"] - lo}
                break
        # Linux x86_64 ABI only. Preserve the raw syscall even for unknown ABIs.
        if os.uname().machine == "x86_64":
            argument = {"202": 4, "35": 1, "230": 3, "449": 4}.get(parts[0])
            if argument is not None:
                pointer = int(parts[argument], 0)
                info["timeout_pointer"] = parts[argument]
                if pointer:
                    data = memory(pid, pointer, 16)
                    info["timeout"] = data
                    if data["bytes"] == 16:
                        raw16 = bytes.fromhex(data["hex"])
                        data["sec"] = int.from_bytes(raw16[:8], "little", signed=True)
                        data["nsec"] = int.from_bytes(raw16[8:], "little", signed=True)
    except (ValueError, IndexError) as error:
        info["parse_error"] = str(error)
    return info


def sample(owned_rows, comms, page_address, offsets):
    """Sample only the common ownership oracle's PIDs; comm is just a filter."""
    observations = []
    for row in owned_rows:
        pid = row.split(" ", 1)[0]
        directory = Path("/proc") / pid
        comm = read(directory / "comm")
        if comms is not None and comm not in comms:
            continue
        stat = read(directory / "stat")
        fields = stat.rsplit(")", 1)[-1].split() if stat and ")" in stat else []
        if len(fields) < 20:
            observations.append({"pid": pid, "comm": comm, "ownership": row,
                                 "missing": "process disappeared before stat"})
            continue
        maps_raw = read(directory / "maps")
        try:
            exe = os.readlink(directory / "exe")
        except OSError as error:
            exe = {"unavailable": str(error)}
        info = {"pid": pid, "starttime": fields[19], "comm": comm, "ownership": row,
                "cmdline": read(directory / "cmdline", binary=True), "exe": exe,
                "state": fields[0], "maps_raw": maps_raw,
                "syscall": syscall_info(pid, directory / "syscall", maps_raw),
                # stat field 52: wait status, not a guessed disappearance exit code.
                "exit_code": fields[49] if len(fields) > 49 and fields[0] in {"Z", "X"} else None,
                "threads": {}}
        try:
            threads = sorted((directory / "task").iterdir(), key=lambda entry: int(entry.name))
        except OSError as error:
            threads = []
            info["threads_error"] = str(error)
        for thread in threads:
            info["threads"][thread.name] = syscall_info(pid, thread / "syscall", maps_raw)
        if page_address is not None:
            data = memory(pid, page_address, 128)
            raw = bytes.fromhex(data["hex"])
            data["fields"] = {str(offset): int.from_bytes(raw[offset:offset + 4], "little")
                              for offset in offsets if offset + 4 <= len(raw)}
            info["page"] = data
        observations.append(info)
    return observations


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runtime-root", help="ordinary installed runtime R (or DARLING_RUNTIME_ROOT)")
    ap.add_argument("--prefix", required=True, help="raw absolute writable guest prefix P")
    ap.add_argument("--wait", type=float, default=45.0, help="maximum sampling duration")
    ap.add_argument("--interval", type=float, default=0.05)
    ap.add_argument("--comm", action="append", help="optional owned process comm filter (default: all verified owned processes)")
    ap.add_argument("--env", action="append", default=[], help="explicit diagnostic KEY=VALUE; cannot change P/R identity")
    ap.add_argument("--cmd", default="echo HELLO=1")
    pages = ap.add_mutually_exclusive_group()
    pages.add_argument("--page")
    pages.add_argument("--page-file")
    ap.add_argument("--page-offsets", default="0,4,8,16,24")
    ap.add_argument("--log", help="new durable guest log path (will not overwrite existing evidence)")
    args = ap.parse_args()
    comms = set(args.comm) if args.comm else None
    if not all(math.isfinite(value) and value > 0 for value in (args.wait, args.interval)):
        ap.error("--wait and --interval must be finite and positive")
    try:
        offsets = [int(value) for value in args.page_offsets.split(",")]
        if any(value < 0 or value + 4 > 128 for value in offsets):
            raise ValueError("page offsets must select a four-byte word within 128 bytes")
        page_address = int(args.page, 16) if args.page else None
        if page_address is not None and page_address < 0:
            raise ValueError("--page must be nonnegative")
        runtime = resolve_runtime(args.runtime_root)
        # environment() guards the original spelling, combined images and P/R overlap.
        selected = runtime.environment(args.prefix)
        base = dict(os.environ)
        for setting in args.env:
            if "=" not in setting:
                raise ValueError(f"--env wants KEY=VALUE, got {setting!r}")
            key, value = setting.split("=", 1)
            if not key or "\0" in setting:
                raise ValueError("invalid --env setting")
            if key in IDENTITY_KEYS and value != selected[key]:
                raise ValueError(f"--env {key} cannot redirect selected runtime/prefix identity")
            base[key] = value
        env = runtime.environment(args.prefix, base)
        state = Path(os.environ.get("DW_STATE_ROOT", str(WORKSPACE.parent / ".west-state")))
        if not state.is_absolute():
            raise ValueError("DW_STATE_ROOT must be absolute")
        if args.log:
            log = Path(args.log)
        else:
            root = state / "diagnostics"
            if root.resolve().is_relative_to(Path("/tmp")):
                raise ValueError("diagnostic evidence must not be under /tmp")
            root.mkdir(parents=True, exist_ok=True)
            log = Path(tempfile.mkdtemp(prefix="trace-guest-", dir=root)) / "guest.log"
        if not log.is_absolute() or log.resolve().is_relative_to(Path("/tmp")):
            raise ValueError("--log must be an absolute durable path outside /tmp")
        log.parent.mkdir(parents=True, exist_ok=True)
    except (OSError, ValueError) as error:
        ap.error(str(error))

    # All files are exclusive: a repeated explicit path cannot destroy evidence.
    with log.open("x") as guest, Path(str(log) + ".samples.jsonl").open("x") as samples, \
            Path(str(log) + ".report").open("x") as report:
        errors = []

        def emit(message):
            # Losing an observer pipe or filling the evidence volume must never
            # short-circuit the lifecycle owner's cleanup while it holds P.
            for stream in (sys.stdout, report):
                try:
                    print(message, file=stream, flush=True)
                except OSError as error:
                    errors.append(f"evidence output unavailable: {error}")

        def err(message):
            errors.append(message)
            emit(message)

        owner = PrefixLifecycleOwner(
            resolve_launcher=lambda _prefix: str(runtime.launcher),
            prefix_env=lambda raw: runtime.environment(raw, base),
            cleanup_mounts=cleanup_prefix_mounts, init_pid_is_usable=darling_init_pid_is_usable,
            inf=emit, err=err, wrn=emit)
        emit(f"run log: {log}")
        emit(f"samples: {samples.name}")
        emit(f"runtime-root: {runtime.root}; prefix: {args.prefix}")
        emit("installation-identity: " + json.dumps(runtime.summary(), sort_keys=True))
        seen = {}
        proc = None
        status = 1
        cleanup_ok = False
        started = time.monotonic()
        old_handlers = {}

        def interrupt(_signum, _frame):
            raise KeyboardInterrupt

        with owner.locked(args.prefix):
            try:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    old_handlers[sig] = signal.signal(sig, interrupt)
                proc = subprocess.Popen([str(runtime.launcher), "shell", "/bin/sh", "-c", args.cmd],
                                        env=env, stdout=guest, stderr=subprocess.STDOUT)
                emit("command: " + json.dumps(proc.args))
                deadline = time.monotonic() + args.wait
                while True:
                    now = time.monotonic()
                    address = page_address
                    page_error = None
                    if args.page_file:
                        try:
                            address = int(Path(args.page_file).read_text().strip(), 16)
                            if address < 0:
                                raise ValueError("negative page address")
                        except (OSError, ValueError) as error:
                            address = None
                            page_error = str(error)
                    owned = owner.process_snapshot(Path(args.prefix))
                    observations = sample(owned, comms, address, offsets)
                    json.dump({"elapsed": now - started, "owned": owned, "page_file_error": page_error,
                               "launcher_rc": proc.poll(), "observations": observations}, samples)
                    samples.write("\n")
                    samples.flush()
                    for info in observations:
                        if "starttime" not in info:
                            continue
                        key = (info["pid"], info["starttime"])
                        rec = seen.setdefault(key, {"comm": info["comm"], "samples": 0, "states": set(),
                                                    "maps": set(), "maps_raw": set(), "exit_code": None})
                        rec["samples"] += 1
                        rec["states"].add(info["state"])
                        rec["exit_code"] = info["exit_code"] if info["exit_code"] is not None else rec["exit_code"]
                        rec["last_elapsed"] = now - started
                        rec["last_syscall"] = info["syscall"]
                        rec["last_threads"] = info["threads"]
                        for line in (info["maps_raw"] or "").splitlines():
                            rec["maps_raw"].add(line)
                            fields = line.split(None, 5)
                            if len(fields) == 6 and fields[5].startswith("/"):
                                rec["maps"].add(fields[5])
                    if now >= deadline:
                        status = proc.poll()
                        if status is None:
                            status = 124
                            emit("sampling deadline reached with launcher still running (timeout, not acceptance)")
                        break
                    time.sleep(min(args.interval, max(0, deadline - time.monotonic())))
            except KeyboardInterrupt:
                status = 130
                emit("interrupted; retaining evidence and shutting down selected prefix")
            except Exception as error:
                status = 1
                err(f"trace error: {type(error).__name__}: {error}")
            finally:
                # A second interrupt must not release the lease halfway through cleanup.
                for sig in old_handlers:
                    signal.signal(sig, signal.SIG_IGN)
                try:
                    cleanup_ok = owner.shutdown(Path(args.prefix))
                except Exception as error:
                    err(f"cleanup error: {type(error).__name__}: {error}")
                    try:
                        owner.finalize(Path(args.prefix))
                    except Exception as rescue_error:
                        err(f"rescue cleanup error: {type(rescue_error).__name__}: {rescue_error}")
                finally:
                    if proc is not None:
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            err("launcher did not exit after prefix cleanup; bounded wait exhausted")
                        emit(f"launcher exit status: {proc.poll()} (None means unobserved)")
                    emit(f"normal cleanup: {cleanup_ok}; lifecycle errors: {len(errors)}")
                    for sig, handler in old_handlers.items():
                        signal.signal(sig, handler)
        emit(f"owned runtime processes observed: {len(seen)}")
        if not seen:
            emit("missing data: no selected owned process sampled; short-lived images may have been missed")
        described = set()
        for (pid, starttime), rec in seen.items():
            emit(f"pid={pid} starttime={starttime} comm={rec['comm']} samples={rec['samples']} "
                 f"states={''.join(sorted(rec['states']))} exit_code={rec['exit_code']} "
                 "(None means no exit status sampled)")
            emit(f"  last-sample-elapsed: {rec['last_elapsed']:.3f}s")
            emit("  syscall: " + json.dumps(rec["last_syscall"], sort_keys=True))
            for tid, state in rec["last_threads"].items():
                emit(f"  thread={tid} syscall: " + json.dumps(state, sort_keys=True))
            for line in sorted(rec["maps_raw"]):
                emit("  map: " + line)
            for path in sorted(rec["maps"]):
                if path in described:
                    continue
                described.add(path)
                if not Path(path).is_file():
                    emit(f"  artifact unavailable: {path}")
                    continue
                try:
                    result = subprocess.run(["sh", str(DESCRIBE), path], capture_output=True,
                                            text=True, timeout=15)
                    emit(f"  describe-artifact: {path}; rc={result.returncode}")
                    for line in (result.stdout + result.stderr).splitlines():
                        emit("    " + line)
                except (OSError, subprocess.TimeoutExpired) as error:
                    emit(f"  describe-artifact unavailable: {path}: {error}")
        emit("diagnostic sampling only; full per-thread syscall/PC/SP, timeout bytes and page timeline are in samples")
        return status if status else (0 if cleanup_ok and not errors else 1)


if __name__ == "__main__":
    sys.exit(main())
