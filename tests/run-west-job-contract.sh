#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
job="$repo/scripts/west-job.sh"

"$job" start --state-dir "$tmp/green" -- /bin/bash -c 'printf GREEN_JOB\\n'
"$job" wait --state-dir "$tmp/green"
grep -F -x -q 'GREEN_JOB' "$tmp/green/log"
test "$(<"$tmp/green/rc")" = 0
"$job" status --state-dir "$tmp/green" | grep -F -x -q "completed rc=0 state=$tmp/green"

"$job" start --state-dir "$tmp/cancelled" -- /bin/bash -c 'sleep 30'
pid="$(<"$tmp/cancelled/pid")"
"$job" cancel --state-dir "$tmp/cancelled"
if "$job" wait --state-dir "$tmp/cancelled"; then
	echo 'cancelled job unexpectedly succeeded' >&2
	exit 1
fi
test "$(<"$tmp/cancelled/rc")" = 143
if kill -0 "$pid" 2>/dev/null; then
	echo "cancelled job is still alive: $pid" >&2
	exit 1
fi

printf 'PASS west-job-contract\n'
