#!/usr/bin/env python3
"""Report processes retaining the typed Darling prefix lifecycle lock."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat


_PHASE = re.compile(r"[A-Za-z0-9_.-]+\Z")
_LOCK_PREFIX = ".darling-prefix-lock-v2-"


def lifecycle_name_hash(name: str) -> int:
    value = 1469598103934665603
    for byte in os.fsencode(name):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value


def lock_path_for_prefix(prefix: Path) -> Path:
    return prefix.parent / f"{_LOCK_PREFIX}{lifecycle_name_hash(prefix.name):016x}"


def _process_identity(process: Path) -> tuple[str, str, str]:
    try:
        raw = (process / "stat").read_text()
        closing = raw.rfind(")")
        opening = raw.find("(")
        if opening < 0 or closing < opening:
            raise ValueError("malformed stat")
        comm = raw[opening + 1 : closing]
        fields = raw[closing + 1 :].split()
        state = fields[0]
        ppid = fields[1]
    except (OSError, ValueError, IndexError):
        comm, state, ppid = "unavailable", "?", "?"
    try:
        executable = os.path.basename(os.readlink(process / "exe"))
    except OSError:
        executable = "unavailable"
    return ppid, state, f"comm={comm} exe={executable}"


def lock_holders(lock_status: os.stat_result, proc_root: Path) -> list[dict[str, str]]:
    holders: list[dict[str, str]] = []
    for process in sorted(proc_root.glob("[0-9]*"), key=lambda path: int(path.name)):
        descriptor_root = process / "fd"
        try:
            descriptors = list(descriptor_root.iterdir())
        except OSError:
            continue
        for descriptor in descriptors:
            try:
                opened = descriptor.stat()
            except OSError:
                continue
            if (
                opened.st_dev != lock_status.st_dev
                or opened.st_ino != lock_status.st_ino
            ):
                continue
            ppid, process_state, description = _process_identity(process)
            holders.append(
                {
                    "pid": process.name,
                    "ppid": ppid,
                    "state": process_state,
                    "fd": descriptor.name,
                    "description": description,
                }
            )
    return holders


def capture(prefix: Path, phase: str, proc_root: Path = Path("/proc")) -> int:
    if not prefix.is_absolute() or not prefix.name or not _PHASE.fullmatch(phase):
        raise SystemExit("prefix must be absolute and phase must be a stable token")
    lock_path = lock_path_for_prefix(prefix)
    try:
        lock_status = lock_path.lstat()
    except FileNotFoundError:
        print(f"PREFIX_LOCK_SNAPSHOT phase={phase} state=absent holders=0")
        return 0
    if stat.S_ISLNK(lock_status.st_mode) or not stat.S_ISREG(lock_status.st_mode):
        raise SystemExit("typed prefix lifecycle lock is not a regular non-symlink file")
    holders = lock_holders(lock_status, proc_root)
    print(
        "PREFIX_LOCK_SNAPSHOT"
        f" phase={phase} state=present dev={lock_status.st_dev}"
        f" ino={lock_status.st_ino} holders={len(holders)}"
    )
    for holder in holders:
        print(
            "PREFIX_LOCK_OWNER"
            f" phase={phase} pid={holder['pid']} ppid={holder['ppid']}"
            f" state={holder['state']} fd={holder['fd']}"
            f" {holder['description']}"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", type=Path, required=True)
    parser.add_argument("--phase", required=True)
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"))
    args = parser.parse_args()
    return capture(args.prefix, args.phase, args.proc_root)


if __name__ == "__main__":
    raise SystemExit(main())
