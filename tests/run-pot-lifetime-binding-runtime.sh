#!/usr/bin/env bash
set -euo pipefail

: "${DPREFIX:?set DPREFIX}"
launcher="${DARLING_LAUNCHER:-${DARLING:-$DPREFIX/bin/darling}}"
if [ ! -x "$launcher" ]; then
	echo "Darling launcher not executable: $launcher" >&2
	exit 2
fi

guest_seconds=60
host_pid=
server_pid=

cleanup() {
	if [ -n "${server_pid:-}" ] && kill -0 "$server_pid" 2>/dev/null; then
		kill -KILL "$server_pid" 2>/dev/null || true
	fi
	if [ -n "${host_pid:-}" ] && kill -0 "$host_pid" 2>/dev/null; then
		kill -KILL "$host_pid" 2>/dev/null || true
	fi
	DPREFIX="$DPREFIX" "$launcher" shutdown >/dev/null 2>&1 || true
}
trap cleanup EXIT

children_of() {
	local parent="$1"
	ps -eo pid=,ppid= | awk -v ppid="$parent" '$2 == ppid { print $1 }'
}

descendants_of() {
	local parent="$1"
	local child
	for child in $(children_of "$parent"); do
		printf '%s\n' "$child"
		descendants_of "$child"
	done
}

wait_for_server() {
	local deadline=$((SECONDS + 30))
	while [ "$SECONDS" -lt "$deadline" ]; do
		server_pid="$(ps -eo pid=,ppid=,comm= | awk -v ppid="$host_pid" '$2 == ppid && $3 == "darlingserver" { print $1; exit }')"
		if [ -n "$server_pid" ]; then
			return 0
		fi
		sleep 0.1
	done
	echo "timed out waiting for darlingserver child of launcher $host_pid" >&2
	return 1
}

wait_for_guest_sleep() {
	local deadline=$((SECONDS + 30))
	local pid
	while [ "$SECONDS" -lt "$deadline" ]; do
		for pid in $(descendants_of "$server_pid"); do
			if ps -p "$pid" -o args= | grep -F -q "sleep $guest_seconds"; then
				return 0
			fi
		done
		sleep 0.1
	done
	echo "timed out waiting for guest sleep descendant of darlingserver $server_pid" >&2
	return 1
}

wait_gone() {
	local pid="$1"
	local deadline=$((SECONDS + 10))
	while [ "$SECONDS" -lt "$deadline" ]; do
		if ! kill -0 "$pid" 2>/dev/null; then
			return 0
		fi
		sleep 0.1
	done
	return 1
}

DPREFIX="$DPREFIX" "$launcher" shell /bin/sh -c "sleep $guest_seconds" &
host_pid=$!

wait_for_server
wait_for_guest_sleep
mapfile -t guest_pids < <(descendants_of "$server_pid")
if [ "${#guest_pids[@]}" -eq 0 ]; then
	echo "darlingserver $server_pid has no guest descendants" >&2
	exit 1
fi

kill -KILL "$server_pid"
wait_gone "$server_pid" || {
	echo "darlingserver survived SIGKILL: $server_pid" >&2
	exit 1
}

failed=0
for pid in "${guest_pids[@]}"; do
	if kill -0 "$pid" 2>/dev/null; then
		echo "guest descendant survived darlingserver death: $pid $(ps -p "$pid" -o args=)" >&2
		kill -KILL "$pid" 2>/dev/null || true
		failed=1
	fi
done
if [ "$failed" -ne 0 ]; then
	exit 1
fi

wait "$host_pid" >/dev/null 2>&1 || true
host_pid=
server_pid=
trap - EXIT
DPREFIX="$DPREFIX" "$launcher" shutdown >/dev/null 2>&1 || true
echo "POT_LIFETIME_BINDING_RUNTIME_OK"
