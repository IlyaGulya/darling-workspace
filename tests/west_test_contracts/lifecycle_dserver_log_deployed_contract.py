#!/usr/bin/env python3
"""Real opt-in boot/reuse gate for the bounded Darlingserver main-log cohort."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import time
from typing import Any

import lifecycle_cohort_deployed_contract as cohort


class ContractError(RuntimeError):
    pass


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def identity(path: pathlib.Path) -> dict[str, int]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o644:
        raise ContractError("dserver.log is not an exact mode-0644 regular file")
    return {
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "uid": metadata.st_uid,
        "gid": metadata.st_gid,
        "nlink": metadata.st_nlink,
        "mode": stat.S_IMODE(metadata.st_mode),
        "size": metadata.st_size,
    }


def proc_starttime(pid: int) -> int:
    value = (pathlib.Path("/proc") / str(pid) / "stat").read_text()
    closing = value.rfind(")")
    if closing < 0:
        raise ContractError(f"malformed /proc/{pid}/stat")
    fields = value[closing + 2:].split()
    if len(fields) < 20:
        raise ContractError(f"short /proc/{pid}/stat")
    return int(fields[19])


def log_holders(path: pathlib.Path) -> list[dict[str, Any]]:
    expected = identity(path)
    holders: list[dict[str, Any]] = []
    for proc in pathlib.Path("/proc").iterdir():
        if not proc.name.isdecimal():
            continue
        fd_root = proc / "fd"
        try:
            descriptors = list(fd_root.iterdir())
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        for descriptor in descriptors:
            try:
                metadata = descriptor.stat()
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                continue
            if (metadata.st_dev, metadata.st_ino) != (expected["device"], expected["inode"]):
                continue
            try:
                fields = dict(
                    line.split(":", 1) for line in
                    (proc / "fdinfo" / descriptor.name).read_text().splitlines()
                    if ":" in line
                )
                flags = int(fields["flags"].strip(), 8)
                pid = int(proc.name)
                starttime = proc_starttime(pid)
                proc_identity = cohort._process_identity(pid)
                if proc_identity is None:
                    continue
                ppid, _sid, observed_starttime = proc_identity
                if observed_starttime != starttime:
                    continue
                executable = (proc / "exe").resolve(strict=True)
            except (FileNotFoundError, KeyError, PermissionError, ProcessLookupError, ValueError):
                continue
            holders.append({
                "pid": pid,
                "ppid": ppid,
                "starttime": starttime,
                "fd": int(descriptor.name),
                "flags": flags,
                "executable_sha256": sha256(executable),
                "role": "retained-o-path" if flags & os.O_PATH else "writer",
            })
    return sorted(holders, key=lambda item: (item["pid"], item["fd"]))


def wait_no_holders(path: pathlib.Path) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if not log_holders(path):
            return
        time.sleep(0.05)
    raise ContractError("dserver.log FD holder survived shutdown")


def lifecycle_environment(prefix: pathlib.Path) -> dict[str, str]:
    environment = cohort._environment(prefix)
    # Existing production process/call logging provides the deterministic
    # write witness; the contract does not add a test-only logging endpoint.
    # Info records are emitted for real process creation/destruction while
    # keeping the inherited bounded file-size policy well below its ceiling.
    environment["DSERVER_LOG_LEVEL"] = "info"
    environment["DSERVER_LOG_STDERR"] = "0"
    return environment


def wait_for_log_append(path: pathlib.Path, previous: bytes) -> bytes:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        current = path.read_bytes()
        if not current.startswith(previous):
            raise ContractError("dserver.log did not preserve prior content")
        appended = current[len(previous):]
        if appended:
            if b"](" not in appended or not appended.endswith(b"\n"):
                raise ContractError("dserver.log append is not a production logger record")
            return current
        time.sleep(0.05)
    raise ContractError("production Darlingserver logger did not append after RPC")


def source_identity(root: pathlib.Path, allowed: set[str]) -> dict[str, Any]:
    def git(*arguments: str, binary: bool = False) -> subprocess.CompletedProcess[Any]:
        return subprocess.run(
            ["git", "-C", str(root), *arguments],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=not binary,
            check=False,
        )

    head = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    status = git("status", "--porcelain", "--untracked-files=all")
    if head.returncode or tree.returncode or status.returncode:
        raise ContractError(f"cannot identify source repository: {root}")
    changed = {line[3:] for line in status.stdout.splitlines() if len(line) > 3}
    if changed != allowed:
        raise ContractError(f"source scope drift in {root}: {sorted(changed)!r}")
    patch = git("diff", "--binary", "HEAD", "--", *sorted(allowed), binary=True)
    if patch.returncode:
        raise ContractError(f"cannot bind source patch: {root}")
    untracked = [path for path in sorted(allowed) if not (root / path).is_file()]
    if untracked:
        raise ContractError(f"source allowlist entry is not a file: {untracked!r}")
    digest = hashlib.sha256(patch.stdout)
    for path in sorted(allowed):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(stat.S_IMODE((root / path).lstat().st_mode).to_bytes(2, "big"))
        digest.update(hashlib.sha256((root / path).read_bytes()).digest())
    return {
        "commit": head.stdout.strip(),
        "tree": tree.stdout.strip(),
        "changed_paths": sorted(allowed),
        "candidate_digest": digest.hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--launcher", type=pathlib.Path, required=True)
    parser.add_argument("--prefix", type=pathlib.Path, required=True)
    parser.add_argument("--evidence-dir", type=pathlib.Path, required=True)
    parser.add_argument("--expected-darlingserver-sha256", required=True)
    parser.add_argument("--workspace-root", type=pathlib.Path, required=True)
    parser.add_argument("--darlingserver-root", type=pathlib.Path, required=True)
    args = parser.parse_args()

    launcher = args.launcher.resolve(strict=True)
    prefix = args.prefix.resolve(strict=True)
    evidence = args.evidence_dir.resolve()
    evidence.mkdir(mode=0o700, parents=True, exist_ok=False)
    deployed_server = (prefix / "bin/darlingserver").resolve(strict=True)
    if sha256(deployed_server) != args.expected_darlingserver_sha256:
        raise ContractError("deployed Darlingserver digest mismatch")
    workspace_identity = source_identity(args.workspace_root.resolve(strict=True), {
        "docs/rootless-namespace-writers-v1.md",
        "docs/lifecycle-dserver-log-cohort-v1.md",
        "lifecycle/namespace-writer-inventory-v1.json",
        "lifecycle/operation-boundary/include/darling_lifecycle_cohort.h",
        "lifecycle/operation-boundary/src/cohort_routing.rs",
        "tests/run-lifecycle-dserver-log-deployed-contract.sh",
        "tests/run-lifecycle-dserver-log-routing-contract.sh",
        "tests/west_test_contracts/lifecycle_cohort_routing_contract.py",
        "tests/west_test_contracts/lifecycle_dserver_log_deployed_contract.py",
        "tests/west_test_contracts/lifecycle_dserver_log_routing_contract.py",
        "tests/west_test_contracts/namespace_writer_inventory_contract.py",
    })
    darlingserver_identity = source_identity(args.darlingserver_root.resolve(strict=True), {
        "internal-include/darlingserver/server.hpp",
        "src/darlingserver.cpp",
        "src/logging.cpp",
        "src/server.cpp",
    })

    log = prefix / "private/var/log/dserver.log"
    commands: list[dict[str, Any]] = []
    cycles: list[dict[str, Any]] = []
    baseline = identity(log)
    previous_content = log.read_bytes()
    try:
        for cycle in range(1, 3):
            marker = f"DSERVER_LOG_CYCLE_{cycle}_OK"
            commands.append(cohort._run(
                [str(launcher), "shell", "/bin/bash", "-c", "printf %s \"$1\"", "dserver-log", marker],
                env=lifecycle_environment(prefix), evidence_dir=evidence,
                name=f"cycle-{cycle}-rpc", expect=marker,
            ))
            current_content = wait_for_log_append(log, previous_content)
            current = identity(log)
            if (current["device"], current["inode"]) != (baseline["device"], baseline["inode"]):
                raise ContractError("dserver.log identity changed across boot/reuse")
            holders = log_holders(log)
            writers = [holder for holder in holders if holder["role"] == "writer"]
            retained = [holder for holder in holders if holder["role"] == "retained-o-path"]
            if len(writers) != 1 or len(retained) != 1:
                raise ContractError(
                    f"expected one writer and one retained capability, found {holders!r}"
                )
            writer = writers[0]
            flags = writer["flags"]
            if (flags & os.O_ACCMODE != os.O_WRONLY
                    or not flags & os.O_APPEND or not flags & os.O_CLOEXEC):
                raise ContractError(f"log writer flags are not O_WRONLY|O_APPEND: {flags:o}")
            if writer["executable_sha256"] != args.expected_darlingserver_sha256:
                raise ContractError("log writer is not the deployed Darlingserver")
            if retained[0]["ppid"] != writer["pid"]:
                raise ContractError("retained log capability is not owned by the Rust controller")
            clean = cohort._shutdown(
                launcher, prefix, evidence, commands, f"cycle-{cycle}-shutdown"
            )
            wait_no_holders(log)
            after = identity(log)
            if (after["device"], after["inode"]) != (baseline["device"], baseline["inode"]):
                raise ContractError("persistent log was replaced or removed by shutdown")
            after_shutdown_content = log.read_bytes()
            if not after_shutdown_content.startswith(current_content):
                raise ContractError("shutdown did not preserve the accepted log append")
            shutdown_append = after_shutdown_content[len(current_content):]
            if not shutdown_append or b"](" not in shutdown_append or not shutdown_append.endswith(b"\n"):
                raise ContractError("Server teardown did not log through the retained lifecycle FD")
            cycles.append({
                "cycle": cycle,
                "identity": current,
                "bytes_before": len(previous_content),
                "bytes_after": len(current_content),
                "bytes_after_shutdown": len(after_shutdown_content),
                "append_sha256": hashlib.sha256(
                    current_content[len(previous_content):]
                ).hexdigest(),
                "shutdown_append_sha256": hashlib.sha256(shutdown_append).hexdigest(),
                "writer": writer,
                "retained_capability": retained[0],
                "teardown": clean,
            })
            previous_content = after_shutdown_content
    except Exception:
        cohort._best_effort_cleanup(launcher, prefix, evidence)
        raise

    report = {
        "schema": 1,
        "verdict": "LIFECYCLE_DSERVER_LOG_DEPLOYED_VALID",
        "threat_model": "cooperative-writers-exact-lifecycle-lease",
        "routing": "OPT_IN_DEFAULT_OFF",
        "artifacts": {
            "launcher_sha256": sha256(launcher),
            "darlingserver_sha256": sha256(deployed_server),
        },
        "source_identity": {
            "workspace": workspace_identity,
            "darlingserver": darlingserver_identity,
        },
        "log": identity(log),
        "cycles": cycles,
        "commands": commands,
    }
    report_path = evidence / "report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print("LIFECYCLE_DSERVER_LOG_DEPLOYED_VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
