"""Result values shared by ``west test`` execution and RED proof layers."""

from __future__ import annotations


class InvocationResult:
    """Captured invocation output together with the runner-owned failure stage."""

    def __init__(self, returncode: int, output: str, failure_phase: str | None):
        self.returncode = returncode
        self.output = output
        self.failure_phase = failure_phase


class RuntimeBuildFailure(RuntimeError):
    """A declared RED runtime build failed at one observable build phase."""

    def __init__(self, phase: str, result):
        super().__init__(f"runtime {phase} failed")
        self.phase = phase
        self.result = result


class RuntimeRedProven(Exception):
    """Internal signal that a declared RED failure was verified successfully."""
