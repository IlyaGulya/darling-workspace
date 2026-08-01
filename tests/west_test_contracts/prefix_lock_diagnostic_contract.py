from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


def _lifecycle_hash(name: str) -> int:
    value = 1469598103934665603
    for byte in os.fsencode(name):
        value ^= byte
        value = (value * 1099511628211) & ((1 << 64) - 1)
    return value


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    reporter = repo / "ci" / "capture-prefix-lock-owners.py"
    with tempfile.TemporaryDirectory(prefix="prefix-lock-diagnostic-") as raw:
        root = Path(raw)
        prefix = root / "prefix"
        prefix.mkdir()
        lock = root / f".darling-prefix-lock-v2-{_lifecycle_hash(prefix.name):016x}"

        absent = subprocess.run(
            [
                sys.executable,
                "-B",
                str(reporter),
                "--prefix",
                str(prefix),
                "--phase",
                "absent",
                "--proc-root",
                str(root / "proc"),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        assert absent.stdout == (
            "PREFIX_LOCK_SNAPSHOT phase=absent state=absent holders=0\n"
        )

        lock.write_bytes(b"")
        process = root / "proc" / "4242"
        descriptors = process / "fd"
        descriptors.mkdir(parents=True)
        (process / "stat").write_text("4242 (test-holder) S 7 0 0 0\n")
        (process / "exe").symlink_to("/usr/bin/test-holder")
        (descriptors / "9").symlink_to(lock)
        present = subprocess.run(
            [
                sys.executable,
                "-B",
                str(reporter),
                "--prefix",
                str(prefix),
                "--phase",
                "before-second-boot",
                "--proc-root",
                str(root / "proc"),
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        lines = present.stdout.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith(
            "PREFIX_LOCK_SNAPSHOT phase=before-second-boot state=present "
        )
        assert lines[0].endswith(" holders=1")
        assert lines[1] == (
            "PREFIX_LOCK_OWNER phase=before-second-boot pid=4242 ppid=7 "
            "state=S fd=9 comm=test-holder exe=test-holder"
        )

        lock.unlink()
        lock.symlink_to(prefix)
        hostile = subprocess.run(
            [
                sys.executable,
                "-B",
                str(reporter),
                "--prefix",
                str(prefix),
                "--phase",
                "hostile",
                "--proc-root",
                str(root / "proc"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        assert hostile.returncode != 0
        assert "not a regular non-symlink file" in hostile.stderr

    print("PREFIX_LOCK_DIAGNOSTIC_CONTRACT=VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
