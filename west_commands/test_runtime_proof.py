"""Domain state machine for behavioral runtime RED-to-GREEN proofs."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Sequence


@dataclass(frozen=True)
class ProofObservation:
    returncode: int
    output: str
    failure_phase: str | None


@dataclass(frozen=True)
class RedOracle:
    failure_phases: tuple[str, ...]
    output_contains: tuple[str, ...]
    output_lacks: tuple[str, ...]

    @classmethod
    def from_manifest(cls, proof: dict) -> "RedOracle":
        phases = proof.get("expect-failure-phase", ())
        contains = proof.get("expect-output-contains", ())
        lacks = proof.get("expect-output-lacks", ())
        return cls(
            failure_phases=_strings(phases),
            output_contains=_strings(contains),
            output_lacks=_strings(lacks),
        )


class ProofState(Enum):
    READY = "ready"
    RED_EXPECTED = "red-expected"
    GREEN = "green"
    FAILED = "failed"


def _strings(value) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value)
    return ()


class RuntimeProofStateMachine:
    """Sequence one bad-runtime observation before one fixed-runtime run."""

    def __init__(self, *, name: str, oracle: RedOracle, error: Callable[[str], None]):
        self.name = name
        self.oracle = oracle
        self.error = error
        self.state = ProofState.READY

    def validate_red(self, observed: ProofObservation) -> bool:
        if self.state is not ProofState.READY:
            raise RuntimeError(f"{self.name}: RED observation repeated in {self.state.value}")
        if observed.returncode == 0:
            self.error(f"{self.name}: RED runtime unexpectedly passed")
            self.state = ProofState.FAILED
            return False
        if (
            self.oracle.failure_phases
            and observed.failure_phase not in self.oracle.failure_phases
        ):
            self.error(
                f"{self.name}: RED failed in phase "
                f"{observed.failure_phase or '<unclassified>'}, want "
                f"{', '.join(self.oracle.failure_phases)}"
            )
            self.state = ProofState.FAILED
            return False
        for needle in self.oracle.output_contains:
            if needle not in observed.output:
                self.error(f"{self.name}: RED failure output missing {needle!r}")
                self.state = ProofState.FAILED
                return False
        for needle in self.oracle.output_lacks:
            if needle in observed.output:
                self.error(
                    f"{self.name}: RED failure output unexpectedly contains {needle!r}"
                )
                self.state = ProofState.FAILED
                return False
        self.state = ProofState.RED_EXPECTED
        return True

    def run_green(self, green: Callable[[], int]) -> int:
        if self.state is not ProofState.RED_EXPECTED:
            raise RuntimeError(f"{self.name}: GREEN attempted before a proven RED")
        returncode = green()
        self.state = ProofState.GREEN if returncode == 0 else ProofState.FAILED
        return returncode

