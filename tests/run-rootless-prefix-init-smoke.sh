#!/usr/bin/env bash
set -euo pipefail

: "${DARLING_LAUNCHER:?west must provide the deployed launcher}"
: "${DPREFIX:?west must provide the test prefix}"

workspace_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
lock_reporter="$workspace_root/ci/capture-prefix-lock-owners.py"

capture_lock_owners() {
	local phase="$1"
	local require_empty="${2:-false}"
	local snapshot
	snapshot="$(python3 -B "$lock_reporter" --prefix "$DPREFIX" --phase "$phase")"
	printf '%s\n' "$snapshot"
	if [[ "$require_empty" == true ]] &&
		grep -q '^PREFIX_LOCK_OWNER ' <<<"$snapshot"
	then
		printf 'ROOTLESS_PREFIX_RESTART_LOCK_LEAK phase=%s\n' "$phase" >&2
		return 1
	fi
}

wait_for_verified_shutdown() {
	local phase="$1"
	local attempt
	for attempt in $(seq 1 100); do
		if [[ ! -S "$DPREFIX/.darlingserver.sock" ]]; then
			capture_lock_owners "$phase" true
			printf 'ROOTLESS_PREFIX_RESTART_SHUTDOWN_OK phase=%s\n' "$phase"
			return 0
		fi
		sleep 0.05
	done
	capture_lock_owners "${phase}-timeout" false || true
	printf 'ROOTLESS_PREFIX_RESTART_SHUTDOWN_TIMEOUT phase=%s\n' "$phase" >&2
	return 1
}

assert_guest_layout() {
	local cycle="$1"
	capture_lock_owners "before-${cycle}" false
	if ! timeout --foreground --kill-after=5s 20s \
		"$DARLING_LAUNCHER" shell /bin/bash -c '
set -eu
for path in \
  /private/var/db/launchd.db/com.apple.launchd \
  /private/tmp \
  /private/var/tmp \
  /var/run \
  /var/tmp \
  /tmp; do
  if ! test -d "$path"; then
    printf "ROOTLESS_PREFIX_INIT_MISSING %s\n" "$path"
    exit 1
  fi
done
'
	then
		capture_lock_owners "failed-${cycle}-boot" false || true
		printf 'ROOTLESS_PREFIX_RESTART_BOOT_FAILED cycle=%s\n' "$cycle" >&2
		return 1
	fi
	printf 'ROOTLESS_PREFIX_RESTART_BOOT_OK cycle=%s\n' "$cycle"
	capture_lock_owners "running-${cycle}" false
	if ! timeout --foreground --kill-after=5s 15s "$DARLING_LAUNCHER" shutdown; then
		capture_lock_owners "failed-${cycle}-shutdown" false || true
		printf 'ROOTLESS_PREFIX_RESTART_SHUTDOWN_FAILED cycle=%s\n' "$cycle" >&2
		return 1
	fi
	wait_for_verified_shutdown "after-${cycle}-shutdown"
}

# The launcher must create these directories as part of the product bootstrap.
# This deliberately runs after a clean guest start, so a West-side directory
# preflight cannot make the test pass accidentally.
assert_guest_layout first
assert_guest_layout second

for path in private/tmp private/var/tmp var/tmp tmp; do
	test "$(stat -c %a "$DPREFIX/$path")" = 1777
done
test -f "$DPREFIX/.darling-prefix-state-v3"
printf '%s\n' ROOTLESS_PREFIX_INIT_GUEST_OK
