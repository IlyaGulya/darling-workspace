"""A failed source-profile preflight must say which failure it observed.

The preflight runs ``west patch verify --applicability-only``, which resolves a
profile's immutable base refs from the mirror. When that fetch cannot reach the
repository the verifier exits non-zero carrying git's transport message, and the
run used to be reported as a profile defect: the reader was told to repair or
rebase a profile that was fine, and the real remedy (network access to the
mirror, then retry) was never mentioned.

The strings below are the ones actually observed: the transient text is copied
from the runtime preflight that aborted before comparison_on_malloc_native, and
the defect text is the wording the verifier produces for a profile it cannot
apply.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_runtime import (  # noqa: E402
    applicability_preflight_advice,
    preflight_retry_allowed,
    transient_preflight_failure,
)

# Verbatim from the aborted preflight (job /tmp/accept-on-c).
TRANSPORT_FAILURE = """
Runtime profile ring-comparison preflight failed with rc 1
patch_stack_lock_first.LockFirstError: git fetch --no-tags immutable refs/tags/patch-stack/v1/bases/f07f265bfbcf071c1adfc808de971e053ea5edc5:refs/west/lock-first-input
fatal: Could not read from remote repository.

Please make sure you have the correct access rights
and the repository exists.
"""

# The verifier's wording for a profile it cannot apply.
APPLICABILITY_DEFECT = """
LockFirstError: git am for immutable 2d6f0321cfe2 failed (1): error: patch failed: src/xnu.c:41
error: src/xnu.c: patch does not apply
"""

PROFILE = "ring-comparison"
# ``always-repair`` reproduces the behaviour this contract exists to catch: one
# closing message for every preflight failure.
ARM = os.environ.get("WEST_APPLICABILITY_DIAGNOSIS_ARM", "green")


def advice(profile: str, output: str) -> str:
    if ARM == "always-repair":
        return (
            f"Repair or rebase that profile with `west patch verify --profile {profile}` "
            "before retrying; this is not a runtime test failure."
        )
    return applicability_preflight_advice(profile, output)


def retry(attempt: int, output: str, *, max_attempts: int = 2) -> bool:
    if ARM == "always-repair":
        return False
    return preflight_retry_allowed(attempt, output, max_attempts=max_attempts)


def run() -> None:
    assert transient_preflight_failure(TRANSPORT_FAILURE), (
        "git's transport failure must be recognised as transient"
    )
    assert not transient_preflight_failure(APPLICABILITY_DEFECT), (
        "an applicability defect must not be mistaken for a transport failure"
    )

    transport_advice = advice(PROFILE, TRANSPORT_FAILURE)
    assert "transport failure" in transport_advice, transport_advice
    assert "Repair or rebase" not in transport_advice, (
        "a transient failure must not send the reader to rebase the profile"
    )
    assert "nothing was deployed or measured" in transport_advice, transport_advice

    defect_advice = advice(PROFILE, APPLICABILITY_DEFECT)
    assert "Repair or rebase that profile" in defect_advice, defect_advice
    assert f"--profile {PROFILE}" in defect_advice, (
        "the defect advice must name the command that reproduces it"
    )
    assert "transport" not in defect_advice, defect_advice

    assert retry(0, TRANSPORT_FAILURE), (
        "a transport failure is worth exactly one retry"
    )
    assert not retry(1, TRANSPORT_FAILURE), (
        "a persistent transport failure must be reported, not retried forever"
    )
    assert not retry(0, APPLICABILITY_DEFECT), (
        "an applicability defect must not be retried"
    )
    assert not retry(0, TRANSPORT_FAILURE, max_attempts=1), (
        "an attempt budget of one means no retry"
    )

    print("PASS applicability-preflight-diagnosis-contract")


def main() -> int:
    if ARM == "green":
        run()
        return 0
    if ARM == "always-repair":
        try:
            run()
        except AssertionError as error:
            print(
                "RED arm (one message for every preflight failure) failed as "
                f"designed: {error}"
            )
            return 0
        print("RED arm unexpectedly passed")
        return 1
    raise SystemExit(f"unknown applicability diagnosis arm: {ARM}")


if __name__ == "__main__":
    raise SystemExit(main())
