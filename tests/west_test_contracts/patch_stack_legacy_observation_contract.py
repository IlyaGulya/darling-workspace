#!/usr/bin/env python3
"""Static policy contract for legacy-mbox observation boundaries."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    host_tier = (ROOT / "ci/run-test-tier.sh").read_text()
    oracle = (ROOT / ".github/workflows/patch-stack-lock-first.yml").read_text()
    patch = (ROOT / "west_commands/patch.py").read_text()

    host = host_tier.split("\thost)", 1)[1].split("\tguest-smoke)", 1)[0]
    assert "exec west test --profile homebrew --env host --materialize-profile" in host
    assert "--legacy-mbox" not in host and "--lock-first" not in host
    assert oracle.count("west patch apply --profile homebrew --legacy-mbox") == 1
    assert "west patch apply --profile homebrew \\\n            --lock-first-evidence" in oracle
    assert "west patch apply --profile homebrew --lock-first" not in oracle
    for marker in (
        "PATCH_STACK_MODE=",
        "PATCH_STACK_REPLAY ",
        "elapsed_replay_seconds={elapsed:.3f}",
        "warning: --legacy-mbox is deprecated for homebrew",
    ):
        assert marker in patch, marker
    print("patch-stack legacy-observation policy: PASS")


if __name__ == "__main__":
    main()
