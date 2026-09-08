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
)


class HostCommand(NamedTuple):
    name: str
    argv: list[str]
    cacheable: bool


def _worker_count() -> int:
    default = min(8, max(1, os.cpu_count() or 1), len(CONTRACTS) + 3)
    value = os.environ.get("DARLING_HOST_TIER_WORKERS", str(default))
    try:
        workers = int(value)
    except ValueError as error:
        raise SystemExit("DARLING_HOST_TIER_WORKERS must be an integer") from error
    if not 1 <= workers <= len(CONTRACTS) + 3:
        raise SystemExit(
            f"DARLING_HOST_TIER_WORKERS must be between 1 and {len(CONTRACTS) + 3}"
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
        workers=_worker_count(),
        cache_root=Path(raw_cache_root) if raw_cache_root else None,
        cache_key=raw_cache_key,
    )


if __name__ == "__main__":
    raise SystemExit(main())
