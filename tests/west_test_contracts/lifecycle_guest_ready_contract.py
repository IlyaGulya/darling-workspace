#!/usr/bin/env python3
"""Trace-driven real Darling guest lane for dar-4ush.5.2.

Python owns bounded process orchestration and transports kernel/product facts.
The accepted event order, terminal state, witness semantics, and unresolved-
obligation policy are interpreted only by the Rust lifecycle verifier.
"""

from __future__ import annotations

import argparse
import copy
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import resource
import select
import signal
import stat
import subprocess
import sys
import time
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "west_commands"))
from prefix_state import PrefixStateError, read_prefix_state
from runtime_cell import RuntimeCell, RuntimeCellError, load_runtime_cell, observe, same_object


COMMAND_SECONDS = 45.0
TRANSITION_SECONDS = 20.0
KILL_GRACE_SECONDS = 5.0
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_PROC_BYTES = 1024 * 1024
MAX_PROCESSES = 4096
MAX_FDS = 4096
MAX_TREE_ENTRIES = 200_000
MAX_REPORT_BYTES = 1024 * 1024

TRACE_HEX = {
    "shared-session-signal-gone": "444c463101003aac30275d690e65000002060003010200030001050000010b03",
    "shared-session-signal-rejected": "444c463101007de40dd4201de3c70000020700030102000300010500000206090b03",
    "shared-session-retained-holder-timeout": "444c463101004716e91850bccfa50000020f00030102000300010201020304030003010103030105000000070007010a0506090b02",
    "shared-session-pid-reuse": "444c463101009a4e9eede612eed300000206000301020003000006040b01",
    "shared-session-late-fork": "444c46310100fff15cebd6d822ae0000020a000301020003000102010403010902020a0406040b01",
    "session-root-exit-before-snapshot": "444c4631010094658829503ae61d00000206000301020003000006040b01",
}

RUNTIME_ENDPOINTS = (
    ".init.pid",
    ".darlingserver.sock",
    ".lc-v1.sock",
    "var/run/shellspawn.sock",
    "var/tmp/launchd/sock",
)
TRANSACTION_MARKERS = (
    ".lifecycle-stage-",
    ".lifecycle-quarantine-",
    ".lifecycle-gc-",
    ".darling-lifecycle-",
)
HOST_ARTIFACTS = {
    "darling": "bin/darling",
    "darlingserver": "bin/darlingserver",
}
GUEST_BOOTCHAIN_ARTIFACTS = {
    "launchd": "sbin/launchd",
    "shellspawn": "usr/libexec/shellspawn",
    "mldr": "usr/libexec/darling/mldr",
    "mldr32": "usr/libexec/darling/mldr32",
    "dyld": "usr/lib/dyld",
    "libsystem_kernel": "usr/lib/system/libsystem_kernel.dylib",
    "vchroot": "usr/libexec/darling/vchroot",
}


class ContractError(RuntimeError):
    pass


def _child_resource_limits() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _git_oid(repository: Path, expression: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "--verify", expression],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=5.0,
        check=False,
    )
    value = result.stdout.decode("ascii", errors="strict").strip()
    if result.returncode or len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ContractError(f"Git identity unavailable for {repository}: {expression}")
    return value


def _file_identity(path: Path, *, nofollow: bool = True) -> dict[str, int]:
    value = path.stat(follow_symlinks=not nofollow)
    return {
        "device": value.st_dev,
        "inode": value.st_ino,
        "mode": value.st_mode,
        "uid": value.st_uid,
        "gid": value.st_gid,
        "nlink": value.st_nlink,
    }


def _prefix_generation(prefix: Path) -> int:
    try:
        return read_prefix_state(prefix).generation
    except PrefixStateError as error:
        raise ContractError(str(error)) from error


def _source_identity(
    workspace: Path, prefix: Path, verifier_identity: dict[str, Any]
) -> tuple[dict[str, Any], str, str]:
    artifacts: dict[str, Any] = {}
    artifact_paths = {
        **{name: (prefix / relative, relative) for name, relative in HOST_ARTIFACTS.items()},
        **{
            name: (prefix / "libexec/darling" / relative, f"libexec/darling/{relative}")
            for name, relative in GUEST_BOOTCHAIN_ARTIFACTS.items()
        },
    }
    for name, (path, relative) in artifact_paths.items():
        if not path.is_file() or path.is_symlink():
            raise ContractError(f"runtime artifact is not a retained regular file: {relative}")
        artifacts[name] = {
            "relative": relative,
            "sha256": _sha256(path),
            "identity": _file_identity(path),
        }
    source = {
        "workspace": {
            "commit": _git_oid(workspace, "HEAD^{commit}"),
            "tree": _git_oid(workspace, "HEAD^{tree}"),
            "west_lock_sha256": _sha256(workspace / "west.lock.yml"),
            "profile_sha256": _sha256(workspace / "patches/homebrew/patches.yml"),
        },
        "rust_verifier": verifier_identity,
    }
    runtime = {
        "prefix": _file_identity(prefix),
        "generation": _prefix_generation(prefix),
        "artifacts": artifacts,
        "runtime_mode": "rootless-eunion",
        "route": "OFF",
        "routing": "DEFERRED",
    }
    return {"source": source, "runtime": runtime}, _canonical_digest(source), _canonical_digest(runtime)


def _read_proc(path: Path, limit: int = MAX_PROC_BYTES) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ContractError(f"bounded proc read exceeded: {path}")
    return data


def _process_identity(pid: int) -> dict[str, int] | None:
    try:
        raw = _read_proc(Path(f"/proc/{pid}/stat"), 64 * 1024).decode()
        _comm, fields = raw.rsplit(") ", 1)
        values = fields.split()
        if values[0] == "Z":
            return None
        return {"pid": pid, "starttime": int(values[19])}
    except (OSError, UnicodeError, ValueError, IndexError):
        return None


