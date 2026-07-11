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

"$job" start --state-dir "$tmp/wait" -- /usr/bin/python3 -c '
import time
time.sleep(0.3)
'
started_at="$(date +%s%N)"
"$job" wait --state-dir "$tmp/wait"
elapsed_ns="$(( $(date +%s%N) - started_at ))"
if ((elapsed_ns < 150000000)); then
	echo "wait returned before its live command finished: ${elapsed_ns}ns" >&2
	exit 1
fi
test "$(<"$tmp/wait/rc")" = 0

mkdir "$tmp/no-tail"
ln -s /usr/bin/false "$tmp/no-tail/tail"
"$job" start --state-dir "$tmp/no-tail-wait" -- /usr/bin/python3 -c '
import time
time.sleep(0.3)
'
PATH="$tmp/no-tail:$PATH" "$job" wait --state-dir "$tmp/no-tail-wait"
test "$(<"$tmp/no-tail-wait/rc")" = 0

"$job" start --state-dir "$tmp/cancelled" -- /usr/bin/python3 -c '
import signal
import sys
import time

def cancelled(_signal, _frame):
    print("COOPERATIVE_CANCEL", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, cancelled)
print("COOPERATIVE_READY", flush=True)
time.sleep(30)
'
pid="$(<"$tmp/cancelled/pid")"
"$job" wait --state-dir "$tmp/cancelled" >/dev/null 2>&1 &
wait_pid=$!
while [[ ! -f "$tmp/cancelled/command-pid" ]] || ! grep -F -x -q 'COOPERATIVE_READY' "$tmp/cancelled/log"; do
	if ! kill -0 "$wait_pid" 2>/dev/null; then
		echo 'cancelled job exited before becoming ready' >&2
		exit 1
	fi
done
command_pid="$(<"$tmp/cancelled/command-pid")"
"$job" cancel --state-dir "$tmp/cancelled"
if wait "$wait_pid"; then
	echo 'cancelled job unexpectedly succeeded' >&2
	exit 1
fi
test "$(<"$tmp/cancelled/rc")" = 143
grep -F -x -q 'COOPERATIVE_CANCEL' "$tmp/cancelled/log"
if kill -0 "$pid" 2>/dev/null; then
	echo "cancelled job is still alive: $pid" >&2
	exit 1
fi
if kill -0 "$command_pid" 2>/dev/null; then
	echo "cancelled command is still alive: $command_pid" >&2
	exit 1
fi

printf 'PASS west-job-contract\n'
