#!/usr/bin/env bash
set -euo pipefail
: "${DPREFIX:?set DPREFIX}"
: "${DARLING:?set DARLING}"
prefix="$DPREFIX"
log="$(mktemp /tmp/west-rootless-shutdown-session.XXXXXX)"
trap 'rm -f "$log"' EXIT
if ! env DARLING_PREFIX="$prefix" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 "$DARLING" shell /bin/bash --login -c 'sleep 1; printf "WEST_ROOTLESS_SHUTDOWN_WORKER_OK\n"' >"$log" 2>&1; then
	printf 'rootless shutdown worker failed before completion:\n' >&2
	cat "$log" >&2 || true
	exit 1
fi
if ! grep -F -x -q WEST_ROOTLESS_SHUTDOWN_WORKER_OK "$log"; then
	printf 'rootless shutdown worker returned without completion marker:\n' >&2
	cat "$log" >&2 || true
	exit 1
fi
env DARLING_PREFIX="$prefix" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 "$DARLING" shutdown
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
