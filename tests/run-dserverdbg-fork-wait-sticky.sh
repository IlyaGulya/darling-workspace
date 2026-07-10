#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?set DPREFIX}"
: "${DSERVER_TEST_FAULT_FILE:?set DSERVER_TEST_FAULT_FILE}"
: "${DSERVER_TEST_TRACE_FILE:?set DSERVER_TEST_TRACE_FILE}"

darling="${DARLING:-$DPREFIX/bin/darling}"
dserverdbg="${DSERVERDBG:-$DPREFIX/bin/dserverdbg}"
launch_log="${DSERVER_TEST_TRACE_FILE}.launch"
launcher_pid=""

cleanup() {
	if [ -n "$launcher_pid" ]; then
		kill "$launcher_pid" 2>/dev/null || true
		wait "$launcher_pid" 2>/dev/null || true
	fi
	rm -f "$launch_log"
}
trap cleanup EXIT

if [ ! -x "$darling" ]; then
	printf 'missing darling launcher: %s\n' "$darling" >&2
	exit 1
fi
if [ ! -x "$dserverdbg" ]; then
	printf 'missing dserverdbg oracle: %s\n' "$dserverdbg" >&2
	exit 1
fi

rm -f "$DSERVER_TEST_FAULT_FILE"
rm -f "$DSERVER_TEST_TRACE_FILE"
rm -f "$launch_log"
rm -f "$DPREFIX/.darlingserver.sock" "$DPREFIX/.init.pid"

# Start only darlingserver. The oracle talks to the server directly, so shell
# startup cannot become the RED reason for a fork-wait proof.
timeout --kill-after=5 45 env \
	DPREFIX="$DPREFIX" \
	DSERVER_TEST_FAULT_FILE="$DSERVER_TEST_FAULT_FILE" \
	DSERVER_TEST_TRACE_FILE="$DSERVER_TEST_TRACE_FILE" \
	DSERVER_TEST_FORK_CHECKIN_TIMEOUT_SECONDS=1 \
	DSERVER_TEST_SKIP_LAUNCHD=1 \
	"$darling" /__west_dserverdbg_start_only__ >"$launch_log" 2>&1 &
launcher_pid=$!

for _ in $(seq 1 80); do
	if [ -S "$DPREFIX/.darlingserver.sock" ] || [ -f "$DPREFIX/.init.pid" ]; then
		break
	fi
	if ! kill -0 "$launcher_pid" 2>/dev/null; then
		break
	fi
	sleep 0.25
done

if kill -0 "$launcher_pid" 2>/dev/null; then
	printf 'DSERVERDBG_LAUNCHER_ALIVE=1\n'
else
	printf 'DSERVERDBG_LAUNCHER_ALIVE=0\n'
fi
if [ -f "$DPREFIX/.init.pid" ]; then
	printf 'DSERVERDBG_INIT_PID=%s\n' "$(sed -n '1p' "$DPREFIX/.init.pid")"
else
	printf 'DSERVERDBG_INIT_PID=missing\n'
fi
if [ -S "$DPREFIX/.darlingserver.sock" ]; then
	printf 'DSERVERDBG_SERVER_SOCKET=present\n'
else
	printf 'DSERVERDBG_SERVER_SOCKET=missing\n'
fi

set +e
env DPREFIX="$DPREFIX" DSERVER_TEST_FAULT_FILE="$DSERVER_TEST_FAULT_FILE" "$dserverdbg" fork-wait-sticky
rc=$?
set -e

printf 'ORACLE_RC=%s\n' "$rc"

kill "$launcher_pid" 2>/dev/null || true
wait "$launcher_pid" 2>/dev/null || true
launcher_pid=""

if [ "$rc" -ne 0 ] && [ -f "$launch_log" ]; then
	printf 'DSERVERDBG_LAUNCH_LOG_BEGIN\n'
	sed -n '1,120p' "$launch_log"
	printf 'DSERVERDBG_LAUNCH_LOG_END\n'
fi

if [ -f "$DSERVER_TEST_TRACE_FILE" ]; then
	grep -E '^(test_fault\.consume name=fork\.|process\.fork_wait\.force_interrupted|process\.checkin\.fork_(notify_parent|skip_semaphore))' "$DSERVER_TEST_TRACE_FILE" || true
fi

exit "$rc"
