"""Prove a paired metadata comparison is not collapsed into one arm.

The acceptance profile runs every comparison twice, once per runtime transport,
with the same command and fixture. The runner deduplicates identical
invocations to avoid repeating a test, and that deduplication used to consider
only the command: the second arm was then reported as "skipped duplicate
invocation already run" and the comparison silently had one side. Ten tests were
skipped this way in the 2026-09-14 acceptance run.

The identity therefore covers the runtime profile, the environment and the
diagnostic mode, while still collapsing genuine repeats of the same arm.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_selection import metadata_invocation_identity  # noqa: E402

COMMAND = "cd darling && <guest-c-fixture> tests/shmem_ring_fd_ownership_guest.c"
ON = {"name": "comparison_on_ring_fd_lifetime", "env": "darling", "runtime-profile": "homebrew-ring-on"}
OFF = {"name": "comparison_off_ring_fd_lifetime", "env": "darling", "runtime-profile": "homebrew-ring-off"}


def invocation(command: str = COMMAND, diag: str = "guarded") -> dict:
    return {"key": command, "diag": diag}


def legacy_identity(_invocation: dict, _test: dict) -> str:
    """The pre-fix identity: the command only."""

    return _invocation["key"]


def main() -> int:
    base = invocation()

    # The defect this contract defends against: with the command alone as the
    # identity, the two arms of a paired comparison collapse into one run.
    assert legacy_identity(base, ON) == legacy_identity(base, OFF), (
        "the pre-fix identity is expected to collapse the two arms; if it no "
        "longer does, this contract is measuring the wrong thing"
    )

    # The arms are different experiments and must both run.
    assert metadata_invocation_identity(base, ON) != metadata_invocation_identity(base, OFF), (
        "the same command under two runtime profiles must not deduplicate"
    )
    listed = {"name": "pair", "env": "darling", "runtime-profiles": ["homebrew-ring-on", "homebrew-ring-off"]}
    other = {"name": "pair", "env": "darling", "runtime-profiles": ["homebrew-ring-on"]}
    assert metadata_invocation_identity(base, listed) != metadata_invocation_identity(base, other), (
        "the runtime profile list form must participate in the identity"
    )
    single = {"name": "pair", "env": "darling", "runtime-profile": "homebrew-ring-on"}
    as_list = {"name": "pair", "env": "darling", "runtime-profiles": ["homebrew-ring-on"]}
    assert metadata_invocation_identity(base, single) == metadata_invocation_identity(base, as_list), (
        "a scalar runtime profile and the one-element list form describe the "
        "same experiment and must deduplicate"
    )

    # Genuine repeats still deduplicate: same command, same arm, same env.
    assert metadata_invocation_identity(base, ON) == metadata_invocation_identity(base, dict(ON)), (
        "an identical declaration must still deduplicate"
    )

    # The environment and the diagnostic mode are also part of the experiment.
    assert metadata_invocation_identity(base, ON) != metadata_invocation_identity(
        base, {**ON, "env": "macos"}
    ), "a different environment must not deduplicate"
    assert metadata_invocation_identity(base, ON) != metadata_invocation_identity(
        invocation(diag="bare"), ON
    ), "a different diagnostic mode must not deduplicate"

    # A different command is a different run regardless of the profile.
    assert metadata_invocation_identity(base, ON) != metadata_invocation_identity(
        invocation(COMMAND + " --extra"), ON
    ), "a different command must not deduplicate"

    print("PASS metadata-invocation-identity-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
