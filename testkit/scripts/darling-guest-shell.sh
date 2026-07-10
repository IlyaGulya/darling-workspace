#!/usr/bin/env bash
# Shared low-level Darling guest command transport.

darling_guest_shell() {
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	shift 3

	timeout --kill-after=5 "$timeout_seconds" \
		env "DPREFIX=$prefix" "DARLING_PREFIX=$prefix" \
		"$launcher" shell /bin/bash --login -c "$@"
}

darling_guest_state_root() {
	# This path is guest-owned. Do not use a host-visible prefix path: overlay or
	# copy-mode may intentionally make guest /tmp invisible to the host.
	printf '%s\n' "${DARLING_GUEST_STATE_DIR:-/tmp/.west-test-state}"
}

darling_guest_read_file() {
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	local path="$4"

	darling_guest_shell "$launcher" "$prefix" "$timeout_seconds" \
		"cat '$path' 2>/dev/null || true"
}

darling_guest_wait_for_line() {
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	local path="$4"
	local expected="$5"
	local deadline=$((SECONDS + timeout_seconds))
	local output

	while ((SECONDS < deadline)); do
		set +e
		output="$(darling_guest_read_file "$launcher" "$prefix" 10 "$path")"
		set -e
		if printf '%s\n' "$output" | grep -F -x -q -- "$expected"; then
			return 0
		fi
		sleep 0.1
	done
	printf 'WEST_GUEST_COMPLETION_TIMEOUT path=%s expected=%s\n' "$path" "$expected" >&2
	return 124
}

darling_guest_upload_file() {
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	local state_dir="$4"
	local source="$5"
	local guest_path="$6"
	local digest_path="$state_dir/upload.sha256"
	local source_digest

	source_digest="$(sha256sum "$source" | awk '{print $1}')"
	set +e
	darling_guest_shell "$launcher" "$prefix" "$timeout_seconds" \
		"mkdir -p '$state_dir'; cat > '$guest_path'; sha256sum '$guest_path' > '$digest_path'" \
		<"$source"
	set -e
	darling_guest_wait_for_line "$launcher" "$prefix" "$timeout_seconds" \
		"$digest_path" "$source_digest  $guest_path"
}

darling_guest_execute_with_verdict() {
	local launcher="$1"
	local prefix="$2"
	local timeout_seconds="$3"
	local state_dir="$4"
	local script="$5"
	local result_path="$state_dir/result"
	local state_path="$state_dir/state"
	local worker
	local quoted_worker
	local completion
	local rc

	worker="$(printf '%s\n' \
		'set +e' \
		'(' \
		"$script" \
		") > '$result_path' 2>&1" \
		'rc=$?' \
		"printf 'WEST_GUEST_COMPLETION_RC=%s\\n' \"\$rc\" > '$state_path.tmp'" \
		"mv '$state_path.tmp' '$state_path'" \
		'exit 0')"
	printf -v quoted_worker '%q' "$worker"

	# The launcher can detach before this shell exits. The worker is the sole
	# success authority and publishes its final rc atomically.
	set +e
	darling_guest_shell "$launcher" "$prefix" 10 \
		"mkdir -p '$state_dir'; rm -f '$state_path' '$state_path.tmp' '$result_path'; nohup /bin/bash -c $quoted_worker >/dev/null 2>&1 &" \
		>/dev/null 2>&1
	set -e

	local deadline=$((SECONDS + timeout_seconds))
	while ((SECONDS < deadline)); do
		set +e
		completion="$(darling_guest_read_file "$launcher" "$prefix" 10 "$state_path")"
		set -e
		if printf '%s\n' "$completion" | grep -Eq '^WEST_GUEST_COMPLETION_RC=[0-9]+$'; then
			set +e
			darling_guest_read_file "$launcher" "$prefix" 10 "$result_path"
			set -e
			rc="$(printf '%s\n' "$completion" | sed -n 's/^WEST_GUEST_COMPLETION_RC=//p' | tail -1)"
			return "$rc"
		fi
		sleep 0.1
	done
	printf 'WEST_GUEST_COMPLETION_TIMEOUT path=%s\n' "$state_path" >&2
	return 124
}
