#!/usr/bin/env bash
set -euo pipefail

src="${DARLING_SRC_ROOT:?set DARLING_SRC_ROOT}"
script="$src/src/sandbox/sandbox-exec.sh"
test -f "$script"

out="$(sh "$script" -f ignored.sb -D KEY=VALUE -p '(version 1)' -n name -- /bin/sh -c 'printf "%s:%s" "$1" "$2"' sh alpha beta)"
test "$out" = "alpha:beta"

out2="$(sh "$script" -q /bin/sh -c 'printf ok')"
test "$out2" = "ok"
