#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
export ROOTLESS_TIER_REPO="$root"
. "$root/ci/rootless-prefix.sh"
export TMPDIR="$DW_STATE_ROOT/tmp"
defer_global_cleanup="${DARLING_TIER_DEFER_GLOBAL_CLEANUP:-0}"
case "$defer_global_cleanup" in
	0|1) ;;
	*)
		echo "DARLING_TIER_DEFER_GLOBAL_CLEANUP must be 0 or 1" >&2
		exit 2
		;;
esac
mkdir -p -- "$TMPDIR"

native_tar() {
	local operation="$1"
	shift
	local flags=(--no-xattrs)
	if [[ "$(uname -s)" == Darwin ]]; then
		flags+=(--no-mac-metadata)
		if [[ "$operation" == extract ]]; then
			flags+=(--options '!mac-ext')
		fi
	fi
	exec env COPYFILE_DISABLE=1 tar "${flags[@]}" "$@"
}

run_tier_phase() {
	local name="$1"
	shift
	local started_ns finished_ns rc
	started_ns="$(date +%s%N)"
	echo "tier phase start: $name"
	set +e
	"$@"
	rc=$?
	set -e
	finished_ns="$(date +%s%N)"
	echo "tier phase complete: $name ($(((finished_ns - started_ns) / 1000000))ms)"
	return "$rc"
}


cleanup_rootless_tier() {
	local test_rc="$1"
	local cleanup_rc=0
	local gc_rc=0
	local jobs_rc=0
	local evidence_rc=0
	local diagnostics_rc=0
	set +e
	if [[ -d "$prefix" ]]; then
		case "$tier_kind" in
			smoke|regression|corpus)
				rootless_prefix_assert_no_guest_toolchain "$tier_kind" "$prefix" || cleanup_rc=1
				;;
			toolchain)
				rootless_prefix_assert_guest_toolchain "$tier_kind" "$prefix" || cleanup_rc=1
				;;
		esac
		if (( test_rc == 0 )) && [[ "$tier_kind" == toolchain ]]; then
			"$root/ci/collect-rootless-diagnostics.sh" \
				"$DW_STATE_ROOT/evidence/rootless-toolchain-diagnostics" "$prefix"
			evidence_rc=$?
			(( evidence_rc == 0 )) || cleanup_rc=1
		fi
		west test --runtime-root "$DARLING_RUNTIME_ROOT" --prefix "$prefix" --cleanup-prefix
		cleanup_command_rc=$?
		(( cleanup_command_rc == 0 )) || cleanup_rc=1
		if [[ "$tier_kind" == corpus || "$tier_kind" == regression ]] && \
			[[ -n "${validation_group:-}" && -n "${diagnostics_dir:-}" ]]; then
			"$root/ci/collect-rootless-diagnostics.sh" \
				"$diagnostics_dir" \
				"$prefix"
			diagnostics_rc=$?
			(( diagnostics_rc == 0 )) || cleanup_rc=1
			rootless_prefix_assert_no_guest_toolchain "$tier_kind" "$prefix" || cleanup_rc=1
		fi
	else
		cleanup_rc=1
		echo "rootless tier prefix disappeared before cleanup: $prefix" >&2
	fi
	if [[ "$defer_global_cleanup" == 0 ]]; then
		west test --gc --gc-runtime-evidence
		gc_rc=$?
		"$root/scripts/west-job.sh" assert-no-live-west-test --state-root "$TMPDIR"
		jobs_rc=$?
	fi
	if (( test_rc == 0 && cleanup_rc == 0 && gc_rc == 0 && jobs_rc == 0 )); then
		rootless_prefix_remove "$tier_kind" "$prefix"
		cleanup_rc=$?
	else
		echo "preserving rootless tier prefix for diagnostics: $prefix" >&2
	fi
	if (( test_rc != 0 )); then
		exit "$test_rc"
	fi
	if (( cleanup_rc != 0 || gc_rc != 0 || jobs_rc != 0 )); then
		exit 1
	fi
	exit 0
}

run_guest_macho_regression_tier() {
	local requested_tier_kind="$1"
	local prefix_variable="$2"
	local diagnostics_relative_path="$3"
	if [[ "$#" -ne 3 ]]; then
		echo "run_guest_macho_regression_tier expects tier kind, prefix variable, and diagnostics path" >&2
		exit 2
	fi
	tier_kind="$requested_tier_kind"
	rootless_runtime_select
	export DARLING_CI_TEST_DATA=1
	validation_group=homebrew
	diagnostics_dir="$DW_STATE_ROOT/evidence/$diagnostics_relative_path"
	prefix="$(rootless_prefix_create "$tier_kind" "$prefix_variable")"
	rootless_prefix_export_output prefix "$prefix"
	trap 'cleanup_rootless_tier "$?"' EXIT
	rootless_prefix_assert_no_guest_toolchain "$tier_kind" "$prefix"
	rm -rf -- "$diagnostics_dir"
	mkdir -p -- "$diagnostics_dir"
	export WEST_TEST_FORBID_GUEST_TOOLCHAIN=1
	rootless_runtime_bootstrap "$prefix"
	WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 west test \
		--env darling \
		--guest-macho-validation-group "$validation_group" \
		--guest-macho-evidence-dir "$diagnostics_dir/fixtures" \
		--runtime-root "$DARLING_RUNTIME_ROOT" \
		--prefix "$prefix"
}

