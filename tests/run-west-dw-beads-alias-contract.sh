#!/usr/bin/env bash
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT"

python3 - <<'PY'
from west_commands.beads_aliases import normalize_beads_args


def check(args, unknown, want):
    got = normalize_beads_args(args, unknown)
    if got != want:
        raise SystemExit(f"{args!r} + {unknown!r}: got {got!r}, want {want!r}")


check(["comment", "dar-123", "done"], [], ["comments", "add", "dar-123", "done"])
check(["add-comment", "dar-123", "done"], [], ["comments", "add", "dar-123", "done"])
check(["comment-add", "dar-123", "done"], [], ["comments", "add", "dar-123", "done"])
check(["comments", "add", "dar-123", "done"], [], ["comments", "add", "dar-123", "done"])
check(["show", "dar-123"], ["--json"], ["show", "dar-123", "--json"])
PY

echo "PASS west-dw-beads-alias-contract"
