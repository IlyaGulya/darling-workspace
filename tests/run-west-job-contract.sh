#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
export WEST_JOB_REGISTRY_ROOT="$tmp"
job="$repo/scripts/west-job.sh"
metadata_contract="$repo/tests/run-west-test-metadata-contract.sh"

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

# A caller may remove completed state while the registry persists. The next
# start for that exact path must reclaim only the dead registration.
rm -rf "$tmp/green"
"$job" start --state-dir "$tmp/green" -- /bin/bash -c 'printf GREEN_RESTARTED\\n'
wait_job --state-dir "$tmp/green"
grep -F -x -q 'GREEN_RESTARTED' "$tmp/green/log"

# Never reclaim an absent-state registration whose recorded process identity
# is still live.
live_state="$tmp/live-registry-only"
entry_name="$(printf '%s' "$live_state" | cksum | awk '{print $1 "-" $2}')"
live_entry="$tmp/.west-job-registry/$entry_name"
sleep 30 &
live_registry_pid=$!
mkdir "$live_entry"
printf '%s\n' "$live_state" >"$live_entry/state-dir"
printf '%s\n' "$live_registry_pid" >"$live_entry/pid"
awk '{print $22}' "/proc/$live_registry_pid/stat" >"$live_entry/start-time"
printf 'west test\n' >"$live_entry/command"
if "$job" start --state-dir "$live_state" -- /bin/true \
	>"$tmp/live-registry.out" 2>"$tmp/live-registry.err"; then
	echo 'live registry entry was unexpectedly stolen' >&2
	exit 1
fi
grep -F -q 'west job registry entry is still live:' "$tmp/live-registry.err"
kill "$live_registry_pid"
wait "$live_registry_pid" 2>/dev/null || true
rm -rf "$live_entry"

"$job" start --state-dir "$tmp/follow" -- /usr/bin/python3 -c '
import time
print("FOLLOW_FIRST", flush=True)
time.sleep(0.2)
print("FOLLOW_LAST", flush=True)
'
"$job" follow --state-dir "$tmp/follow" >"$tmp/follow.out"
grep -F -x -q 'FOLLOW_FIRST' "$tmp/follow.out"
grep -F -x -q 'FOLLOW_LAST' "$tmp/follow.out"
grep -F -x -q "completed rc=0 state=$tmp/follow" "$tmp/follow.out"

"$job" start --state-dir "$tmp/follow-failure" -- /bin/bash -c \
	'printf "FOLLOW_FAILURE\n"; exit 7'
set +e
"$job" follow --state-dir "$tmp/follow-failure" >"$tmp/follow-failure.out"
follow_failure_rc=$?
set -e
test "$follow_failure_rc" = 7
grep -F -x -q 'FOLLOW_FAILURE' "$tmp/follow-failure.out"
grep -F -x -q "completed rc=7 state=$tmp/follow-failure" "$tmp/follow-failure.out"

"$job" start --state-dir "$tmp/follow-resume" -- /usr/bin/python3 -c '
import time
print("FOLLOW_RESUME_READY", flush=True)
time.sleep(2)
'
set +e
"$job" follow --state-dir "$tmp/follow-resume" --timeout-seconds 1 \
	>"$tmp/follow-timeout.out" 2>"$tmp/follow-timeout.err"
follow_rc=$?
set -e
test "$follow_rc" = 124
grep -F -x -q 'FOLLOW_RESUME_READY' "$tmp/follow-timeout.out"
grep -F -q 'follow timed out; job remains running pid=' "$tmp/follow-timeout.err"
"$job" status --state-dir "$tmp/follow-resume" | grep -F -q 'running pid='
"$job" follow --state-dir "$tmp/follow-resume" >"$tmp/follow-resume.out"
grep -F -x -q "completed rc=0 state=$tmp/follow-resume" "$tmp/follow-resume.out"

# Exercise an attached observer against real logs, synchronizing mutations with
# observed progress rather than assuming scheduler timing between phase writes.
python3 - "$job" "$tmp" <<'PY'
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time