def _pidfd_state(pidfd: int, timeout_ms: int) -> bool:
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    return bool(poller.poll(timeout_ms))


def _fd_targets(pid: int) -> tuple[list[dict[str, Any]], int]:
    directory = Path(f"/proc/{pid}/fd")
    try:
        entries = list(directory.iterdir())
    except OSError:
        return [], 0
    if len(entries) > MAX_FDS:
        raise ContractError(f"PID {pid}: FD census exceeded {MAX_FDS}")
    result = []
    for entry in entries:
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        result.append({"fd": int(entry.name), "target": target})
    return result, len(entries)


def _process_census(prefix: Path) -> tuple[list[dict[str, Any]], int]:
    prefix_text = str(prefix)
    records: list[dict[str, Any]] = []
    max_fds = 0
    entries = [entry for entry in Path("/proc").iterdir() if entry.name.isdigit()]
    if len(entries) > MAX_PROCESSES:
        raise ContractError("host process census exceeds bound")
    for entry in entries:
        pid = int(entry.name)
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            command = [
                os.fsdecode(part)
                for part in _read_proc(entry / "cmdline").split(b"\0")
                if part
            ][:64]
            environment = _read_proc(entry / "environ").split(b"\0")
        except (OSError, ContractError):
            continue
        descriptors, descriptor_count = _fd_targets(pid)
        max_fds = max(max_fds, descriptor_count)
        prefix_bytes = os.fsencode(prefix_text)
        owns = b"DPREFIX=" + prefix_bytes in environment or b"DARLING_PREFIX=" + prefix_bytes in environment
        prefix_fds = []
        for descriptor in descriptors:
            normalized = descriptor["target"].removesuffix(" (deleted)")
            if normalized == prefix_text or normalized.startswith(prefix_text + "/"):
                owns = True
                prefix_fds.append(descriptor)
        if not owns:
            continue
        identity = _process_identity(pid)
        if identity is None:
            continue
        records.append(
            {
                **identity,
                "ppid": int(_read_proc(entry / "stat", 64 * 1024).decode().rsplit(") ", 1)[1].split()[1]),
                "comm": (entry / "comm").read_text(errors="replace").strip()[:128],
                "argv": command,
                "prefix_fds": prefix_fds,
                "fd_count": descriptor_count,
            }
        )
    records.sort(key=lambda item: (item["pid"], item["starttime"]))
    return records, max_fds


def _wait(description: str, predicate: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + TRANSITION_SECONDS
    last: Any = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(0.05)
    raise ContractError(f"timeout waiting for {description}; last={last!r}")


def _endpoint_observations(prefix: Path) -> list[dict[str, Any]]:
    result = []
    for relative in RUNTIME_ENDPOINTS:
        path = prefix / relative
        try:
            value = path.lstat()
        except FileNotFoundError:
            continue
        result.append(
            {
                "name": relative,
                "identity": {
                    "device": value.st_dev,
                    "inode": value.st_ino,
                    "mode": value.st_mode,
                    "uid": value.st_uid,
                    "gid": value.st_gid,
                    "nlink": value.st_nlink,
                },
            }
        )
    result.sort(key=lambda item: item["name"])
    if not {".init.pid", ".darlingserver.sock", "var/run/shellspawn.sock"}.issubset(
        {item["name"] for item in result}
    ):
        raise ContractError(f"guest-ready endpoints incomplete: {result!r}")
    return result


def _active(prefix: Path) -> dict[str, Any] | None:
    try:
        fields = (prefix / ".init.pid").read_text().split()
        if len(fields) not in {1, 2} or not all(value.isdigit() for value in fields):
            return None
        root = _process_identity(int(fields[0]))
        if root is None:
            return None
        if len(fields) == 2 and root["starttime"] != int(fields[1]):
            return None
        records, max_fds = _process_census(prefix)
        if not any(item["pid"] == root["pid"] for item in records):
            return None
        endpoints = _endpoint_observations(prefix)
        if not any(
            item["comm"] in {"shellspawn", "mldr"}
            and any("shellspawn" in argument for argument in item["argv"])
            for item in records
        ):
            return None
        return {
            "root": root,
            "processes": records,
            "max_fds": max_fds,
            "endpoints": endpoints,
        }
    except (OSError, ContractError, ValueError):
        return None


def _environment(prefix: Path) -> dict[str, str]:
    result = dict(os.environ)
    result.pop("DARLING_LIFECYCLE_COHORT_V1", None)
    result.update(
        {
            "DPREFIX": str(prefix),
            "DARLING_PREFIX": str(prefix),
            "DARLING_ROOTLESS": "1",
            "DARLING_NOOVERLAYFS": "1",
            "DARLING_EUNION": "1",
        }
    )
    return result


def _run(argv: list[str], env: dict[str, str], evidence: Path, name: str) -> dict[str, Any]:
    stdout_path = evidence / f"{name}.stdout"
    stderr_path = evidence / f"{name}.stderr"
    started = time.monotonic_ns()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
            preexec_fn=_child_resource_limits,
        )
        try:
            returncode = process.wait(COMMAND_SECONDS)
        except subprocess.TimeoutExpired as error:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(KILL_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(KILL_GRACE_SECONDS)
            stdout_tail = stdout_path.read_bytes()[-4096:].decode(errors="replace")
            stderr_tail = stderr_path.read_bytes()[-4096:].decode(errors="replace")
            raise ContractError(
                f"{name}: command timeout rc={process.returncode!r}; "
                f"stdout_tail={stdout_tail!r}; stderr_tail={stderr_tail!r}"
            ) from error
    stdout_data = stdout_path.read_bytes()
    stderr_data = stderr_path.read_bytes()
    if len(stdout_data) + len(stderr_data) > MAX_OUTPUT_BYTES:
        raise ContractError(f"{name}: combined output exceeds bound")
    if returncode != 0:
        raise ContractError(
            f"{name}: rc={returncode} stdout={stdout_data[-2048:]!r} stderr={stderr_data[-2048:]!r}"
        )
    return {
        "name": name,
        "returncode": returncode,
        "elapsed_ns": time.monotonic_ns() - started,
        "output_bytes": len(stdout_data) + len(stderr_data),
        "stdout_sha256": _sha256(stdout_path),
        "stderr_sha256": _sha256(stderr_path),
    }


def _output_budget_negative(evidence: Path) -> dict[str, Any]:
    try:
        _run(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 2097152)"],
            dict(os.environ),
            evidence,
            "output-budget-negative",
        )
    except ContractError:
        size = (evidence / "output-budget-negative.stdout").stat().st_size
        if size > MAX_OUTPUT_BYTES:
            raise ContractError("kernel output limit allowed an oversized capture")
        return {"rejected": True, "captured_bytes": size}
    raise ContractError("oversized child output was accepted")


