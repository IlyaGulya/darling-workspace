#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
job="$repo/scripts/west-job.sh"

cleanup() {
	local state
	for state in "$tmp"/*; do
		[[ -f "$state/pid" ]] || continue
		WEST_JOB_CANCEL_GRACE_SECONDS=1 "$job" cancel --state-dir "$state" >/dev/null 2>&1 || true
	done
	rm -rf "$tmp"
}
trap cleanup EXIT

wait_job() {
	env -u CODEX_CI "$job" wait "$@"
}

"$job" start --state-dir "$tmp/green" -- /bin/bash -c 'printf GREEN_JOB\\n'
wait_job --state-dir "$tmp/green"
grep -F -x -q 'GREEN_JOB' "$tmp/green/log"
test "$(<"$tmp/green/rc")" = 0
"$job" status --state-dir "$tmp/green" | grep -F -x -q "completed rc=0 state=$tmp/green"

mkdir -p "$tmp/bin"
cat >"$tmp/bin/west" <<'SCRIPT'
#!/usr/bin/env bash
set -euo pipefail
test "${1:-}" = test
printf 'WEST_TEST_JOB_READY\n'
exec sleep 30
SCRIPT
chmod +x "$tmp/bin/west"
"$job" start --state-dir "$tmp/live-west-test" -- \
	env "PATH=$tmp/bin:$PATH" west test
while [[ ! -f "$tmp/live-west-test/command-pid" ]] || \
	! grep -F -x -q 'WEST_TEST_JOB_READY' "$tmp/live-west-test/log"; do
	:
done
if "$job" assert-no-live-west-test --state-root "$tmp" \
	>"$tmp/live-west-test.out" 2>"$tmp/live-west-test.err"; then
	echo 'cleanup audit guard unexpectedly allowed a live west test job' >&2
	exit 1
fi
grep -F -x -q \
	"cleanup audit blocked by live west test job: $tmp/live-west-test" \
	"$tmp/live-west-test.err"
"$job" cancel --state-dir "$tmp/live-west-test" >/dev/null
if wait_job --state-dir "$tmp/live-west-test"; then
	echo 'live west test job unexpectedly succeeded after cancellation' >&2
	exit 1
fi
"$job" assert-no-live-west-test --state-root "$tmp"

"$job" start --state-dir "$tmp/wait" -- /usr/bin/python3 -c '
import time
time.sleep(0.3)
'
started_at="$(date +%s%N)"
wait_job --state-dir "$tmp/wait"
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
PATH="$tmp/no-tail:$PATH" env -u CODEX_CI "$job" wait --state-dir "$tmp/no-tail-wait"
test "$(<"$tmp/no-tail-wait/rc")" = 0

"$job" start --state-dir "$tmp/agent" -- /usr/bin/python3 -c '
import signal
import sys
import time

def cancelled(_signal, _frame):
    print("AGENT_CANCELLED", flush=True)
    sys.exit(0)

signal.signal(signal.SIGINT, cancelled)
print("AGENT_READY", flush=True)
time.sleep(30)
'
while [[ ! -f "$tmp/agent/command-pid" ]] || ! grep -F -x -q 'AGENT_READY' "$tmp/agent/log"; do
	:
done
if CODEX_CI=1 "$job" wait --state-dir "$tmp/agent" >"$tmp/agent.out" 2>"$tmp/agent.err"; then
	echo 'agent-mode wait unexpectedly succeeded' >&2
	exit 1
fi
grep -F -x -q 'west-job wait is unsafe under CODEX_CI; use west-job.sh status to poll the state directory' "$tmp/agent.err"
"$job" status --state-dir "$tmp/agent" | grep -F -x -q "running pid=$(<"$tmp/agent/pid") state=$tmp/agent"
"$job" cancel --state-dir "$tmp/agent"
if wait_job --state-dir "$tmp/agent"; then
	echo 'cancelled agent-mode job unexpectedly succeeded' >&2
	exit 1
fi
test "$(<"$tmp/agent/rc")" = 143
grep -F -x -q 'AGENT_CANCELLED' "$tmp/agent/log"

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
wait_job --state-dir "$tmp/cancelled" >/dev/null 2>&1 &
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

env WEST_JOB_CANCEL_GRACE_SECONDS=1 "$job" start --state-dir "$tmp/unresponsive" -- \
	bash -c 'trap "" INT; printf "UNRESPONSIVE_READY\\n"; sleep 30'
while [[ ! -f "$tmp/unresponsive/command-pid" ]] || ! grep -F -x -q 'UNRESPONSIVE_READY' "$tmp/unresponsive/log"; do
	if ! kill -0 "$(<"$tmp/unresponsive/pid")" 2>/dev/null; then
		echo 'unresponsive job exited before becoming ready' >&2
		exit 1
	fi
done
unresponsive_output="$(WEST_JOB_CANCEL_GRACE_SECONDS=1 "$job" cancel --state-dir "$tmp/unresponsive")"
printf '%s\n' "$unresponsive_output" | grep -F -q 'cancelling unresponsive command-pid=' || {
	printf '%s\n' "$unresponsive_output" >&2
	exit 1
}
if wait_job --state-dir "$tmp/unresponsive"; then
	echo 'unresponsive job unexpectedly succeeded' >&2
	exit 1
fi

"$job" start --state-dir "$tmp/invalid-grace" -- bash -c 'sleep 30'
while [[ ! -f "$tmp/invalid-grace/command-pid" ]]; do
	:
done
if WEST_JOB_CANCEL_GRACE_SECONDS=zero "$job" cancel --state-dir "$tmp/invalid-grace" \
	>"$tmp/invalid-grace.out" 2>"$tmp/invalid-grace.err"; then
	echo 'invalid cancel grace unexpectedly succeeded' >&2
	exit 1
fi
grep -F -x -q 'WEST_JOB_CANCEL_GRACE_SECONDS must be a positive integer' "$tmp/invalid-grace.err"
WEST_JOB_CANCEL_GRACE_SECONDS=1 "$job" cancel --state-dir "$tmp/invalid-grace" >/dev/null
if wait_job --state-dir "$tmp/invalid-grace"; then
	echo 'invalid grace cleanup job unexpectedly succeeded' >&2
	exit 1
fi

printf 'PASS west-job-contract\n'