job, root = sys.argv[1], Path(sys.argv[2])
state = root / "activity state"
activity = root / "guest activity.log"
done = root / "activity.done"
subprocess.run([
    job, "start", "--state-dir", str(state), "--activity-log", str(activity),
    "--", sys.executable, "-c",
    "import pathlib,sys,time\n"
    "print('  runtime profile preflight: homebrew', flush=True)\n"
    "while not pathlib.Path(sys.argv[1] + '.build').exists(): time.sleep(0.02)\n"
    "print('  runtime phase start: GREEN build', flush=True)\n"
    "while not pathlib.Path(sys.argv[1]).exists(): time.sleep(0.02)\n"
    "print('  runtime phase complete: GREEN build (1.0s)', flush=True)\n",
    str(done),
], check=True)
observer = subprocess.Popen(
    [job, "follow", "--state-dir", str(state), "--timeout-seconds", "30"],
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)
pending = b""
transcript = b""


def progress(stage, path, minimum_age=None):
    global pending, transcript
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if b"\n" not in pending:
            ready, _, _ = select.select([observer.stdout], [], [], max(0, deadline - time.monotonic()))
            assert ready, (stage, transcript)
            data = os.read(observer.stdout.fileno(), 65536)
            assert data, (stage, transcript, observer.poll())
            pending += data
            transcript += data
            continue
        line, pending = pending.split(b"\n", 1)
        text = line.decode()
        if (text.startswith("following pid=") and f"log={str(path)!r}" in text
                and f"stage={stage!r}" in text):
            if minimum_age is not None:
                match = re.search(r"log-change-age=(\d+)s", text)
                assert match and int(match[1]) >= minimum_age, text
            return
    raise AssertionError((stage, transcript))


try:
    progress("runtime profile preflight: homebrew", state / "log")
    progress("unavailable", activity)
    Path(str(done) + ".build").touch()
    progress("runtime phase start: GREEN build", state / "log")
    activity.write_text("STOCK_PHASE=config")
    # A partial record must join the next append, not become a bogus stage.
    with activity.open("a") as output:
        output.write("ure\n")
    progress("STOCK_PHASE=configure", activity)
    replacement = activity.with_suffix(".replacement")
    replacement.write_text("WEST_GUEST_STAGE=run\n")
    replacement.replace(activity)
    progress("WEST_GUEST_STAGE=run", activity)
    # Truncate/regrow beyond the previous cursor between observations.
    activity.write_text("STOCK_PHASE=check\n" + "checking\n" * 100)
    progress("STOCK_PHASE=check", activity)
    activity.write_text("STOCK_PHASE=end\n")
    progress("STOCK_PHASE=end", activity)
    activity.unlink()
    progress("unavailable", activity)
    activity.write_text("WEST_GUEST_STAGE=cleanup\n")
    progress("WEST_GUEST_STAGE=cleanup", activity)
    done.touch()
    rest, errors = observer.communicate(timeout=10)
    transcript += rest
    assert observer.returncode == 0, (transcript, errors)
    assert b"runtime phase complete: GREEN build" in transcript, transcript
    assert f"completed rc=0 state={state}".encode() in transcript, transcript
finally:
    if observer.poll() is None:
        observer.terminate()
        observer.communicate(timeout=5)

# A fresh follow reconstructs the persisted watched paths/stage, and reports
# actual file age rather than pretending that observation was a fresh write.
old = time.time() - 60
os.utime(activity, (old, old))
resumed = subprocess.run(
    [job, "follow", "--state-dir", str(state), "--activity-log", str(root / "missing optional.log")],
    capture_output=True, text=True, timeout=10, check=True,
)
line = next(line for line in resumed.stdout.splitlines()
            if line.startswith("following pid=") and f"log={str(activity)!r}" in line)
assert "stage='WEST_GUEST_STAGE=cleanup'" in line, line
assert int(re.search(r"log-change-age=(\d+)s", line)[1]) >= 60, line
assert f"log={str(root / 'missing optional.log')!r} stage='unavailable'" in resumed.stdout
PY

if CODEX_CI=1 env -u WEST_JOB_ACTIVE -u WEST_JOB_STATE_DIR "$metadata_contract" \
	>"$tmp/direct-contract.out" 2>"$tmp/direct-contract.err"; then
	echo 'direct metadata contract unexpectedly ran in CODEX_CI' >&2
	exit 1
