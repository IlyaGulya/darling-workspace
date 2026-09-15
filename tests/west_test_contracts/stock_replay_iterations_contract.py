"""Prove the stock replay's repetition count is plumbed and honest.

The wget-repeat phase reinstalls wget from source many times on purpose: the
repetition is what exposes intermittent transport and lifecycle failures, so the
default count is the acceptance workload. A shortened run exists for iteration,
and it must be impossible to mistake for the acceptance one - both the requested
count and the workload class have to reach the guest program.

The replay script itself runs a stock build at import time, so the fragment it
splices into its guest program lives in west_commands/stock_replay_program.py
and is tested here instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from stock_replay_program import (  # noqa: E402
    DEFAULT_WGET_ITERATIONS,
    ENVIRONMENT_VARIABLE,
    resolve_wget_iterations,
    wget_repeat_phase,
)


def main() -> int:
    # The default is the acceptance workload and says so.
    count, acceptance = resolve_wget_iterations({})
    assert count == DEFAULT_WGET_ITERATIONS and acceptance, (count, acceptance)

    # An explicit count is honoured, and anything shorter is flagged as not the
    # acceptance workload so its evidence cannot be read as the gate.
    count, acceptance = resolve_wget_iterations({ENVIRONMENT_VARIABLE: "3"})
    assert (count, acceptance) == (3, False), (count, acceptance)
    count, acceptance = resolve_wget_iterations({ENVIRONMENT_VARIABLE: str(DEFAULT_WGET_ITERATIONS)})
    assert (count, acceptance) == (DEFAULT_WGET_ITERATIONS, True), (count, acceptance)
    count, acceptance = resolve_wget_iterations({ENVIRONMENT_VARIABLE: "  "})
    assert (count, acceptance) == (DEFAULT_WGET_ITERATIONS, True), (count, acceptance)

    for bad in ("zero", "0", "-2", "1.5"):
        try:
            resolve_wget_iterations({ENVIRONMENT_VARIABLE: bad})
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} must be rejected as an iteration count")

    # The guest program must actually revisit the reinstall the requested number
    # of times, and must not carry a longer loop from the default.
    three = wget_repeat_phase(3)
    assert "for iteration in 1 2 3; do" in three, three
    assert "iterations=3 acceptance=0" in three, three
    assert 'STOCK_WGET_REBUILD_OK iteration=%s' in three, three
    assert '"$brew" reinstall --keep-tmp --build-from-source wget' in three, three
    assert "for iteration in 1 2 3 4" not in three, three

    one = wget_repeat_phase(1)
    assert "for iteration in 1; do" in one and "iterations=1 acceptance=0" in one, one

    full = wget_repeat_phase(DEFAULT_WGET_ITERATIONS)
    assert f"for iteration in {' '.join(str(i) for i in range(1, DEFAULT_WGET_ITERATIONS + 1))}; do" in full, full
    assert f"iterations={DEFAULT_WGET_ITERATIONS} acceptance=1" in full, full

    try:
        wget_repeat_phase(0)
    except ValueError:
        pass
    else:
        raise AssertionError("a zero-iteration phase must be rejected")

    print("PASS stock-replay-iterations-contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
