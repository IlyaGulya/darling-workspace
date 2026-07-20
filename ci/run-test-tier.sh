#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
export ROOTLESS_TIER_REPO="$root"
. "$root/ci/rootless-prefix.sh"

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
				"$root/.west-test/rootless-toolchain-diagnostics" "$prefix"
			evidence_rc=$?
			(( evidence_rc == 0 )) || cleanup_rc=1
		fi
		west test --prefix "$prefix" --cleanup-prefix
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
	west test --gc --gc-runtime-evidence
	gc_rc=$?
	"$root/scripts/west-job.sh" assert-no-live-west-test --state-root "${TMPDIR:-/tmp}"
	jobs_rc=$?
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
	validation_group=homebrew
	diagnostics_dir="$root/$diagnostics_relative_path"
	prefix="$(rootless_prefix_create "$tier_kind" "$prefix_variable")"
	rootless_prefix_export_output prefix "$prefix"
	trap 'cleanup_rootless_tier "$?"' EXIT
	rootless_prefix_assert_no_guest_toolchain "$tier_kind" "$prefix"
	rm -rf -- "$diagnostics_dir"
	mkdir -p -- "$diagnostics_dir"
	export DARLING_ROOTLESS=1
	export DARLING_NOOVERLAYFS=1
	export DARLING_EUNION=1
	export WEST_TEST_FORBID_GUEST_TOOLCHAIN=1
	WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 west test --prefix "$prefix" \
		--bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal \
		--runtime-build-timeout-seconds 600
	WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 west test \
		--profile homebrew \
		--env darling \
		--guest-macho-validation-group "$validation_group" \
		--guest-macho-evidence-dir "$diagnostics_dir/fixtures" \
		--reuse-prefix-runtime \
		--prefix "$prefix"
}

case "${1:-}" in
	host)
		# Source-bound host cases must be selected through metadata so west can
		# materialize the patch profile before CMake compiles the real source.
		tests/run-west-patch-stack-materialize-contract.sh
		tests/run-west-patch-stack-shadow-contract.sh
		tests/run-patch-stack-shadow-hosted-workflow-contract.sh
		tests/run-patch-stack-migration-inventory-contract.sh
		exec west test --profile homebrew --env host --materialize-profile "${@:2}"
		;;
	guest-smoke)
		tier_kind=smoke
		prefix="$(rootless_prefix_create "$tier_kind" DARLING_SMOKE_PREFIX)"
		rootless_prefix_export_output prefix "$prefix"
		trap 'cleanup_rootless_tier "$?"' EXIT
		WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 west test --prefix "$prefix" \
			--bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal \
			--runtime-build-timeout-seconds 600
		WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 west test \
			--profile homebrew --patch darling/rootless-prefix-initialization.patch \
			--env darling --label 'name:rootless_prefix_initialization_guest' \
			--reuse-prefix-runtime \
			--prefix "$prefix" "${@:2}"
		WEST_TEST_FORBID_GUEST_TOOLCHAIN=1 west test \
			--profile homebrew --patch darling/rootless-prefix-initialization.patch \
			--env darling --label 'name:rootless_prebuilt_macho_regression' \
			--reuse-prefix-runtime \
			--prefix "$prefix" "${@:2}"
		;;
	guest-full)
		if [[ -n "${2:-}" ]]; then
			echo "guest-full does not accept additional test selectors" >&2
			exit 2
		fi
		run_guest_macho_regression_tier regression DARLING_REGRESSION_PREFIX \
			.west-test/guest-full-diagnostics
		;;
	guest-macho-validation)
		if [[ -n "${2:-}" ]]; then
			echo "guest-macho-validation does not accept a validation group" >&2
			exit 2
		fi
		run_guest_macho_regression_tier corpus DARLING_CORPUS_PREFIX \
			.west-test/guest-macho-validation-diagnostics/homebrew
		;;
	guest-toolchain)
		tier_kind=toolchain
		prefix="$(rootless_prefix_create "$tier_kind" DARLING_TOOLCHAIN_PREFIX)"
		rootless_prefix_export_output prefix "$prefix"
		trap 'cleanup_rootless_tier "$?"' EXIT
		west test --prefix "$prefix" \
			--bootstrap-runtime-profile homebrew-guest-toolchain-provisioning \
			--runtime-build-timeout-seconds 1800
		# Select the patch-owned script explicitly. A broad CTest smoke label can
		# select unrelated regressions and still omit this acceptance proof.
		west test --profile homebrew \
			--patch darling/rootless-prefix-initialization.patch \
			--env darling --label 'name:rootless_guest_toolchain_compile_execute' \
			--reuse-prefix-runtime \
			--prefix "$prefix" "${@:2}"
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
	macos-installed)
		exec "$root/ci/run-macos-installed-tests.sh" \
			"${2:?macos-installed requires an installed bundle}"
		;;
	*)
		echo "usage: $0 host|guest-smoke|guest-macho-validation|guest-full|guest-toolchain|macos|macos-package|macos-installed" >&2
		exit 2
		;;
esac
