"""Fixture-runner dispatch for ``west test`` invocation plans."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any


Invocation = dict[str, Any]
Runner = Callable[[Invocation, dict[str, str] | None], int]


def dispatch_fixture_runner(
    invocation: Invocation,
    env: dict[str, str] | None,
    *,
    runners: Iterable[tuple[str, Runner]],
    fallback: Runner,
) -> int:
    """Run the first registered fixture matching an invocation plan.

    The registry order is explicit domain policy. A fixture plan is expected to
    select exactly one specialized runner; ordinary script/CTest plans fall
    through to the shared command executor.
    """

    for field, runner in runners:
        if invocation.get(field):
            return runner(invocation, env)
    return fallback(invocation, env)
