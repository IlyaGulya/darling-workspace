#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?west must provide the test prefix}"
: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
source "$repo/testkit/scripts/darling-guest-shell.sh"

# Keep the Linux dynamic loader's preload path free of list separators, and
# never inject the obstacle into compiler, launcher, or guest-shell startup.
unset LD_PRELOAD DARLING_STACK_GROWTH_PROBE
work=$(mktemp -d /tmp/darling-mldr-stack-growth.XXXXXX)
trap 'rm -rf -- "$work"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
timeout --kill-after=5 15 "${CC:-gcc}" -std=gnu11 -Wall -Wextra -fPIC -shared \
    "$repo/tests/mldr_stack_growth_obstacle.c" -o "$work/obstacle.so"

guest_program=$(cat <<'GUEST'
set -euo pipefail
status=0
# /usr/bin/env applies these variables only when it executes /bin/rm. The
# preload path is Linux-host absolute; the rm operand uses the guest host mount.
output=$(/usr/bin/env DARLING_STACK_GROWTH_PROBE=1 LD_PRELOAD="$1" \
    /bin/rm -f "/Volumes/SystemRoot$2/nonexistent" 2>&1) || status=$?
printf '%s\n' "$output"
case $'\n'"$output"$'\n' in
    *$'\nSTACK_GROWTH_OBSTACLE_READY\n'*) ;;
    *) printf 'MLDR_STACK_GROWTH_INFRASTRUCTURE_FAILURE missing obstacle readiness rc=%s\n' "$status" >&2; exit 1 ;;
esac
case "$status" in
    0) printf '%s\n' MLDR_STACK_GROWTH_OK ;;
    139)
        printf '%s\n' WEST_TEST_FAILURE_PHASE=run MLDR_STACK_GROWTH_BROKEN
        exit 139
        ;;
    *) printf 'MLDR_STACK_GROWTH_INFRASTRUCTURE_FAILURE rm rc=%s\n' "$status" >&2; exit 1 ;;
esac
GUEST
)

status=0
# Use a regular log, not a command-substitution pipe: prefix daemons can retain
# the launcher's output descriptors. West owns their shutdown and diagnostics.
darling_guest_shell "$DARLING_LAUNCHER" "$DPREFIX" 80 \
    'exec /usr/bin/env -i /bin/bash -c "$3" west-mldr-stack-growth "$1" "$2"' \
    west-mldr-stack-growth "$work/obstacle.so" "$work" "$guest_program" \
    >"$work/guest.log" 2>&1 || status=$?
output=$(<"$work/guest.log")
printf '%s\n' "$output"
if [ "$status" -ne 0 ]; then
    exit "$status"
fi
# A successful launcher alone is not proof the guest reached its verdict.
case $'\n'"$output"$'\n' in
    *$'\nMLDR_STACK_GROWTH_OK\n'*) ;;
    *) printf '%s\n' 'MLDR_STACK_GROWTH_INFRASTRUCTURE_FAILURE missing guest success verdict' >&2; exit 1 ;;
esac
