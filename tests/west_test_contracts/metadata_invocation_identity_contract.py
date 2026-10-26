"""Distinct workload declarations must not collapse because commands coincide."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_selection import metadata_invocation_identity

COMMAND = "cd darling && <guest-c-fixture> tests/shmem_ring_fd_ownership_guest.c"
FIRST = {"id": "fd-lifetime-first", "name": "fd-lifetime", "env": "darling"}
SECOND = {"id": "fd-lifetime-second", "name": "fd-lifetime", "env": "darling"}


def invocation(command: str = COMMAND, diag: str = "guarded") -> dict:
    return {"key": command, "diag": diag}


def main() -> int:
    base = invocation()
    assert metadata_invocation_identity(base, FIRST) != metadata_invocation_identity(base, SECOND), (
        "same display name and command do not make distinct declarations one run"
    )
    assert metadata_invocation_identity(base, FIRST) == metadata_invocation_identity(base, dict(FIRST)), (
        "a repeated reference to the same declaration may deduplicate"
    )
    assert metadata_invocation_identity(base, FIRST) != metadata_invocation_identity(
        base, {**FIRST, "env": "macos"}
    ), "different registration environments must not deduplicate"
    assert metadata_invocation_identity(base, FIRST) != metadata_invocation_identity(
        invocation(diag="bare"), FIRST
    ), "different diagnostic modes must not deduplicate"
    assert metadata_invocation_identity(base, FIRST) != metadata_invocation_identity(
        invocation(COMMAND + " --extra"), FIRST
    ), "different commands must not deduplicate"
    print("PASS metadata-invocation-identity-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
