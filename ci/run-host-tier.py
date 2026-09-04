#!/usr/bin/env python3
"""Run independent host-tier contracts with bounded fail-fast concurrency."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


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
)


def _worker_count() -> int:
    value = os.environ.get("DARLING_HOST_TIER_WORKERS", "4")
    try:
        workers = int(value)
    except ValueError as error:
        raise SystemExit("DARLING_HOST_TIER_WORKERS must be an integer") from error
    if not 1 <= workers <= len(CONTRACTS) + 1:
        raise SystemExit(
            f"DARLING_HOST_TIER_WORKERS must be between 1 and {len(CONTRACTS) + 1}"
        )
    return workers


def main() -> int:
    stop = threading.Event()
    lock = threading.Lock()
    active: set[subprocess.Popen[bytes]] = set()
    commands = [[str(ROOT / contract)] for contract in CONTRACTS]
    commands.append(
        [
            "west",
            "test",
            "--profile",
            "homebrew",
            "--env",
            "host",
            "--materialize-profile",
            *sys.argv[1:],
        ]
    )

    def signal_processes(processes: tuple[subprocess.Popen[bytes], ...], sig: int) -> None:
        for process in processes:
            if process.poll() is not None:
                continue
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass

    def run(command: list[str]) -> tuple[list[str], int]:
        if stop.is_set():
            return command, 0
        process = subprocess.Popen(command, cwd=ROOT, start_new_session=True)
        with lock:
            active.add(process)
            cancelled = stop.is_set()
        if cancelled:
            signal_processes((process,), signal.SIGTERM)
        returncode = process.wait()
        with lock:
            active.discard(process)
        if returncode:
            stop.set()
        return command, returncode

    failure: tuple[list[str], int] | None = None
    try:
        with ThreadPoolExecutor(max_workers=_worker_count()) as pool:
            futures = [pool.submit(run, command) for command in commands]
            for future in as_completed(futures):
                command, returncode = future.result()
                if returncode and failure is None:
                    failure = command, returncode
                    with lock:
                        processes = tuple(active)
                    signal_processes(processes, signal.SIGTERM)
    except KeyboardInterrupt:
        stop.set()
        with lock:
            processes = tuple(active)
        signal_processes(processes, signal.SIGINT)
        return 130

    if failure is not None:
        command, returncode = failure
        print(
            f"host tier command failed ({returncode}): {' '.join(command)}",
            file=sys.stderr,
        )
        return returncode if 0 < returncode < 256 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
