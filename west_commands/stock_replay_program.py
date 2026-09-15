"""Guest program fragments for the stock stack replay.

The replay script is executed as a test asset, so its guest program is built
here instead: the fragment is importable, which makes the parts that are easy to
get wrong - the repetition count in particular - testable without running a
stock build.

``wget-repeat`` deliberately reinstalls wget from source many times, because the
repetition is what exposes intermittent transport and lifecycle failures. That
count is the acceptance workload. ``WEST_STOCK_WGET_ITERATIONS`` lowers it for
iteration only, and every shortened run says so in its output so its evidence
cannot be mistaken for the acceptance one.
"""

from __future__ import annotations

from collections.abc import Mapping

DEFAULT_WGET_ITERATIONS = 12
ENVIRONMENT_VARIABLE = "WEST_STOCK_WGET_ITERATIONS"


def resolve_wget_iterations(environ: Mapping[str, str]) -> tuple[int, bool]:
    """Return ``(count, is_acceptance_workload)`` for the wget-repeat phase."""

    raw = environ.get(ENVIRONMENT_VARIABLE)
    if raw is None or not raw.strip():
        return DEFAULT_WGET_ITERATIONS, True
    try:
        value = int(raw.strip())
    except ValueError as error:
        raise ValueError(f"{ENVIRONMENT_VARIABLE} is not an integer: {raw}") from error
    if value <= 0:
        raise ValueError(f"{ENVIRONMENT_VARIABLE} must be positive")
    return value, value == DEFAULT_WGET_ITERATIONS


def wget_repeat_phase(iterations: int) -> str:
    """Return the guest fragment that reinstalls wget ``iterations`` times."""

    if iterations <= 0:
        raise ValueError("wget-repeat needs at least one iteration")
    loop = " ".join(str(index) for index in range(1, iterations + 1))
    acceptance = 1 if iterations == DEFAULT_WGET_ITERATIONS else 0
    return f"""printf 'STOCK_WGET_REPEAT iterations={iterations} acceptance={acceptance}\\n'
for iteration in {loop}; do
    printf 'STOCK_WGET_REBUILD_BEGIN iteration=%s\\n' "$iteration"
    "$brew" reinstall --keep-tmp --build-from-source wget
    /usr/local/bin/wget --timeout=60 --tries=1 -O "$work/wget-example.html" https://example.com/
    test -s "$work/wget-example.html"
    printf 'STOCK_WGET_REBUILD_OK iteration=%s\\n' "$iteration"
done
"""
