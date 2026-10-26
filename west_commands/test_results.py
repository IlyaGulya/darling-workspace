"""Captured results shared by structured ``west test`` execution."""

from __future__ import annotations


class InvocationResult:
    """Captured invocation output together with the runner-owned failure stage."""

    def __init__(self, returncode: int, output: str, failure_phase: str | None):
        self.returncode = returncode
        self.output = output
        self.failure_phase = failure_phase


