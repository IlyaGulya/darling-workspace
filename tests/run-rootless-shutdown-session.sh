#!/usr/bin/env bash
set -euo pipefail
: "${DPREFIX:?set DPREFIX}"
prefix="$DPREFIX"
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DARLING_WORKSPACE_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
source "$DARLING_WORKSPACE_ROOT/scripts/darling-runtime.sh"
darling_resolve_runtime "${DARLING_RUNTIME_ROOT:-}" "$prefix"
source "$DARLING_WORKSPACE_ROOT/testkit/scripts/darling-guest-shell.sh"
log_root="${DW_STATE_ROOT:-${DARLING_WORKSPACE_ROOT%/*}/.west-state}/shutdown-session"
mkdir -p "$log_root"
log="$(mktemp "$log_root/guest-XXXXXX.log")"
printf 'WEST_ROOTLESS_SHUTDOWN_LOG=%s\n' "$log"
shell_job=
cleanup() {
	local status=$?
	if [ -n "$shell_job" ]; then
		if ! "$DARLING_LAUNCHER" shutdown >>"$log" 2>&1; then
			printf 'rootless shutdown cleanup failed; retained log: %s\n' "$log" >&2
			status=1
		fi
		wait "$shell_job" || true
	fi
	printf 'rootless shutdown evidence: %s\n' "$log"
	exit "$status"
}
trap cleanup EXIT
darling_guest_shell "$DARLING_LAUNCHER" "$prefix" 45 '
	/bin/sleep 120 &
	first=$!
	/bin/sleep 120 &
	second=$!
	printf "WEST_ROOTLESS_SHUTDOWN_READY %s %s %s\n" "$$" "$first" "$second"
	wait
' >"$log" 2>&1 &
shell_job=$!
ready=
for attempt in $(seq 1 100); do
	ready="$(grep '^WEST_ROOTLESS_SHUTDOWN_READY ' "$log" || true)"
	[ -n "$ready" ] && break
	kill -0 "$shell_job" 2>/dev/null || break
	sleep 0.1
done
if [ -z "$ready" ]; then
	printf 'rootless shutdown worker failed before readiness:\n' >&2
	cat "$log" >&2 || true
	exit 1
fi
read -r marker shell_pid first_pid second_pid <<<"$ready"
# Both lifecycle owners must acknowledge the same stopped state successfully.
"$DARLING_LAUNCHER" shutdown &
first_shutdown=$!
"$DARLING_LAUNCHER" shutdown &
second_shutdown=$!
shutdown_status=0
wait "$first_shutdown" || shutdown_status=1
wait "$second_shutdown" || shutdown_status=1
[ "$shutdown_status" -eq 0 ] || exit "$shutdown_status"
# Guest PID values are namespace-local, not host /proc identities. The final
# ownership census below proves the whole selected prefix is gone without
# accidentally inspecting an unrelated host process with the same numeric PID.
for pid in "$shell_pid" "$first_pid" "$second_pid"; do
	case "$pid" in ''|*[!0-9]*) echo "invalid guest readiness PID: $pid" >&2; exit 1 ;; esac
	[ "$pid" -gt 0 ] || exit 1
done
[ "$shell_pid" != "$first_pid" ] && [ "$shell_pid" != "$second_pid" ] && \
	[ "$first_pid" != "$second_pid" ] || exit 1
wait "$shell_job" || true
shell_job=
if grep -E -q 'Failed to interrupt_enter|interrupt_enter failed' "$log"; then
	printf 'rootless shutdown lost RPC service before guest termination:\n' >&2
	cat "$log" >&2
	exit 1
fi
for attempt in $(seq 1 20); do
	if ! left=$(darling_prefix_owned_pids "$prefix"); then
		printf 'rootless shutdown cannot establish prefix process ownership\n' >&2
		exit 1
	fi
	[ -z "$left" ] && break
	if [ "$attempt" -eq 20 ]; then
		printf 'rootless shutdown left prefix-owned process(es): %s\n' "$left" >&2
		ps -o pid=,ppid=,pgid=,sid=,comm=,args= -p "${left//$'\n'/,}" >&2 || true
		for pid in $left; do printf 'rootless shutdown executable %s: %s\n' "$pid" "$(readlink "/proc/$pid/exe" 2>/dev/null || printf '<unreadable>')" >&2; done
		exit 1
	fi
	sleep 0.1
done
for state in .init.pid .darlingserver.sock; do
	if [ -e "$prefix/$state" ] || [ -L "$prefix/$state" ]; then
		printf 'rootless shutdown left prefix state: %s\n' "$state" >&2
		exit 1
	fi
done
printf 'WEST_ROOTLESS_SHUTDOWN_SESSION_OK\n'
