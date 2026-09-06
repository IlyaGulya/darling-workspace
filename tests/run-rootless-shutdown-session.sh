#!/usr/bin/env bash
set -euo pipefail
: "${DPREFIX:?set DPREFIX}"
: "${DARLING:?set DARLING}"
prefix="$DPREFIX"
source "$(dirname "$0")/../testkit/scripts/darling-guest-shell.sh"
log="$(mktemp /tmp/west-rootless-shutdown-session.XXXXXX)"
shell_job=
cleanup() {
	local status=$?
	if [ -n "$shell_job" ]; then
		env DARLING_PREFIX="$prefix" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 "$DARLING" shutdown || true
		wait "$shell_job" || true
	fi
	rm -f "$log"
	exit "$status"
}
trap cleanup EXIT
export DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1
darling_guest_shell "$DARLING" "$prefix" 45 '
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
env DARLING_PREFIX="$prefix" "$DARLING" shutdown
# A force-stopped shell need not return success. Its processes must be gone
# before shutdown returns, while the RPC server remains available until then.
for pid in "$shell_pid" "$first_pid" "$second_pid"; do
	if stat="$(cat "/proc/$pid/stat" 2>/dev/null)"; then
		state="${stat##*) }"
		state="${state%% *}"
		if [ "$state" != Z ] && [ "$state" != X ]; then
			printf 'rootless shutdown left prefix-owned process(es): %s\n' "$pid" >&2
			exit 1
		fi
	elif [ -e "/proc/$pid/stat" ]; then
		printf 'rootless shutdown cannot read process state: %s\n' "$pid" >&2
		exit 1
	fi
done
wait "$shell_job" || true
shell_job=
if grep -E -q 'Failed to interrupt_enter|interrupt_enter failed' "$log"; then
	printf 'rootless shutdown lost RPC service before guest termination:\n' >&2
	cat "$log" >&2
	exit 1
fi
for attempt in $(seq 1 20); do
	left=()
	for proc in /proc/[0-9]*; do
		[ -r "$proc/environ" ] || continue
		if { tr '\0' '\n' <"$proc/environ"; } 2>/dev/null | grep -F -x "DARLING_PREFIX=$prefix" >/dev/null; then
			executable="$(readlink "$proc/exe" 2>/dev/null || true)"
			case "$executable" in "$prefix"/*) left+=("${proc##*/}");; esac
		fi
	done
	[ "${#left[@]}" -eq 0 ] && break
	if [ "$attempt" -eq 20 ]; then
		printf 'rootless shutdown left prefix-owned process(es): %s\n' "${left[*]}" >&2
		ps -o pid=,ppid=,pgid=,sid=,comm=,args= -p "$(IFS=,; printf '%s' "${left[*]}")" >&2 || true
		for pid in "${left[@]}"; do printf 'rootless shutdown executable %s: %s\n' "$pid" "$(readlink "/proc/$pid/exe" 2>/dev/null || printf '<unreadable>')" >&2; done
		exit 1
	fi
	sleep 0.1
done
for state in .init.pid .darlingserver.sock; do
	if [ -e "$prefix/$state" ]; then
		printf 'rootless shutdown left prefix state: %s\n' "$state" >&2
		exit 1
	fi
done
printf 'WEST_ROOTLESS_SHUTDOWN_SESSION_OK\n'
