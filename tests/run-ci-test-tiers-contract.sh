#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/bin"

for tool in west cmake ctest; do
	cat >"$tmp/bin/$tool" <<'TOOL'
#!/usr/bin/env bash
printf '%s %s\n' "$(basename "$0")" "$*" >>"$CI_CONTRACT_LOG"
if [ "$(basename "$0")" = west ] && [ "${1:-}" = topdir ]; then
	exit "${CI_WEST_TOPDIR_RC:-0}"
fi
if [ "$(basename "$0")" = west ] && {
	[[ "$*" == *"--bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal"* ]] ||
	[[ "$*" == *"name:rootless_prefix_initialization_guest"* ]] ||
	[[ "$*" == *"name:rootless_prebuilt_macho_regression"* ]]
}; then
	[ "${WEST_TEST_FORBID_GUEST_TOOLCHAIN:-}" = 1 ] || {
		echo 'no-CLT smoke command did not set WEST_TEST_FORBID_GUEST_TOOLCHAIN=1' >&2
		exit 1
	}
fi
if [ "$(basename "$0")" = west ] && [[ " $* " == *" --bootstrap-runtime-profile homebrew-guest-toolchain-provisioning "* ]]; then
	prefix=""
	while (($#)); do
		if [ "$1" = --prefix ] && [ $# -ge 2 ]; then
			prefix="$2"
			break
		fi
		shift
	done
	if [ -n "$prefix" ]; then
		mkdir -p "$prefix/Library/Developer/CommandLineTools/usr/bin" \
			"$prefix/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
		touch "$prefix/Library/Developer/CommandLineTools/usr/bin/clang"
		chmod +x "$prefix/Library/Developer/CommandLineTools/usr/bin/clang"
	fi
fi
if [ "$(basename "$0")" = west ] &&
	[[ "$*" == *"--env darling"* && "$*" == *"--prefix"* && "$*" == *"--list"* ]]; then
	cat <<'LIST'
  Test  #1: darling/abort_with_payload_no_group_broadcast
  Test  #2: darling/select_fdset_guest
  Test  #3: darling/getattrlist_name_objtype_guest
  Test  #4: darling/darwin_priority_guest
  Test  #5: darling/socket_siocgifconf_guest
  Test  #6: darling/bzero_return_register_guest
  Test  #7: darling/sigexc_sa_restart_guest
  Test  #8: darling/sigexc_default_resend_self_guest
  Test  #9: darling/ulock_eintr_retry_guest
  Test #10: darling/vchroot_pathnull_guard_guest
  Test #11: darling/chown_disabled_null_guard_guest
  Test #12: darling/fd_guard_ebadf_guest
  Test #13: darling/fork_checkin_signal_storm_guest
  Test #14: darling/rootless_no_mount_guest
LIST
fi
TOOL
	chmod +x "$tmp/bin/$tool"
done

export PATH="$tmp/bin:$PATH"
export CI_CONTRACT_LOG="$tmp/commands"
export RUNNER_TEMP="$tmp/runner"
export DARLING_SMOKE_PREFIX="$tmp/runner/darling-rootless-smoke"
export DARLING_REGRESSION_PREFIX="$tmp/runner/darling-rootless-regression"

"$repo/ci/run-test-tier.sh" host
"$repo/ci/run-test-tier.sh" guest-smoke
if "$repo/ci/run-test-tier.sh" guest-full; then
	echo 'guest-full unexpectedly passed without a prebuilt regression corpus' >&2
	exit 1
else
	full_rc=$?
	[ "$full_rc" -eq 78 ] || {
		echo "guest-full returned $full_rc instead of blocker rc 78" >&2
		exit 1
	}
fi
DARLING_TOOLCHAIN_PREFIX="$tmp/runner/darling-rootless-toolchain" \
	"$repo/ci/run-test-tier.sh" guest-toolchain
DARLING_TESTKIT_BUILD="$tmp/macos-build" "$repo/ci/run-test-tier.sh" macos
DARLING_TESTKIT_BUILD="$tmp/package-build" \
	"$repo/ci/run-test-tier.sh" macos-package "$tmp/oracle"

grep -F -x -q 'west test --profile homebrew --env host --materialize-profile' "$tmp/commands"
grep -F -x -q "west test --prefix $tmp/runner/darling-rootless-smoke --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal --runtime-build-timeout-seconds 600" "$tmp/commands"
grep -F -x -q "west test --profile homebrew --patch darling/rootless-prefix-initialization.patch --env darling --label name:rootless_prefix_initialization_guest --reuse-prefix-runtime --prefix $tmp/runner/darling-rootless-smoke" "$tmp/commands"
grep -F -x -q "west test --profile homebrew --patch darling/rootless-prefix-initialization.patch --env darling --label name:rootless_prebuilt_macho_regression --reuse-prefix-runtime --prefix $tmp/runner/darling-rootless-smoke" "$tmp/commands"
grep -F -x -q "west test --prefix $tmp/runner/darling-rootless-regression --bootstrap-runtime-profile homebrew-rootless-bootstrap-minimal --runtime-build-timeout-seconds 600" "$tmp/commands"
if grep -F -x -q "west test --env darling --prefix $tmp/runner/darling-rootless-regression" "$tmp/commands"; then
	echo 'blocked guest-full unexpectedly selected a regression command' >&2
	exit 1
fi
grep -F -x -q "west test --prefix $tmp/runner/darling-rootless-toolchain --bootstrap-runtime-profile homebrew-guest-toolchain-provisioning --runtime-build-timeout-seconds 600" "$tmp/commands"
grep -F -x -q "west test --profile homebrew --patch darling/rootless-prefix-initialization.patch --env darling --label name:rootless_guest_toolchain_compile_execute --reuse-prefix-runtime --prefix $tmp/runner/darling-rootless-toolchain" "$tmp/commands"
grep -F -x -q "cmake -S testkit -B $tmp/macos-build -DBUILD_TESTING=ON" "$tmp/commands"
grep -F -x -q "ctest --test-dir $tmp/macos-build --output-on-failure -L env:macos" "$tmp/commands"
grep -F -x -q "cmake --install $tmp/package-build" "$tmp/commands"

deps_script='darling-dev/darling-workspace/ci/install-darling-build-deps.sh'
[ "$(grep -F -c "run: $deps_script" "$repo/.github/workflows/test-infra.yml")" -ge 2 ]
[ "$(grep -F -c 'timeout-minutes: 30' "$repo/.github/workflows/test-infra.yml")" -ge 2 ]
[ "$(grep -F -c 'actions/checkout@v7' "$repo/.github/workflows/test-infra.yml")" -ge 5 ]
[ "$(grep -F -c 'actions/upload-artifact@v7' "$repo/.github/workflows/test-infra.yml")" -ge 2 ]
[ "$(grep -F -c 'actions/cache@v4' "$repo/.github/workflows/test-infra.yml")" -ge 1 ]
[ "$(grep -F -c 'darling-command-line-tools-reviewed-v1-${{ runner.os }}' "$repo/.github/workflows/test-infra.yml")" -ge 1 ]
[ "$(grep -F -c 'ci/collect-rootless-diagnostics.sh .west-test/rootless-diagnostics/tier' "$repo/.github/workflows/test-infra.yml")" -ge 2 ]
[ "$(grep -F -c 'ci/run-rootless-bootstrap-diagnostic.sh' "$repo/.github/workflows/test-infra.yml")" -ge 2 ]
[ "$(grep -F -c -- '--runtime-build-timeout-seconds 600' "$repo/ci/run-rootless-bootstrap-diagnostic.sh")" -ge 1 ]
[ "$(grep -F -c 'ci/cleanup-rootless-prefixes.sh' "$repo/.github/workflows/test-infra.yml")" -ge 3 ]
[ "$(grep -F -c 'cargo build --release --locked --manifest-path darling-dev/darling-debug-runner/Cargo.toml' "$repo/.github/workflows/test-infra.yml")" -eq 3 ]
[ "$(grep -F -c "github.event_name == 'pull_request'" "$repo/.github/workflows/test-infra.yml")" -ge 1 ]
[ "$(grep -F -c 'Run full rootless regression (blocked pending prebuilt corpus)' "$repo/.github/workflows/test-infra.yml")" -eq 1 ]
! grep -F -q 'actions/checkout@v4' "$repo/.github/workflows/test-infra.yml"
! grep -F -q 'actions/upload-artifact@v4' "$repo/.github/workflows/test-infra.yml"
! grep -F -q $'\t\texec west test --profile homebrew --patch' "$repo/ci/run-test-tier.sh"
for package in libfuse-dev libx11-dev libcairo2-dev libxrandr-dev libfreetype6-dev strace; do
	grep -F -q "$package" "$repo/ci/install-darling-build-deps.sh"
done

mkdir -p "$tmp/installed/testcase"
cat >"$tmp/installed/testcase/compat.sample" <<'SAMPLE'
#!/usr/bin/env bash
printf 'SAMPLE_OK\n'
SAMPLE
chmod +x "$tmp/installed/testcase/compat.sample"
printf 'sample\tcompat.sample\tSAMPLE_OK\n' >"$tmp/installed/compat-install-manifest.tsv"
"$repo/ci/run-test-tier.sh" macos-installed "$tmp/installed" |
	grep -F -x -q 'PASS macos/sample'

"$repo/tests/run-rootless-prefix-contract.sh"
"$repo/tests/run-rootless-cleanup-contract.sh"

: >"$tmp/commands"
CI_WEST_TOPDIR_RC=1 "$repo/ci/bootstrap-west.sh"
grep -F -x -q "west init -l $repo" "$tmp/commands"
grep -F -x -q 'west update' "$tmp/commands"

printf 'PASS ci-test-tiers-contract\n'
