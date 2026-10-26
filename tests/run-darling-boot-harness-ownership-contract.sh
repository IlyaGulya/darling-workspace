#!/usr/bin/env bash
# Host-only ownership regression: shared executable + exact prefix directory FD.
# A PID record, argv or environment alone cannot claim a process. No guest boots.
set -euo pipefail
workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
harness="${DARLING_BOOT_HARNESS_UNDER_TEST:-$workspace_root/scripts/darling-boot-run.sh}"
state="${DW_STATE_ROOT:-$workspace_root/../.west-state}/contracts/boot-ownership"
mkdir -p "$state"
tmp="$(mktemp -d "$state/run.XXXXXX")"
pids=()
cleanup() {
	for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
	for pid in "${pids[@]}"; do wait "$pid" 2>/dev/null || true; done
	rm -rf -- "$tmp"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
prefix="$tmp/prefix"
other="$tmp/other-prefix"
mkdir -p "$prefix" "$other" "$tmp/runtime/bin"
printf '#include <unistd.h>\nint main(void) { for (;;) pause(); }\n' > "$tmp/holder.c"
mkdir -p "$prefix/bin"
# Refusing host sentinel for the old observer's launcher-presence prerequisite;
# neither implementation may execute it in --list-owned mode.
cp /bin/false "$prefix/bin/darling"
cc -O2 -Wall -Wextra -o "$tmp/runtime/bin/darlingserver" "$tmp/holder.c"

env -u DPREFIX -u DARLING_PREFIX "$tmp/runtime/bin/darlingserver" 4 3 prefix 6 1000 1000 8 0 3<"$prefix" &
server=$!; pids+=("$server")
env -u DPREFIX -u DARLING_PREFIX "$tmp/runtime/bin/darlingserver" 4 3 other-prefix 6 1000 1000 8 0 3<"$other" &
other_server=$!; pids+=("$other_server")
DPREFIX="$prefix" DARLING_PREFIX="$prefix" "$tmp/runtime/bin/darlingserver" "--prefix=$prefix" 3<"$other" &
observer=$!; pids+=("$observer")
DPREFIX="$prefix" DARLING_PREFIX="$prefix" "$tmp/runtime/bin/darlingserver" &
env_observer=$!; pids+=("$env_observer")
sleep 60 &
recorded_unrelated=$!; pids+=("$recorded_unrelated")
printf '%s\n' "$recorded_unrelated" > "$prefix/.init.pid"
# Wait for exec, not an arbitrary process readiness interval.
for pid in "$server" "$other_server" "$observer" "$env_observer"; do
	for attempt in {1..100}; do
		[[ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" == "$tmp/runtime/bin/darlingserver" ]] && break
		sleep 0.01
	done
	[[ "$(readlink "/proc/$pid/exe")" == "$tmp/runtime/bin/darlingserver" ]]
done

if "$harness" --prefix "$prefix" --list-owned > "$tmp/observed" 2>&1; then
	:
else
	result=$?
	cat "$tmp/observed" >&2
	exit "$result"
fi
has_pid() { grep -Eq "^$1$" "$tmp/observed"; }
has_pid "$server" || { cat "$tmp/observed"; echo 'FAIL missing capability owner' >&2; exit 1; }
for pid in "$other_server" "$observer" "$env_observer" "$recorded_unrelated"; do
	if has_pid "$pid"; then
		cat "$tmp/observed"
		echo "FAIL unrelated process classified as owned: $pid" >&2
		exit 1
	fi
	kill -0 "$pid"
done
[[ "$(cat "$prefix/.init.pid")" == "$recorded_unrelated" ]]

# Diagnostic observation does not consume stale metadata or remove socket files.
printf '2147483647\n' > "$prefix/.init.pid"
python3 - "$prefix" <<'PY'
import os
import socket
import sys
os.chdir(sys.argv[1])
for name in ('.darlingserver.sock', '.darlingserver.stat.sock'):
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(name)
PY
before="$(stat -c '%n:%d:%i:%f:%s' "$prefix/.init.pid" "$prefix/.darlingserver.sock" "$prefix/.darlingserver.stat.sock")"
"$harness" --prefix "$prefix" --list-owned > "$tmp/stale-observed" 2>&1
[[ "$(cat "$prefix/.init.pid")" == 2147483647 ]]
[[ "$(stat -c '%n:%d:%i:%f:%s' "$prefix/.init.pid" "$prefix/.darlingserver.sock" "$prefix/.darlingserver.stat.sock")" == "$before" ]]
printf 'DARLING-BOOT-HARNESS-OWNERSHIP PASS\n'
