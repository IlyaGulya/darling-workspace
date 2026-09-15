#!/usr/bin/env python3
"""Run independent host-tier contracts with bounded fail-fast concurrency."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import NamedTuple


ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = (
    "tests/run-west-patch-stack-materialize-contract.sh",
    "tests/run-west-patch-stack-lock-first-contract.sh",
    "tests/run-profile-composition-dependency-contract.sh",
    "tests/run-west-patch-stack-default-cutover-contract.sh",
    "tests/run-west-patch-stack-retirement-policy-contract.sh",
    "tests/run-west-patch-stack-runtime-source-contract.sh",
    "tests/run-patch-stack-immutable-oracle-contract.sh",
    "tests/run-west-patch-stack-export-contract.sh",
    "tests/run-patch-stack-lock-first-hosted-workflow-contract.sh",
    "tests/run-patch-stack-migration-inventory-contract.sh",
    "tests/run-west-test-ctest-backend-contract.sh",
    "tests/run-west-test-runtime-cache-contract.sh",
    "tests/run-west-test-stock-stack-cache-contract.sh",
    "tests/run-west-test-stock-replay-iterations-contract.sh",
    "tests/run-parallel-load-contract.sh",
    "tests/run-west-test-verdict-cache-contract.sh",
    "tests/run-west-test-applicability-preflight-diagnosis-contract.sh",
    "tests/run-west-test-metadata-run-report-contract.sh",
    "tests/run-west-test-dry-run-contract.sh",
    "tests/run-rootless-debug-cleanup-contract.sh",
    "tests/run-west-test-runtime-source-reuse-contract.sh",
    "tests/run-west-test-metadata-invocation-identity-contract.sh",
    # Recovered: these contracts existed and passed but were listed by no tier,
    # so nothing ran them. Registered after running each one on this checkout.
    "tests/run-check-python-syntax-contract.sh",
    "tests/run-handoff-transaction-contract.sh",
    "tests/run-rootless-diagnostics-contract.sh",
    "tests/run-rootless-shutdown-consumer-contract.sh",
    "tests/run-rtk-exit-status-contract.sh",
    "tests/run-west-darling-build-contract.sh",
    "tests/run-west-deploy-transaction-contract.sh",
    "tests/run-west-dev-output-contract.sh",
    "tests/run-west-doctor-output-contract.sh",
    "tests/run-west-doctor-prefix-contract.sh",
    "tests/run-west-dw-beads-alias-contract.sh",
    "tests/run-west-extension-help-contract.sh",
    "tests/run-west-job-contract.sh",
    "tests/run-west-patch-explain-contract.sh",
    "tests/run-west-patch-export-preflight-contract.sh",
    "tests/run-west-patch-stack-preflight-contract.sh",
    "tests/run-west-prefix-repair-contract.sh",
    "tests/run-west-profile-discovery-contract.sh",
    "tests/run-west-source-worktree-contract.sh",
    "tests/run-west-test-ctest-lifecycle-contract.sh",
    "tests/run-west-test-descriptor-transport-contract.sh",
    "tests/run-west-test-dispatch-contract.sh",
    "tests/run-west-test-execution-contract.sh",
    "tests/run-west-test-expect-failure-contract.sh",
    "tests/run-west-test-manifest-contract.sh",
    "tests/run-west-test-resource-provider-contract.sh",
    "tests/run-west-test-results-contract.sh",
    "tests/run-west-test-runtime-red-contract.sh",
    "tests/run-west-test-stacked-omission-contract.sh",
    "tests/run-west-test-worktree-cleanup-contract.sh",
    "tests/run-west-update-parallel-contract.sh",
    "tests/run-patch-stack-mutation-contract.sh",
    "tests/run-lifecycle-explorer-contract.sh",
    "tests/run-lifecycle-fuzz-contract.sh",
    "tests/run-lifecycle-operation-boundary-contract.sh",
    "tests/run-lifecycle-trace-contract.sh",
    # Documented in AGENTS.md as focused contracts but invoked by no entrypoint.
    # run-west-test-testkit-contract.sh is the root of a family: the guest-macho,
    # guest-toolchain, darling-c-test, runtime-build and macho-corpus contracts
    # are only named by it, so registering it makes all of them reachable again.
    "tests/run-west-patch-verify-contract.sh",
    "tests/run-west-test-testkit-contract.sh",
    "tests/run-west-test-add-compat-cmake-contract.sh",
    "tests/run-west-test-gc-contract.sh",
    "tests/run-west-test-guarded-timeout-contract.sh",
    "tests/run-west-test-guest-command-contract.sh",
    "tests/run-west-test-prefix-cleanup-contract.sh",
    "tests/run-clt-provenance-contract.sh",
    "tests/run-rootless-cleanup-contract.sh",
    "tests/run-rootless-prefix-contract.sh",
)

# Contract runners deliberately kept out of the tier. Each entry states the
# reason, and the census below fails the tier if a runner is in neither this
# mapping nor CONTRACTS: an unaccounted contract silently proves nothing.
EXCLUDED_CONTRACTS = {
    "tests/run-ci-test-tiers-contract.sh":
        "drives the tier runner itself; running the tier from inside the tier recurses",
    "tests/run-objc4-macro-contract.sh":
        "requires OBJC4_MACRO_CONTRACT_CANDIDATE, a reviewed objc4 source tree supplied by the operator",
    "tests/run-lifecycle-real-kernel-contract.sh":
        "runs a privileged cgroup-v2 fixture (bounded sudo) and needs an interpreter with os.pidfd_open",
    "tests/run-perf-archive-forensic-contract.sh":
        "fails: the archive-forensic exception ledger disagrees with the contract's expectation "
        "(assert at perf_archive_forensic_contract.py:40); the runner was repaired to use the "
        "house YAML-capable interpreter, which is what made the drift visible",
    "tests/run-legacy-runtime-inventory-contract.sh":
        "fails: locks/patch-stack legacy inventory disagrees with the current profile series",
    "tests/run-namespace-writer-inventory-contract.sh":
        "fails: lifecycle/namespace-writer-inventory-v1.json has no symbol anchor for "
        "darling.startup.prefix-provision in the current darling checkout",
}


def unaccounted_contracts(tests_dir: Path) -> list[str]:
    """Return contract runners that no entrypoint accounts for.

    A runner counts as accounted for when this tier registers it, when it is
    listed in EXCLUDED_CONTRACTS with a reason, when CI or patch metadata names
    it, or when a contract this tier runs names it. A mention in an excluded
    contract does not count: that contract does not run.
    """
    workspace = tests_dir.parent
    registered = {Path(contract).name for contract in CONTRACTS}
    accounted = registered | {Path(contract).name for contract in EXCLUDED_CONTRACTS}
    sources = list((workspace / "ci").rglob("*.py"))
    sources += list((workspace / "ci").rglob("*.sh"))
    sources += list((workspace / ".github" / "workflows").glob("*"))
    sources += list((workspace / "patches").glob("*/patches.yml"))
    sources += [workspace / contract for contract in CONTRACTS]
    for source in sources:
        try:
            text = source.read_text()
        except (OSError, UnicodeDecodeError):
            continue
        for path in tests_dir.glob("run-*contract*.sh"):
            if path.name in text:
                accounted.add(path.name)
    return sorted(
        path.name
        for path in tests_dir.glob("run-*contract*.sh")
        if path.name not in accounted
    )


class HostCommand(NamedTuple):
    name: str
    argv: list[str]
    cacheable: bool


def _worker_count(command_count: int) -> int:
    default = min(8, max(1, os.cpu_count() or 1), command_count)
    value = os.environ.get("DARLING_HOST_TIER_WORKERS", str(default))
    try:
        workers = int(value)
    except ValueError as error:
        raise SystemExit("DARLING_HOST_TIER_WORKERS must be an integer") from error
    if not 1 <= workers <= command_count:
        raise SystemExit(
            f"DARLING_HOST_TIER_WORKERS must be between 1 and {command_count}"
        )
    return workers


def _cache_marker(
    root: Path,
    cache_key: str,
    command: HostCommand,
) -> Path:
    identity = hashlib.sha256(
        json.dumps(
            {"cache_key": cache_key, "name": command.name, "argv": command.argv},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return root / f"{identity}.json"


def _read_cache_marker(
    marker: Path,
    cache_key: str,
    command: HostCommand,
) -> bool:
    if not marker.exists():
        return False
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError(f"host tier cache marker is not a regular file: {marker}")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"host tier cache marker is invalid: {marker}: {error}") from error
    expected = {
        "schema_version": 1,
        "kind": "west-host-tier-result",
        "cache_key": cache_key,
        "name": command.name,
        "argv": command.argv,
        "returncode": 0,
    }
    if value != expected:
        raise RuntimeError(f"host tier cache marker identity differs: {marker}")
    return True


def _write_cache_marker(
    marker: Path,
    cache_key: str,
    command: HostCommand,
) -> None:
    value = {
        "schema_version": 1,
        "kind": "west-host-tier-result",
        "cache_key": cache_key,
        "name": command.name,
        "argv": command.argv,
        "returncode": 0,
    }
    temporary = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(marker)


def run_commands(
    commands: list[HostCommand],
    *,
    workers: int,
    cache_root: Path | None,
    cache_key: str | None,
) -> int:
    if not 1 <= workers <= len(commands):
        raise ValueError("host tier worker count is outside the command range")
    if (cache_root is None) != (cache_key is None):
        raise ValueError("host tier cache root and key must be configured together")
    if cache_key is not None and re.fullmatch(r"[0-9a-f]{64}", cache_key) is None:
        raise ValueError("host tier cache key must be a lowercase SHA-256")
    if cache_root is not None:
        cache_root.mkdir(parents=True, mode=0o700, exist_ok=True)
        if cache_root.is_symlink() or not cache_root.is_dir():
            raise ValueError("host tier cache root must be a real directory")

    stop = threading.Event()
    state_lock = threading.Lock()
    output_lock = threading.Lock()
    active: set[subprocess.Popen[bytes]] = set()

    def signal_processes(
        processes: tuple[subprocess.Popen[bytes], ...],
        sig: int,
    ) -> None:
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

    def execute(command: HostCommand) -> tuple[HostCommand, int]:
        if stop.is_set():
            return command, 0
        started = time.monotonic_ns()
        cache_state = "disabled"
        lock_descriptor: int | None = None
        marker: Path | None = None
        try:
            if command.cacheable and cache_root is not None and cache_key is not None:
                marker = _cache_marker(cache_root, cache_key, command)
                lock_path = marker.with_suffix(".lock")
                lock_descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                if _read_cache_marker(marker, cache_key, command):
                    cache_state = "hit"
                    returncode = 0
                    return command, returncode
                cache_state = "miss"
            process = subprocess.Popen(command.argv, cwd=ROOT, start_new_session=True)
            with state_lock:
                active.add(process)
                cancelled = stop.is_set()
            if cancelled:
                signal_processes((process,), signal.SIGTERM)
            returncode = process.wait()
            with state_lock:
                active.discard(process)
            if returncode:
                stop.set()
            elif marker is not None and cache_key is not None:
                _write_cache_marker(marker, cache_key, command)
            return command, returncode
        finally:
            if lock_descriptor is not None:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)
            elapsed_ms = (time.monotonic_ns() - started) // 1_000_000
            with output_lock:
                print(
                    f"host tier command complete: {command.name} "
                    f"elapsed_ms={elapsed_ms} cache={cache_state}",
                    flush=True,
                )

    failure: tuple[HostCommand, int] | None = None
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(execute, command) for command in commands]
            for future in as_completed(futures):
                command, returncode = future.result()
                if returncode and failure is None:
                    failure = command, returncode
                    with state_lock:
                        processes = tuple(active)
                    signal_processes(processes, signal.SIGTERM)
    except KeyboardInterrupt:
        stop.set()
        with state_lock:
            processes = tuple(active)
        signal_processes(processes, signal.SIGINT)
        return 130

    if failure is not None:
        command, returncode = failure
        print(
            f"host tier command failed ({returncode}): {' '.join(command.argv)}",
            file=sys.stderr,
        )
        return returncode if 0 < returncode < 256 else 1
    return 0


def main() -> int:
    unaccounted = unaccounted_contracts(ROOT / "tests")
    if unaccounted:
        print(
            "host tier contract census failed: these runners are in neither CONTRACTS nor "
            "EXCLUDED_CONTRACTS, so nothing runs them:\n  " + "\n  ".join(unaccounted),
            file=sys.stderr,
        )
        return 2
    commands = [
        HostCommand(Path(contract).stem, [str(ROOT / contract)], True)
        for contract in CONTRACTS
    ]
    commands.append(
        HostCommand(
            "native-inventory",
            [str(ROOT / "tests/run-native-inventory-contract.sh")],
            False,
        )
    )
    commands.append(
        HostCommand(
            "native-artifact",
            [sys.executable, "-B", str(ROOT / "tests/west_test_contracts/native_artifact_contract.py")],
            True,
        )
    )
    commands.append(
        HostCommand(
            "native-transport",
            [sys.executable, "-B", str(ROOT / "tests/west_test_contracts/native_transport_contract.py")],
            True,
        )
    )
    profile_materialization = (
        []
        if os.environ.get("WEST_PREMATERIALIZED_PROFILE") == "homebrew"
        else ["--materialize-profile"]
    )
    commands.append(
        HostCommand(
            "homebrew-host-metadata",
            [
                "west",
                "test",
                "--profile",
                "homebrew",
                "--env",
                "host",
                *profile_materialization,
                *sys.argv[1:],
            ],
            False,
        )
    )
    raw_cache_root = os.environ.get("WEST_HOST_CONTRACT_CACHE_DIR")
    raw_cache_key = os.environ.get("WEST_HOST_CONTRACT_CACHE_KEY")
    return run_commands(
        commands,
        workers=_worker_count(len(commands)),
        cache_root=Path(raw_cache_root) if raw_cache_root else None,
        cache_key=raw_cache_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
