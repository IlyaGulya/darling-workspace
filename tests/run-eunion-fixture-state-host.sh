#!/usr/bin/env bash
# Exercise the real fixture preparer, including safe refusal before replacement.
set -euo pipefail
root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
preparer="${1:-$root/experiments/e-union/run.sh}"
: "${XNU_SRC_ROOT:?select the production XNU source with XNU_SRC_ROOT}"
state="${DW_STATE_ROOT:-$(cd "$root/.." && pwd)/.west-state}"
mkdir -p "$state/h"
work="$(mktemp -d "$state/h/fXXXXXXX")"
trap 'rm -rf -- "$work"' EXIT

mkdir "$work/f"
printf 'KEEP\n' > "$work/f/keep"
if EUNION_FIXTURE_OWNER=first bash "$preparer" --prepare-fixture "$work/f"; then
    echo 'FAIL preparer accepted an unknown existing fixture' >&2
    exit 1
fi
printf 'KEEP\n' | cmp -s - "$work/f/keep"

EUNION_FIXTURE_OWNER=first bash "$preparer" --prepare-fixture "$work/o"
printf 'OWNED\n' > "$work/o/keep"
if EUNION_FIXTURE_OWNER=second bash "$preparer" --prepare-fixture "$work/o"; then
    echo 'FAIL preparer accepted a different fixture owner' >&2
    exit 1
fi
printf 'OWNED\n' | cmp -s - "$work/o/keep"
EUNION_FIXTURE_OWNER=first bash "$preparer" --prepare-fixture "$work/o"
test ! -e "$work/o/keep"
printf 'RETAIN\n' > "$work/o/keep"
if XNU_SRC_ROOT="$work/missing" EUNION_FIXTURE_OWNER=first \
    bash "$preparer" --prepare-fixture "$work/o"; then
    echo 'FAIL preparer accepted a missing source prerequisite' >&2
    exit 1
fi
printf 'RETAIN\n' | cmp -s - "$work/o/keep"

ln -s "$work/f" "$work/l"
if EUNION_FIXTURE_OWNER=first bash "$preparer" --prepare-fixture "$work/l"; then
    echo 'FAIL preparer replaced a symlink fixture root' >&2
    exit 1
fi
test -L "$work/l"
printf 'KEEP\n' | cmp -s - "$work/f/keep"
if DW_STATE_ROOT="$work/new" EUNION_FIXTURE_OWNER=first \
    bash "$preparer" --prepare-fixture "$work/new"; then
    echo 'FAIL preparer accepted its entire state root as a fixture' >&2
    exit 1
fi
test ! -e "$work/new"
printf 'PASS eunion-fixture-state-host\n'