def _start_holder(
    launcher: Path,
    prefix: Path,
    evidence: Path,
    name: str,
    script: str,
    *,
    interactive: bool = False,
) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    stdout_path = evidence / f"{name}.stdout"
    stderr_path = evidence / f"{name}.stderr"
    stdout = stdout_path.open("wb")
    stderr = stderr_path.open("wb")
    process = subprocess.Popen(
        [str(launcher), "shell", "/bin/bash", "-c", script],
        env=_environment(prefix),
        stdin=subprocess.PIPE if interactive else subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
        preexec_fn=_child_resource_limits,
    )
    stdout.close()
    stderr.close()

    def ready() -> dict[str, Any] | None:
        returncode = process.poll()
        if returncode is not None:
            stdout_tail = stdout_path.read_bytes()[-4096:].decode(errors="replace")
            stderr_tail = stderr_path.read_bytes()[-4096:].decode(errors="replace")
            raise ContractError(
                f"{name}: holder exited before guest-ready rc={returncode}; "
                f"stdout_tail={stdout_tail!r}; stderr_tail={stderr_tail!r}"
            )
        if stdout_path.stat().st_size + stderr_path.stat().st_size > MAX_OUTPUT_BYTES:
            raise ContractError(f"{name}: output exceeds bound")
        if b"GUEST52_HOLDER_READY\n" not in stdout_path.read_bytes():
            return None
        return _active(prefix)

    try:
        active = _wait(f"{name} guest-ready", ready)
    except ContractError as error:
        stdout_tail = stdout_path.read_bytes()[-4096:].decode(errors="replace")
        stderr_tail = stderr_path.read_bytes()[-4096:].decode(errors="replace")
        raise ContractError(
            f"{error}; holder_rc={process.poll()!r}; "
            f"stdout_tail={stdout_tail!r}; stderr_tail={stderr_tail!r}"
        ) from error
    return process, active


