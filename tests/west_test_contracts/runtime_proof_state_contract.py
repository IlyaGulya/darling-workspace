"""Behavioral contract for runtime RED-to-GREEN state transitions."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from west_commands.test_runtime_proof import (
    ProofObservation,
    ProofState,
    RedOracle,
    RuntimeProofStateMachine,
)


errors = []
machine = RuntimeProofStateMachine(
    name="runtime contract",
    oracle=RedOracle.from_manifest(
        {
            "expect-failure-phase": ["guest-run"],
            "expect-output-contains": ["OLD_RUNTIME_BROKEN"],
            "expect-output-lacks": ["compile failed"],
        }
    ),
    error=errors.append,
)
assert machine.validate_red(
    ProofObservation(7, "OLD_RUNTIME_BROKEN\n", "guest-run")
)
green_calls = []
assert machine.run_green(lambda: green_calls.append("green") or 0) == 0
assert machine.state is ProofState.GREEN
assert green_calls == ["green"]
assert not errors

provider_machine = RuntimeProofStateMachine(
    name="provider contract",
    oracle=RedOracle.from_manifest(
        {
            "expect-failure-phase": "provider",
            "expect-output-contains": ["PROVIDER_RUNTIME_BROKEN"],
        }
    ),
    error=errors.append,
)
assert provider_machine.validate_red(
    ProofObservation(1, "PROVIDER_RUNTIME_BROKEN\n", "provider")
)
assert provider_machine.run_green(lambda: 0) == 0
assert provider_machine.state is ProofState.GREEN

for observation, expected in (
    (ProofObservation(0, "", None), "unexpectedly passed"),
    (ProofObservation(1, "OLD_RUNTIME_BROKEN", "compile"), "failed in phase"),
    (ProofObservation(1, "unrelated", "guest-run"), "output missing"),
    (
        ProofObservation(1, "OLD_RUNTIME_BROKEN compile failed", "guest-run"),
        "unexpectedly contains",
    ),
):
    errors = []
    rejected = RuntimeProofStateMachine(
        name="rejected",
        oracle=machine.oracle,
        error=errors.append,
    )
    assert not rejected.validate_red(observation)
    assert rejected.state is ProofState.FAILED
    assert expected in errors[-1], errors
    try:
        rejected.run_green(lambda: 0)
    except RuntimeError as error:
        assert "before a proven RED" in str(error)
    else:
        raise AssertionError("GREEN ran after an invalid RED")

print("PASS runtime-proof-state-contract")