fi
grep -F -x -q 'metadata contract requires scripts/west-job.sh in CODEX_CI' \
	"$tmp/direct-contract.err"
"$job" start --state-dir "$tmp/metadata-contract" -- \
	"$metadata_contract" --transport-gate-probe
wait_job --state-dir "$tmp/metadata-contract"
grep -F -x -q 'WEST_METADATA_TRANSPORT_GATE_OK' "$tmp/metadata-contract/log"

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

# The cleanup audit must only trust states registered by west-job itself.  A
# live, lookalike directory under the same shared parent is not a west job and
# must not block cleanup or be read as one.
mkdir -p "$tmp/unregistered-west-test"
sleep 30 &
unregistered_pid=$!
printf 'west test\n' >"$tmp/unregistered-west-test/command"
printf '%s\n' "$unregistered_pid" >"$tmp/unregistered-west-test/pid"
awk '{print $22}' "/proc/$unregistered_pid/stat" >"$tmp/unregistered-west-test/start-time"
"$job" assert-no-live-west-test --state-root "$tmp"
kill "$unregistered_pid"
wait "$unregistered_pid" 2>/dev/null || true

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

# A shell cannot install its cleanup trap if SIGINT was ignored on entry.
# Python's signal.signal() fixtures above do not exercise that exec contract.
"$job" start --state-dir "$tmp/shell-cancel" -- /bin/bash -c '
	cancelled() {
		kill "$child"
		wait "$child" || true
		printf "SHELL_CANCEL_CLEANUP_COMPLETE\n"
		exit 0
	}
	trap cancelled INT
	sleep 30 &
	child=$!
	printf "SHELL_CANCEL_READY\n"
	wait "$child"
'
while ! grep -F -x -q SHELL_CANCEL_READY "$tmp/shell-cancel/log"; do
	kill -0 "$(<"$tmp/shell-cancel/pid")" 2>/dev/null || exit 1
done
"$job" cancel --state-dir "$tmp/shell-cancel"
if wait_job --state-dir "$tmp/shell-cancel"; then
	echo 'cancelled shell job unexpectedly succeeded' >&2
	exit 1
fi
test "$(<"$tmp/shell-cancel/rc")" = 143
grep -F -x -q SHELL_CANCEL_CLEANUP_COMPLETE "$tmp/shell-cancel/log"

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

env WEST_JOB_CANCEL_GRACE_SECONDS=1 "$job" start \
	--state-dir "$tmp/nested-session" -- bash -c '
		setsid sh -c "echo \\\$\\$ > \"$1\"; exec sleep 30" &
		printf "NESTED_SESSION_READY\\n"
		wait
	' bash "$tmp/nested-session-pid"
while [[ ! -s "$tmp/nested-session-pid" ]] || \
	! grep -F -x -q 'NESTED_SESSION_READY' "$tmp/nested-session/log"; do
	:
done
nested_session_pid="$(<"$tmp/nested-session-pid")"
WEST_JOB_CANCEL_GRACE_SECONDS=1 "$job" cancel \
	--state-dir "$tmp/nested-session" >/dev/null
if wait_job --state-dir "$tmp/nested-session"; then
	echo 'nested-session job unexpectedly succeeded' >&2
	exit 1
fi
if kill -0 "$nested_session_pid" 2>/dev/null; then
	echo "nested session survived cancellation: $nested_session_pid" >&2
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

# Relative state paths are canonicalized before registry identity is recorded,
# so later callers may address them from another working directory.
relative_cwd="$tmp/relative-cwd"
custom_registry="$tmp/custom-registry"
mkdir -p "$relative_cwd" "$custom_registry"
(
	cd "$relative_cwd"
	WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" start \
		--state-dir relative-state -- /bin/true
)
relative_state="$relative_cwd/relative-state"
wait_job --state-dir "$relative_state"
relative_entry_name="$(printf '%s' "$relative_state" | cksum | awk '{print $1 "-" $2}')"
relative_entry="$custom_registry/.west-job-registry/$relative_entry_name"
test "$(<"$relative_entry/state-dir")" = "$relative_state"

# The cleanup gate defaults to the same custom global registry as start.
WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" start \
	--state-dir "$tmp/custom-live-west-test" -- \
	env "PATH=$tmp/bin:$PATH" west test
