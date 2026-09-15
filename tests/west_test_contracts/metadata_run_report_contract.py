"""A metadata selection must say what it ran, what it skipped, and where the evidence is.

Three confusions in one session, all caused by silence rather than by missing
data:

* ``diag: guarded`` fixtures run without a bundle directory and print their
  verdict only in the job log, so an empty bundle listing read as "did not run".
* A selected test missing from the log's deploy sequence read as "dropped"; it
  was simply later in the profile's execution order, which nothing stated.
* The bundle root is ``<workspace-parent>/darling-debug``, and nothing printed
  it, so a rollup that scanned the manifest sibling reported tests as never run.

The strings below are the behaviour: they are what an operator reads when
deciding whether a test ran and where to look.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_selection import (  # noqa: E402
    metadata_run_summary,
    metadata_selection_plan,
    metadata_test_outcome,
)

# ``guarded-no-bundle-implies-not-run`` reproduces the reading this contract
# exists to prevent: a missing bundle described as the test not having run.
ARM = os.environ.get("WEST_METADATA_REPORT_ARM", "green")


def outcome(name: str, returncode: int, bundle: object | None) -> str:
    if ARM == "guarded-no-bundle-implies-not-run" and bundle is None:
        return f"  {name}: did not run (no bundle)"
    return metadata_test_outcome(name, returncode, bundle)


def run() -> None:
    plan = metadata_selection_plan(["first", "second", "third"], "/evidence/root")
    assert len(plan) == 2, plan
    assert "3 test(s)" in plan[0], plan
    assert "first, second, third" in plan[0], (
        "the plan must state the execution order, which is profile order"
    )
    assert "/evidence/root" in plan[1], (
        "the plan must name the resolved evidence root"
    )

    empty = metadata_selection_plan([], "/evidence/root")
    assert "0 test(s)" in empty[0] and "none" in empty[0], empty

    passed = outcome("guarded_fixture", 0, None)
    assert "passed" in passed, passed
    assert "job log" in passed, (
        "a guarded fixture without a bundle must say where its diagnostics are"
    )
    assert "did not run" not in passed and "not run" not in passed, (
        "a missing bundle must never read as the test not having run"
    )

    bundled = outcome("forensic_fixture", 0, "/evidence/root/bundle")
    assert "passed" in bundled and "/evidence/root/bundle" in bundled, bundled
    assert "job log" not in bundled, bundled

    failed = outcome("forensic_fixture", 7, "/evidence/root/bundle")
    assert "failed rc=7" in failed, failed

    summary = metadata_run_summary(
        {
            "selected": 5, "executed": 2, "passed": 1, "failed": 1,
            "duplicate": 1, "verdict": 1,
        }
    )
    assert "executed 2 (1 passed, 1 failed)" in summary, summary
    assert "skipped 2" in summary, summary
    assert "duplicate invocation 1" in summary and "reused verdict 1" in summary, summary
    assert "of 5 selected" in summary, summary

    nothing = metadata_run_summary({"selected": 3})
    assert "executed 0" in nothing and "skipped 0" in nothing, nothing

    print("PASS metadata-run-report-contract")


def main() -> int:
    if ARM == "green":
        run()
        return 0
    if ARM == "guarded-no-bundle-implies-not-run":
        try:
            run()
        except AssertionError as error:
            print(
                "RED arm (a missing bundle described as not having run) failed as "
                f"designed: {error}"
            )
            return 0
        print("RED arm unexpectedly passed")
        return 1
    raise SystemExit(f"unknown metadata report arm: {ARM}")


if __name__ == "__main__":
    raise SystemExit(main())
