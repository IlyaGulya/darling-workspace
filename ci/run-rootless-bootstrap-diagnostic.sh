#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
trace_dir="${1:?bootstrap trace directory is required}"
ROOTLESS_TIER_REPO="$root"
. "$root/ci/rootless-prefix.sh"

prefix="$(rootless_prefix_create diagnostic DARLING_DIAGNOSTIC_PREFIX)"
rootless_prefix_export_output prefix "$prefix"

cleanup_diagnostic_prefix() {
	local test_rc="$1"
	local cleanup_rc=0
	local gc_rc=0
	local jobs_rc=0
	set +e
	west test --prefix "$prefix" --cleanup-prefix
	cleanup_rc=$?
	west test --gc --gc-runtime-evidence
	gc_rc=$?
	"$root/scripts/west-job.sh" assert-no-live-west-test --state-root "${TMPDIR:-/tmp}"
	jobs_rc=$?
	if (( test_rc == 0 && cleanup_rc == 0 && gc_rc == 0 && jobs_rc == 0 )); then
		rootless_prefix_remove diagnostic "$prefix"
		cleanup_rc=$?
	else
		echo "preserving bootstrap diagnostic prefix: $prefix" >&2
	fi
	if (( test_rc != 0 )); then
		exit "$test_rc"
	fi
	if (( cleanup_rc != 0 || gc_rc != 0 || jobs_rc != 0 )); then
		exit 1
	fi
	exit 0
}

trap 'cleanup_diagnostic_prefix "$?"' EXIT
mkdir -p -- "$trace_dir"
WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 \
	timeout --foreground --kill-after=15s 720s \
	west test --prefix "$prefix" \
		--bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal \
		--bootstrap-timeout-seconds 75 \
		--runtime-build-timeout-seconds 600 \
		--bootstrap-syscall-trace "$trace_dir"
