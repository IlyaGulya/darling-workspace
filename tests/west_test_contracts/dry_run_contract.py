"""A plan-only flag must never become a run, and a plan must never delete.

``west test --dry-run`` only plans ``--gc``; for any other selection it used to be
ignored and the selected tests ran anyway. That started a second prefix-backed
run while one was already in flight, which is why the refusal below exists.

The contract drives the real CLI twice: once proving the refusal happens before
any work, and once proving the plan path still works and still deletes nothing.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEST = ["west", "test"]


def run(arguments: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [*WEST, *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


def make_bundles(root: Path) -> list[Path]:
    """Two bundles old enough for --keep-last 1 to consider one prunable."""

    old = time.time() - 7 * 86400
    created = []
    for name in ("20260101T000000Z-west-test-first", "20260102T000000Z-west-test-second"):
        bundle = root / name
        bundle.mkdir(parents=True)
        (bundle / "stdout.log").write_text("marker\n")
        (bundle / "exit-status.txt").write_text("exit status: 0\n")
        os.utime(bundle, (old, old))
        created.append(bundle)
    return created


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="west-test-dry-run-contract-") as raw:
        top = Path(raw)
        bundles = top / "darling-debug"
        bundles.mkdir()
        scratch = top / "proof-scratch"
        scratch.mkdir()
        created = make_bundles(bundles)

        # A plan-only flag with a test selection must refuse before doing work: no
        # bundle appears, and the message says what the flag actually means.
        refused = run([
            "--dry-run",
            "--profile", "homebrew",
            "--label", "name:progress_classifier$",
            "--bundle-root", str(bundles),
        ])
        assert refused.returncode != 0, refused.stdout + refused.stderr
        combined = refused.stdout + refused.stderr
        assert "--dry-run only plans what --gc would prune" in combined, combined
        assert "start the selected run" in combined, combined
        assert sorted(bundles.iterdir()) == created, (
            "a refused plan must not create evidence or start the selected run"
        )

        # The plan path itself still works, still reports what it would remove,
        # and still removes nothing.
        planned = run([
            "--gc", "--dry-run",
            "--keep-last", "1",
            "--max-bundle-mb", "1000000",
            "--bundle-root", str(bundles),
            "--proof-scratch-root", str(scratch),
        ])
        assert planned.returncode == 0, planned.stdout + planned.stderr
        assert "would prune" in planned.stdout, planned.stdout
        assert all(bundle.is_dir() for bundle in created), (
            "a dry-run plan must not delete the bundles it lists"
        )
        assert sorted(bundles.iterdir()) == created, sorted(bundles.iterdir())

    print("PASS west-test-dry-run-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