while [[ ! -f "$tmp/custom-live-west-test/command-pid" ]] || \
	! grep -F -x -q 'WEST_TEST_JOB_READY' "$tmp/custom-live-west-test/log"; do
	:
done
if WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" assert-no-live-west-test \
	>"$tmp/custom-live-west-test.out" 2>"$tmp/custom-live-west-test.err"; then
	echo 'custom-registry cleanup audit missed live west test' >&2
	exit 1
fi
grep -F -x -q \
	"cleanup audit blocked by live west test job: $tmp/custom-live-west-test" \
	"$tmp/custom-live-west-test.err"
"$job" cancel --state-dir "$tmp/custom-live-west-test" >/dev/null
if wait_job --state-dir "$tmp/custom-live-west-test"; then
	echo 'custom-registry cancelled west test unexpectedly succeeded' >&2
	exit 1
fi
WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" assert-no-live-west-test

# A reservation interrupted after the runner publishes its state identity is
# reconciled into the registry and preserved rather than mistaken for stale.
partial_live_state="$tmp/partial-live-state"
mkdir "$partial_live_state"
sleep 30 &
partial_live_pid=$!
printf '%s\n' "$partial_live_pid" >"$partial_live_state/pid"
awk '{print $22}' "/proc/$partial_live_pid/stat" >"$partial_live_state/start-time"
partial_live_name="$(printf '%s' "$partial_live_state" | cksum | awk '{print $1 "-" $2}')"
partial_live_entry="$custom_registry/.west-job-registry/$partial_live_name"
mkdir "$partial_live_entry"
printf '%s\n' "$partial_live_state" >"$partial_live_entry/state-dir"
printf 'west test partial-live\n' >"$partial_live_entry/command"
WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" start \
	--state-dir "$tmp/reconcile-trigger" -- /bin/true
wait_job --state-dir "$tmp/reconcile-trigger"
test "$(<"$partial_live_entry/pid")" = "$partial_live_pid"
test "$(<"$partial_live_entry/start-time")" = "$(<"$partial_live_state/start-time")"
kill "$partial_live_pid"
wait "$partial_live_pid" 2>/dev/null || true

# An interrupted reservation with no live state identity is safely removed, so
# the exact state path can be retried.
partial_stale_state="$tmp/partial-stale-state"
partial_stale_name="$(printf '%s' "$partial_stale_state" | cksum | awk '{print $1 "-" $2}')"
partial_stale_entry="$custom_registry/.west-job-registry/$partial_stale_name"
mkdir "$partial_stale_entry"
printf '%s\n' "$partial_stale_state" >"$partial_stale_entry/state-dir"
WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" start \
	--state-dir "$partial_stale_state" -- /bin/true
wait_job --state-dir "$partial_stale_state"

# Many interrupted pre-publication entries are removed by one later
# reservation instead of accumulating beyond the status enumeration cap.
for ((index = 0; index < 129; index++)); do
	mkdir "$custom_registry/.west-job-registry/interrupted-$index"
done
WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" start \
	--state-dir "$tmp/interrupted-prune-trigger" -- /bin/true
wait_job --state-dir "$tmp/interrupted-prune-trigger"
interrupted_count=0
for entry in "$custom_registry/.west-job-registry"/*; do
	if [[ -d "$entry" && ! -L "$entry" ]]; then
		((interrupted_count += 1))
	fi
done
if ((interrupted_count > 1)); then
	echo "interrupted registry entries accumulated: $interrupted_count" >&2
	exit 1
fi

# Reservation prunes completed entries under the registry lock, keeping a long
# sequence of distinct completed jobs below the status collector's hard cap.
for ((index = 0; index < 129; index++)); do
	state="$tmp/sequential-$index"
	WEST_JOB_REGISTRY_ROOT="$custom_registry" "$job" start \
		--state-dir "$state" -- /bin/true
	wait_job --state-dir "$state"
done
registry_count=0
for entry in "$custom_registry/.west-job-registry"/*; do
	if [[ -d "$entry" && ! -L "$entry" ]]; then
		((registry_count += 1))
	fi
done
if ((registry_count > 1)); then
	echo "completed registry entries accumulated: $registry_count" >&2
	exit 1
fi

printf 'PASS west-job-contract\n'