case "${1:-}" in
	host)
		exec "$root/ci/run-host-tier.py" "${@:2}"
		;;
	guest-smoke)
		tier_kind=smoke
		rootless_runtime_select
		prefix="$(rootless_prefix_create "$tier_kind" DARLING_SMOKE_PREFIX)"
		rootless_prefix_export_output prefix "$prefix"
		trap 'cleanup_rootless_tier "$?"' EXIT
		export WEST_TEST_FORBID_GUEST_TOOLCHAIN=1
		run_tier_phase bootstrap-runtime rootless_runtime_bootstrap "$prefix"
		for workload in rootless_prefix_initialization_guest rootless_prebuilt_macho_regression; do
			run_tier_phase "$workload" west test \
				--workload "$workload" --env darling \
				--runtime-root "$DARLING_RUNTIME_ROOT" \
				--prefix "$prefix" "${@:2}"
		done
		;;
	guest-full)
		if [[ -n "${2:-}" ]]; then
			echo "guest-full does not accept additional test selectors" >&2
			exit 2
		fi
		run_guest_macho_regression_tier regression DARLING_REGRESSION_PREFIX \
			guest-full-diagnostics
		;;
	guest-macho-validation)
		if [[ -n "${2:-}" ]]; then
			echo "guest-macho-validation does not accept a validation group" >&2
			exit 2
		fi
		run_guest_macho_regression_tier corpus DARLING_CORPUS_PREFIX \
			guest-macho-validation-diagnostics/homebrew
		;;
	guest-toolchain)
		tier_kind=toolchain
		rootless_runtime_select
		prefix="$(rootless_prefix_create "$tier_kind" DARLING_TOOLCHAIN_PREFIX)"
		rootless_prefix_export_output prefix "$prefix"
		trap 'cleanup_rootless_tier "$?"' EXIT
		rootless_runtime_bootstrap "$prefix" darling-command-line-tools
		# Select the real compiler/SDK acceptance proof, not a broad smoke label.
		west test --workload rootless_guest_toolchain_compile_execute \
			--env darling --runtime-root "$DARLING_RUNTIME_ROOT" \
			--prefix "$prefix" "${@:2}"
		;;
	acceptance-cleanup)
		unset DARLING_TIER_DEFER_GLOBAL_CLEANUP
		west test --gc --gc-runtime-evidence
		exec "$root/scripts/west-job.sh" assert-no-live-west-test \
			--state-root "$TMPDIR"
		;;
	macos)
		build="${DARLING_TESTKIT_BUILD:-$root/.west-test/macos-build}"
		cmake -S testkit -B "$build" -DBUILD_TESTING=ON
		cmake --build "$build" --parallel
		exec ctest --test-dir "$build" --output-on-failure -L 'env:macos'
		;;
	macos-package)
		output="${2:?macos-package requires an output directory}"
		build="${DARLING_TESTKIT_BUILD:-$root/.west-test/macos-build}"
		cmake -S testkit -B "$build" -DBUILD_TESTING=ON \
			-DCMAKE_INSTALL_PREFIX="$output"
		cmake --build "$build" --parallel
		exec cmake --install "$build"
		;;
	macos-archive)
		bundle="${2:?macos-archive requires an installed bundle}"
		archive="${3:?macos-archive requires an output archive}"
		# Keep a literal portable file tree: no implicit AppleDouble sidecars.
		native_tar create -cf "$archive" -C "$bundle" .
		;;
	macos-extract)
		archive="${2:?macos-extract requires an archive}"
		destination="${3:?macos-extract requires a new destination directory}"
		mkdir -- "$destination"
		native_tar extract -xpf "$archive" --no-same-owner -C "$destination"
		;;
	macos-installed)
		exec "$root/ci/run-macos-installed-tests.sh" \
			"${2:?macos-installed requires an installed bundle}" "${@:3}"
		;;
	macos-ssh)
		exec python3 "$root/ci/native-transport.py" ssh \
			"${2:?macos-ssh requires an SSH host}" \
			"${3:?macos-ssh requires an installed bundle}" "${@:4}"
		;;
	*)
		echo "usage: $0 host|guest-smoke|guest-macho-validation|guest-full|guest-toolchain|macos|macos-package|macos-archive|macos-extract|macos-installed|macos-ssh [tier arguments]" >&2
		exit 2
		;;
esac
