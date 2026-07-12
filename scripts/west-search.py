#!/usr/bin/env python3
"""Run a bounded ripgrep over explicitly named source roots."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "west_commands"))

from test_execution import process_output_text, run_bounded


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pattern")
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=30)
    parser.add_argument("--rg", default="rg", help=argparse.SUPPRESS)
    parser.add_argument("--glob", action="append", default=[])
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be > 0")
    roots = [root.expanduser().resolve() for root in args.roots]
    missing = [str(root) for root in roots if not root.exists()]
    if missing:
        parser.error("search root does not exist: " + ", ".join(missing))
    command = [args.rg, "--line-number", "--color=never"]
    for glob in args.glob:
        command.extend(("--glob", glob))
    command.extend(("--", args.pattern, *(str(root) for root in roots)))
    result = run_bounded(
        command,
        cwd=Path.cwd(),
        env=None,
        timeout_seconds=args.timeout_seconds,
        capture_output=True,
    )
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.timed_out:
        sys.stderr.write(
            f"west-search: timed out after {args.timeout_seconds}s; process group reaped\n"
        )
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