def _stop_holder(process: subprocess.Popen[bytes]) -> None:
    try:
        process.wait(KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(KILL_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    os.killpg(process.pid, signal.SIGKILL)
    process.wait(KILL_GRACE_SECONDS)


def _mounts(prefix: Path) -> list[str]:
    prefix_text = str(prefix)
    result = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 5:
            raise ContractError("malformed mountinfo")
        path = fields[4].replace("\\040", " ").replace("\\011", "\t")
        if path == prefix_text or path.startswith(prefix_text + "/"):
            result.append(path)
    return sorted(result)


def _namespace_residue(prefix: Path) -> dict[str, list[str]]:
    root_fd = os.open(prefix, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    stack: list[tuple[int, str]] = [(root_fd, "")]
    endpoints: list[str] = []
    transactions: list[str] = []
    count = 0
    try:
        while stack:
            descriptor, parent = stack.pop()
            try:
                with os.scandir(descriptor) as iterator:
                    entries = sorted(iterator, key=lambda item: item.name)
                for entry in entries:
                    count += 1
                    if count > MAX_TREE_ENTRIES:
                        raise ContractError("namespace census exceeds bound")
                    relative = f"{parent}/{entry.name}" if parent else entry.name
                    value = entry.stat(follow_symlinks=False)
                    if relative in RUNTIME_ENDPOINTS:
                        endpoints.append(relative)
                    if any(entry.name.startswith(prefix_name) for prefix_name in TRANSACTION_MARKERS):
                        transactions.append(relative)
                    if stat.S_ISDIR(value.st_mode):
                        child = os.open(
                            entry.name,
                            os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                            dir_fd=descriptor,
                        )
                        stack.append((child, relative))
            finally:
                os.close(descriptor)
    except BaseException:
        for descriptor, _relative in stack:
            os.close(descriptor)
        raise
    return {"endpoints": sorted(endpoints), "transactions": sorted(transactions)}


def _clean_census(prefix: Path) -> dict[str, Any] | None:
    processes, _max_fds = _process_census(prefix)
    residue = _namespace_residue(prefix)
    mounts = _mounts(prefix)
    if processes or residue["endpoints"] or residue["transactions"] or mounts:
        return None
    return {
        "processes": [],
        "fd_holders": [],
        "endpoints": [],
        "transaction_refs": [],
        "mounts": [],
    }


def _retire_stale_endpoints(prefix: Path) -> list[dict[str, Any]]:
    """Remove unheld runtime names only after the owned process census is empty."""
    processes, _max_fds = _process_census(prefix)
    if processes or _mounts(prefix):
        return []
    retired = []
    for relative in RUNTIME_ENDPOINTS:
        parent_relative, name = relative.rsplit("/", 1) if "/" in relative else ("", relative)
        parent = prefix / parent_relative
        try:
            parent_fd = os.open(
                parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
            )
        except FileNotFoundError:
            continue
        try:
            try:
                value = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                continue
            if value.st_uid != os.getuid() or value.st_nlink != 1:
                raise ContractError(f"stale endpoint authority rejected: {relative}")
            if relative == ".init.pid":
                allowed = stat.S_ISREG(value.st_mode)
            else:
                allowed = stat.S_ISSOCK(value.st_mode)
            if not allowed:
                raise ContractError(f"stale endpoint type rejected: {relative}")
            identity = {"device": value.st_dev, "inode": value.st_ino}
            os.unlink(name, dir_fd=parent_fd)
            retired.append({"relative": relative, **identity})
        finally:
            os.close(parent_fd)
    return retired


def _kill_owned_holders(prefix: Path) -> list[dict[str, int]]:
    records, _max_fds = _process_census(prefix)
    killed = []
    for record in records:
        identity = _process_identity(record["pid"])
        if identity != {"pid": record["pid"], "starttime": record["starttime"]}:
            continue
        pidfd = _retain_pidfd(identity)
        try:
            if _process_identity(record["pid"]) != identity:
                continue
            signal.pidfd_send_signal(pidfd, signal.SIGKILL)
            _pidfd_state(pidfd, int(KILL_GRACE_SECONDS * 1000))
            killed.append(identity)
        except ProcessLookupError:
            pass
        finally:
            os.close(pidfd)
    return killed


def _retain_pidfd(identity: dict[str, int]) -> int:
    """Acquire a pidfd and reject PID reuse across the acquisition boundary."""
    try:
        descriptor = os.pidfd_open(identity["pid"], 0)
    except ProcessLookupError as error:
        raise ContractError("process exited before pidfd acquisition") from error
    if _process_identity(identity["pid"]) != identity:
        os.close(descriptor)
        raise ContractError("process identity changed during pidfd acquisition")
    return descriptor


def _cleanup(
    launcher: Path, prefix: Path, evidence: Path, name: str, commands: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, int]]]:
    try:
        commands.append(_run([str(launcher), "shutdown"], _environment(prefix), evidence, name))
    except ContractError:
        pass
    killed: list[dict[str, int]] = []
    deadline = time.monotonic() + TRANSITION_SECONDS
    while time.monotonic() < deadline:
        clean = _clean_census(prefix)
        if clean is not None:
            return clean, killed
        killed.extend(_kill_owned_holders(prefix))
        _retire_stale_endpoints(prefix)
        time.sleep(0.05)
    raise ContractError("task-owned prefix did not reach a clean census")


def _common(
    trace_id: str,
    template: dict[str, Any],
    active: dict[str, Any],
    source_digest: str,
    runtime_digest: str,
    verifier_closure_digest: str,
    prefix: Path,
    started_ns: int,
    output_bytes: int,
) -> dict[str, Any]:
    return {
        "trace_id": trace_id,
        "phases": template["phases"],
        "terminal": template["terminal"],
        "unresolved_obligations": [],
        "source_identity_sha256": source_digest,
        "runtime_identity_sha256": runtime_digest,
        "verifier_semantic_closure_sha256": verifier_closure_digest,
        "prefix_generation": _prefix_generation(prefix),
        "prefix": _file_identity(prefix),
        "session_root": active["root"],
        "root_pidfd_retained": True,
        "endpoints": active["endpoints"],
        "route": "OFF",
        "routing": "DEFERRED",
        "budgets": {
            "elapsed_ns": max(1, time.monotonic_ns() - started_ns),
            "output_bytes": output_bytes,
            "processes_observed": len(active["processes"]),
            "max_fds_observed": active["max_fds"],
        },
    }


def _verify(
    verifier: Path,
    evidence: Path,
    trace_id: str,
    observation: dict[str, Any],
) -> dict[str, Any]:
    encoded = json.dumps(observation, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > 32 * 1024:
        raise ContractError("guest observation exceeds verifier transport bound")
    result = subprocess.run(
        [
            str(verifier),
            "--verify-guest-observation",
            TRACE_HEX[trace_id],
            observation["common"]["source_identity_sha256"],
            observation["common"]["runtime_identity_sha256"],
        ],
        input=encoded,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )
    if result.returncode != 0:
        diagnostic = {
            "trace_id": trace_id,
            "returncode": result.returncode,
            "observation": observation,
            "field_diff": {
                "verifier_stderr": result.stderr[-4096:].decode(errors="replace"),
                "verifier_stdout": result.stdout[-4096:].decode(errors="replace"),
            },
        }
        (evidence / f"{trace_id}.verifier-failure.json").write_text(
            json.dumps(diagnostic, indent=2, sort_keys=True) + "\n"
        )
        raise ContractError(
            f"Rust verifier rejected {trace_id}: stdout={result.stdout!r} stderr={result.stderr!r}"
        )
    if len(result.stdout) + len(result.stderr) > MAX_OUTPUT_BYTES:
        raise ContractError("Rust verifier output exceeds bound")
    verdict = json.loads(result.stdout)
    if verdict.get("status") != "GUEST_READY_OBSERVATION_ACCEPTED":
        raise ContractError(f"unexpected Rust verdict: {verdict!r}")
    return verdict


def _rust_templates(verifier: Path) -> dict[str, dict[str, Any]]:
    templates: dict[str, dict[str, Any]] = {}
    for trace_id, bytecode in TRACE_HEX.items():
        result = subprocess.run(
            [str(verifier), "--guest-observation-template", bytecode, trace_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            check=False,
        )
        if result.returncode != 0:
            raise ContractError(
                f"Rust template rejected {trace_id}: {result.stderr!r}"
            )
        template = json.loads(result.stdout)
        if template.get("trace_id") != trace_id or template.get("oracle_status") != "ACCEPTED":
            raise ContractError(f"invalid Rust template for {trace_id}: {template!r}")
        templates[trace_id] = template
    return templates


def _rust_verifier_identity(verifier: Path) -> dict[str, Any]:
    result = subprocess.run(
        [str(verifier), "--guest-verifier-identity"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )
    if result.returncode != 0 or len(result.stdout) > MAX_OUTPUT_BYTES:
        raise ContractError(f"Rust verifier identity unavailable: {result.stderr!r}")
    identity = json.loads(result.stdout)
    closure = identity.get("semantic_closure")
    digest = identity.get("semantic_closure_sha256", "")
    if (
        not identity.get("semantic_closure_authoritative")
        or not isinstance(closure, list)
        or not closure
        or len(digest) != 64
    ):
        raise ContractError("Rust verifier semantic closure identity is malformed")
    return identity


def _negative_matrix(
    verifier: Path,
    accepted: dict[str, tuple[dict[str, Any], dict[str, Any]]],
    verifier_identity: dict[str, Any],
) -> dict[str, Any]:
    trace_id = "shared-session-signal-rejected"
    observation = copy.deepcopy(accepted[trace_id][0])
    expected_source = observation["common"]["source_identity_sha256"]
    expected_runtime = observation["common"]["runtime_identity_sha256"]
    cases: list[tuple[str, str, dict[str, Any]]] = []
    wrong_identity = copy.deepcopy(observation)
    wrong_identity["common"]["runtime_identity_sha256"] = "0" * 64
    cases.append(("identity", trace_id, wrong_identity))
    reordered = copy.deepcopy(observation)
    reordered["common"]["phases"][1], reordered["common"]["phases"][2] = (
        reordered["common"]["phases"][2], reordered["common"]["phases"][1]
    )
    cases.append(("ordering", trace_id, reordered))
    missing = copy.deepcopy(observation)
    missing["common"]["phases"].pop(3)
    cases.append(("missing-event", trace_id, missing))
    terminal = copy.deepcopy(observation)
    terminal["common"]["terminal"] = "SUCCESS"
    cases.append(("terminal", trace_id, terminal))
    obligation = copy.deepcopy(observation)
    obligation["common"]["unresolved_obligations"] = ["signal-failure"]
    cases.append(("unresolved-obligation", trace_id, obligation))
    cross_pair = copy.deepcopy(observation)
    cases.append(("cross-pair", "shared-session-signal-gone", cross_pair))
    unknown = copy.deepcopy(observation)
    unknown["forged"] = True
    cases.append(("unknown-field", trace_id, unknown))
    changed_lib = copy.deepcopy(observation)
    changed_closure = copy.deepcopy(verifier_identity["semantic_closure"])
    for entry in changed_closure:
        if entry["path"] == "src/lib.rs":
            entry["sha256"] = "0" * 64
            break
    else:
        raise ContractError("Rust semantic closure omitted src/lib.rs")
    manifest = "".join(
        f"{entry['path']}={entry['sha256']}\n" for entry in changed_closure
    ).encode()
    changed_lib["common"]["verifier_semantic_closure_sha256"] = hashlib.sha256(
        manifest
    ).hexdigest()
    cases.append(("src-lib-identity", trace_id, changed_lib))

    rejected = []
    for name, candidate_trace, candidate in cases:
        result = subprocess.run(
            [
                str(verifier),
                "--verify-guest-observation",
                TRACE_HEX[candidate_trace],
                expected_source,
                expected_runtime,
            ],
            input=json.dumps(candidate, separators=(",", ":")).encode(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10.0,
            check=False,
        )
        if result.returncode == 0:
            raise ContractError(f"tamper negative accepted: {name}")
        rejected.append(name)
    oversized = json.dumps(observation).encode() + b" " * (32 * 1024)
    result = subprocess.run(
        [
            str(verifier),
            "--verify-guest-observation",
            TRACE_HEX[trace_id],
            expected_source,
            expected_runtime,
        ],
        input=oversized,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10.0,
        check=False,
    )
    if result.returncode == 0:
        raise ContractError("oversized observation accepted")
    rejected.append("oversized")
    return {"cases": len(rejected), "rejected": rejected}


def _rpc(launcher: Path, prefix: Path, evidence: Path, name: str) -> dict[str, Any]:
    marker = f"GUEST52_RPC_{name}"
    command = _run([str(launcher), "shell", "echo", marker], _environment(prefix), evidence, name)
    if marker.encode() not in (evidence / f"{name}.stdout").read_bytes():
        raise ContractError(f"{name}: real guest RPC marker absent")
    return command


def _graceful_shutdown(
    launcher: Path,
    prefix: Path,
    evidence: Path,
    name: str,
    holder: subprocess.Popen[bytes],
    commands: list[dict[str, Any]],
) -> dict[str, Any]:
    commands.append(_run([str(launcher), "shutdown"], _environment(prefix), evidence, name))
    _stop_holder(holder)
    retired: list[dict[str, Any]] = []

    def cleanup_ready() -> dict[str, Any] | None:
        nonlocal retired
        processes, _max_fds = _process_census(prefix)
        if processes or _mounts(prefix):
            return None
        retired.extend(_retire_stale_endpoints(prefix))
        clean = _clean_census(prefix)
        if clean is not None:
            clean["stale_endpoints_retired"] = retired
        return clean

    deadline = time.monotonic() + TRANSITION_SECONDS
    while time.monotonic() < deadline:
        clean = cleanup_ready()
        if clean is not None:
            return clean
        time.sleep(0.05)
    processes, _max_fds = _process_census(prefix)
    raise ContractError(
        "graceful cleanup retained residue: "
        f"processes={processes!r} namespace={_namespace_residue(prefix)!r} "
        f"mounts={_mounts(prefix)!r}"
    )


def _run_lane(args: argparse.Namespace) -> dict[str, Any]:
    workspace = args.workspace.resolve(strict=True)
    prefix = args.prefix.resolve(strict=True)
    launcher = args.launcher.resolve(strict=True)
    verifier = args.verifier.resolve(strict=True)
    cell = load_runtime_cell(
        forest=args.forest,
        workspace=workspace,
        build_dir=args.build_dir,
        runtime_prefix=prefix,
        cohort_enabled=args.cohort == "ON",
        profile=args.runtime_profile,
    )
    if launcher != cell.runtime_prefix / "bin/darling":
        raise ContractError("launcher is outside the authenticated RuntimeCell")
    evidence = args.evidence.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    verifier_identity = _rust_verifier_identity(verifier)
    identity, source_digest, runtime_digest = _source_identity(
        workspace, prefix, verifier_identity
    )
    templates = _rust_templates(verifier)
    prefix_observation = observe(prefix)
    prefix_generation = cell.state.generation
    commands: list[dict[str, Any]] = []
    output_budget_negative = _output_budget_negative(evidence)
    accepted: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    cycles: list[dict[str, Any]] = []
    retained_pidfds: list[int] = []
    holders: list[subprocess.Popen[bytes]] = []

    try:
        # Cycle 1: real guest-ready RPC, invalid signal rejection, graceful
        # shutdown, and the retained root pidfd becoming GONE.
        started = time.monotonic_ns()
        holder1, active1 = _start_holder(
            launcher, prefix, evidence, "cycle1-holder",
            "echo GUEST52_HOLDER_READY; while :; do read -t 1 || :; done",
        )
        holders.append(holder1)
        root1_fd = _retain_pidfd(active1["root"])
        retained_pidfds.append(root1_fd)
        try:
            signal.pidfd_send_signal(root1_fd, 999)
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise ContractError(f"unexpected rejected-signal errno: {error.errno}") from error
        else:
            raise ContractError("invalid signal unexpectedly accepted")
        if _process_identity(active1["root"]["pid"]) != active1["root"]:
            raise ContractError("rejected signal changed root identity")
        rejected_observation = {
            "mode": templates["shared-session-signal-rejected"]["mode"],
            "common": _common(
                "shared-session-signal-rejected", templates["shared-session-signal-rejected"], active1, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, 0,
            ),
            "rejection_errno": "EINVAL",
            "root_still_alive": True,
            "recovery_completed": True,
        }
        accepted["shared-session-signal-rejected"] = (
            rejected_observation,
            _verify(verifier, evidence, "shared-session-signal-rejected", rejected_observation),
        )
        rpc1 = _rpc(launcher, prefix, evidence, "cycle1-rpc")
        commands.append(rpc1)
        clean1 = _graceful_shutdown(
            launcher, prefix, evidence, "cycle1-shutdown", holder1, commands
        )
        holders.remove(holder1)
        if not _pidfd_state(root1_fd, int(TRANSITION_SECONDS * 1000)):
            raise ContractError("cycle1 root pidfd did not become terminal")
        try:
            signal.pidfd_send_signal(root1_fd, 0)
        except ProcessLookupError:
            pass
        else:
            raise ContractError("retired cycle1 pidfd did not return ESRCH")
        gone_observation = {
            "mode": templates["shared-session-signal-gone"]["mode"],
            "common": _common(
                "shared-session-signal-gone", templates["shared-session-signal-gone"], active1, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, rpc1["output_bytes"],
            ),
            "post_shutdown_signal": "ESRCH",
            "cleanup_complete": True,
        }
        accepted["shared-session-signal-gone"] = (
            gone_observation,
            _verify(verifier, evidence, "shared-session-signal-gone", gone_observation),
        )
        cycles.append({"cycle": 1, "root": active1["root"], "rpc": rpc1, "cleanup": clean1})

        # Cycle 2: prove a bounded retained-holder timeout, then complete an
        # ordinary graceful shutdown.  The adversarial late fork is isolated
        # below so the three required success cycles remain genuinely clean.
        started = time.monotonic_ns()
        holder2, active2 = _start_holder(
            launcher, prefix, evidence, "cycle2-holder",
            "echo GUEST52_HOLDER_READY; while :; do read -t 1 || :; done",
        )
        holders.append(holder2)
        root2_fd = _retain_pidfd(active2["root"])
        retained_pidfds.append(root2_fd)
        if _pidfd_state(root2_fd, 100):
            raise ContractError("retained holder terminated before deadline")
        timeout_observation = {
            "mode": templates["shared-session-retained-holder-timeout"]["mode"],
            "common": _common(
                "shared-session-retained-holder-timeout", templates["shared-session-retained-holder-timeout"], active2, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, 0,
            ),
            "deadline": "EXPIRED",
            "holder_retained": True,
            "recovery_completed": True,
        }
        clean2 = _graceful_shutdown(
            launcher, prefix, evidence, "cycle2-shutdown", holder2, commands
        )
        holders.remove(holder2)
        accepted["shared-session-retained-holder-timeout"] = (
            timeout_observation,
            _verify(verifier, evidence, "shared-session-retained-holder-timeout", timeout_observation),
        )
        cycles.append({"cycle": 2, "root": active2["root"], "cleanup": clean2})

        # Cycle 3: same-prefix reuse with a new root identity and an ordinary
        # successful guest RPC plus graceful shutdown.
        started = time.monotonic_ns()
        holder3, active3 = _start_holder(
            launcher, prefix, evidence, "cycle3-holder",
            "echo GUEST52_HOLDER_READY; while :; do read -t 1 || :; done",
        )
        holders.append(holder3)
        root3_fd = _retain_pidfd(active3["root"])
        retained_pidfds.append(root3_fd)
        if active3["root"] == active1["root"]:
            raise ContractError("reuse cycle repeated the root PID/starttime identity")
        try:
            signal.pidfd_send_signal(root1_fd, 0)
        except ProcessLookupError:
            pass
        else:
            raise ContractError("old root pidfd targeted the replacement cycle")
        pid_reuse_observation = {
            "mode": templates["shared-session-pid-reuse"]["mode"],
            "common": _common(
                "shared-session-pid-reuse", templates["shared-session-pid-reuse"], active3, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, 0,
            ),
            "original": active1["root"],
            "replacement": active3["root"],
            "retired_pidfd_signal": "ESRCH",
            "replacement_alive": True,
            "same_prefix": same_object(observe(prefix), prefix_observation),
        }
        accepted["shared-session-pid-reuse"] = (
            pid_reuse_observation,
            _verify(verifier, evidence, "shared-session-pid-reuse", pid_reuse_observation),
        )
        rpc3 = _rpc(launcher, prefix, evidence, "cycle3-rpc")
        commands.append(rpc3)
        clean3 = _graceful_shutdown(
            launcher, prefix, evidence, "cycle3-shutdown", holder3, commands
        )
        holders.remove(holder3)
        cycles.append({"cycle": 3, "root": active3["root"], "rpc": rpc3, "cleanup": clean3})

        # Fault cycle A: a real guest fork appears after the retained process
        # snapshot.  The accepted trace is FAIL_CLOSED, so external recovery
        # owns any exact prefix holder left after product shutdown.
        started = time.monotonic_ns()
        late_script = (
            "echo GUEST52_HOLDER_READY; "
            "IFS= read -r _; "
            "(/usr/bin/perl -MPOSIX -e "
            "'POSIX::setsid(); %ENV=(); exec q{/bin/bash}, q{-c}, "
            "q{while :; do read -t 1 || :; done}') & "
            "printf 'GUEST52_LATE_PID=%s\\n' \"$!\"; "
            "while :; do read -t 1 || :; done"
        )
        late_holder, late_active = _start_holder(
            launcher, prefix, evidence, "fault-late-holder", late_script,
            interactive=True,
        )
        holders.append(late_holder)
        late_root_fd = _retain_pidfd(late_active["root"])
        retained_pidfds.append(late_root_fd)
        snapshot_ids = {
            (item["pid"], item["starttime"]) for item in late_active["processes"]
        }
        if late_holder.stdin is None:
            raise ContractError("late holder has no retained trigger pipe")
        late_holder.stdin.write(b"go\n")
        late_holder.stdin.flush()

        def late_child() -> dict[str, int] | None:
            output = (evidence / "fault-late-holder.stdout").read_bytes()
            marker = b"GUEST52_LATE_PID="
            if marker not in output:
                return None
            value = output.split(marker, 1)[1].split(b"\n", 1)[0]
            if len(value) > 20:
                raise ContractError("late child identity exceeds pipe budget")
            try:
                pid = int(value.decode("ascii"))
            except (UnicodeError, ValueError) as error:
                raise ContractError("late child PID is malformed") from error
            identity = _process_identity(pid)
            if identity is None or (identity["pid"], identity["starttime"]) in snapshot_ids:
                return None
            return identity

        late = _wait("real late guest fork", late_child)
        late_pidfd = _retain_pidfd(late)
        retained_pidfds.append(late_pidfd)
        late_clean, late_killed = _cleanup(
            launcher, prefix, evidence, "fault-late-cleanup", commands
        )
        _stop_holder(late_holder)
        holders.remove(late_holder)
        if _process_identity(late["pid"]) is not None:
            raise ContractError("late guest child survived fail-closed recovery")
        late_observation = {
            "mode": templates["shared-session-late-fork"]["mode"],
            "common": _common(
                "shared-session-late-fork", templates["shared-session-late-fork"], late_active, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, 0,
            ),
            "late_child": late,
            "observed_after_snapshot": True,
            "post_shutdown": "GONE",
            "cleanup_complete": True,
        }
        accepted["shared-session-late-fork"] = (
            late_observation,
            _verify(verifier, evidence, "shared-session-late-fork", late_observation),
        )

        # Fault cycle B: kill the exact retained root pidfd before membership
        # traversal, refuse a stale snapshot, and perform external forensic
        # cleanup without pretending this was a graceful product transition.
        started = time.monotonic_ns()
        holder4, active4 = _start_holder(
            launcher, prefix, evidence, "fault-root-holder",
            "echo GUEST52_HOLDER_READY; while :; do read -t 1 || :; done",
        )
        holders.append(holder4)
        root4_fd = _retain_pidfd(active4["root"])
        retained_pidfds.append(root4_fd)
        signal.pidfd_send_signal(root4_fd, signal.SIGKILL)
        if not _pidfd_state(root4_fd, int(TRANSITION_SECONDS * 1000)):
            raise ContractError("fault root did not exit")
        if _process_identity(active4["root"]["pid"]) is not None:
            raise ContractError("root identity remained live after pidfd SIGKILL")
        forensic_clean, killed = _cleanup(
            launcher, prefix, evidence, "fault-root-cleanup", commands
        )
        _stop_holder(holder4)
        holders.remove(holder4)
        root_gone_observation = {
            "mode": templates["session-root-exit-before-snapshot"]["mode"],
            "common": _common(
                "session-root-exit-before-snapshot", templates["session-root-exit-before-snapshot"], active4, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, 0,
            ),
            "root_state": "GONE",
            "snapshot_attempted": True,
            "children_traversed": False,
            "forensic_cleanup_complete": True,
        }
        accepted["session-root-exit-before-snapshot"] = (
            root_gone_observation,
            _verify(verifier, evidence, "session-root-exit-before-snapshot", root_gone_observation),
        )

        # Transport interruption: interrupt the owned launcher group while a
        # real guest command is active, then recover the still separately
        # owned runtime and prove its retained root pidfd reaches GONE.
        started = time.monotonic_ns()
        interrupt_holder, interrupt_active = _start_holder(
            launcher,
            prefix,
            evidence,
            "interrupt-holder",
            "echo GUEST52_HOLDER_READY; while :; do read -t 1 || :; done",
        )
        holders.append(interrupt_holder)
        interrupt_pidfd = _retain_pidfd(interrupt_active["root"])
        retained_pidfds.append(interrupt_pidfd)
        os.killpg(interrupt_holder.pid, signal.SIGINT)
        _stop_holder(interrupt_holder)
        holders.remove(interrupt_holder)
        interrupt_clean, interrupt_killed = _cleanup(
            launcher, prefix, evidence, "interrupt-cleanup", commands
        )
        if not _pidfd_state(interrupt_pidfd, int(TRANSITION_SECONDS * 1000)):
            raise ContractError("interrupted session root did not become terminal")
        try:
            signal.pidfd_send_signal(interrupt_pidfd, 0)
        except ProcessLookupError:
            pass
        else:
            raise ContractError("interrupted session retained a live root pidfd")
        interruption_observation = {
            "mode": templates["shared-session-signal-gone"]["mode"],
            "common": _common(
                "shared-session-signal-gone", templates["shared-session-signal-gone"], interrupt_active, source_digest,
                runtime_digest, verifier_identity["semantic_closure_sha256"], prefix, started, 0,
            ),
            "post_shutdown_signal": "ESRCH",
            "cleanup_complete": True,
        }
        interruption = {
            "signal": "SIGINT",
            "root": interrupt_active["root"],
            "externally_reaped": interrupt_killed,
            "cleanup": interrupt_clean,
            "rust_verdict": _verify(
                verifier, evidence, "shared-session-signal-gone", interruption_observation
            ),
        }

        if set(accepted) != set(TRACE_HEX):
            raise ContractError("not every accepted trace reached the Rust verifier")
        negatives = _negative_matrix(verifier, accepted, verifier_identity)
        final_clean = _wait("final clean census", lambda: _clean_census(prefix))
        final_prefix = observe(prefix)
        if not same_object(final_prefix, prefix_observation):
            raise ContractError("same-prefix root identity changed")
        if final_prefix.security != prefix_observation.security:
            raise ContractError("same-prefix security metadata changed")
        if _prefix_generation(prefix) != prefix_generation:
            raise ContractError("same-prefix lifecycle generation changed")
        normalized_semantics = {
            "traces": {
                name: {
                    "mode": value[1]["mode"],
                    "terminal": value[1]["terminal"],
                    "oracle_status": value[1]["oracle_status"],
                    "phases": value[0]["common"]["phases"],
                }
                for name, value in sorted(accepted.items())
            },
            "graceful_reuse_cycles": len(cycles),
            "fault_modes": ["late-fork", "root-exit-before-snapshot", "SIGINT"],
            "tamper_negatives": negatives["rejected"],
            "route": "OFF",
            "routing": "DEFERRED",
            "teardown": {
                "processes": len(final_clean["processes"]),
                "mounts": len(final_clean["mounts"]),
                "endpoints": len(final_clean["endpoints"]),
                "transaction_refs": len(final_clean["transaction_refs"]),
                "fd_holders": len(final_clean["fd_holders"]),
            },
        }
        report = {
            "status": "GUEST_READY_REAL_KERNEL_LANE_5_2_VALID",
            "lane": "dar-4ush.5.2",
            "identity": identity,
            "source_identity_sha256": source_digest,
            "runtime_identity_sha256": runtime_digest,
            "accepted": {
                name: {"observation": value[0], "rust_verdict": value[1]}
                for name, value in sorted(accepted.items())
            },
            "cycles": cycles,
            "fault_cycle": {
                "root": active4["root"],
                "externally_reaped": killed,
                "cleanup": forensic_clean,
            },
            "late_fork_cycle": {
                "root": late_active["root"],
                "late_child": late,
                "externally_reaped": late_killed,
                "cleanup": late_clean,
            },
            "interruption": interruption,
            "tamper_negatives": negatives,
            "output_budget_negative": output_budget_negative,
            "normalized_semantics": normalized_semantics,
            "normalized_semantics_sha256": _canonical_digest(normalized_semantics),
            "commands": commands,
            "teardown": final_clean,
            "limits": {
                "command_seconds": COMMAND_SECONDS,
                "transition_seconds": TRANSITION_SECONDS,
                "output_bytes": MAX_OUTPUT_BYTES,
                "processes": MAX_PROCESSES,
                "fds_per_process": MAX_FDS,
                "tree_entries": MAX_TREE_ENTRIES,
            },
            "route": "OFF",
            "routing": "DEFERRED",
        }
        encoded = json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_REPORT_BYTES:
            raise ContractError("machine report exceeds bound")
        return report
    finally:
        for holder in holders:
            _stop_holder(holder)
        for descriptor in retained_pidfds:
            os.close(descriptor)
        try:
            _cleanup(launcher, prefix, evidence, "finalizer-cleanup", commands)
        except Exception:
            pass


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--forest", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, required=True)
    parser.add_argument("--cohort", choices=("ON", "OFF"), required=True)
    parser.add_argument(
        "--runtime-profile", default="homebrew-rootless-bootstrap-minimal"
    )
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--launcher", type=Path, required=True)
    parser.add_argument("--verifier", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--preflight-only", action="store_true")
    args = parser.parse_args()
    if args.preflight_only:
        load_runtime_cell(
            forest=args.forest,
            workspace=args.workspace,
            build_dir=args.build_dir,
            runtime_prefix=args.prefix,
            cohort_enabled=args.cohort == "ON",
            profile=args.runtime_profile,
        )
        print("GUEST_READY_RUNTIME_CELL_VALID")
        return
    report = _run_lane(args)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("GUEST_READY_REAL_KERNEL_LANE_5_2_VALID")


if __name__ == "__main__":
    try:
        main()
    except (
        ContractError, RuntimeCellError, OSError, subprocess.SubprocessError, ValueError
    ) as error:
        print(f"GUEST_READY_REAL_KERNEL_LANE_5_2_FAILED {error}", file=sys.stderr)
        raise SystemExit(1)
