#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
tmp_profile="patches/__metadata_contract"
tmp_invalid_profile="patches/__metadata_invalid_contract"
tmp_runtime_red_profile="patches/__metadata_runtime_red_contract"
guest_prefix=/tmp/west-test-guest-c-fixture-prefix
source_script_marker=/tmp/west-source-script-fixture-second-case
trap 'rm -rf "$tmp_profile" "$tmp_invalid_profile" "$tmp_runtime_red_profile" "$guest_prefix" "$source_script_marker"' EXIT
mkdir -p "$tmp_profile" "$tmp_invalid_profile" "$tmp_runtime_red_profile"
cat >"$tmp_profile/patches.yml" <<'YAML'
patches:
- path: test/ctest-label.patch
  module: darling
  tests:
  - name: ctest_label_contract
    kind: contract
    env: host
    diag: bare
    red: true
    red-proof:
      mode: self
      why-self: Metadata-only fixture; this script validates selection/listing, not RED execution.
    ctest-label: bead:dar-gwn.5
- path: test/source-only.patch
  module: darling
  tests:
  - name: source_only_contract
    kind: source-contract
    coverage-tier: source
    env: host
    diag: bare
    red: true
    red-proof:
      mode: source-base
      source-env: DARLING_SRC_ROOT
    runner: python
    script: tests/source_only_contract.py
- path: test/source-only-exception.patch
  module: darling
  tests:
  - name: source_only_exception_contract
    kind: source-contract
    coverage-tier: source
    env: host
    diag: bare
    runner: python
    script: tests/source_only_contract.py
  test-exception:
    reason: blocked-on-runtime-hook
    note: Metadata-only fixture proving source-contract plus explicit exception is not reported as an unexplained missing behavioral test.
- path: test/model.patch
  module: darling
  tests:
  - name: model_contract
    kind: contract
    coverage-tier: model
    env: host
    diag: bare
    red: true
    red-proof:
      mode: self
      why-self: Metadata-only fixture that verifies model-tier reporting.
    runner: python
    script: tests/model_contract.py
- path: test/c-fixture.patch
  module: darling
  tests:
  - name: c_fixture_contract
    kind: unit
    env: host
    diag: bare
    red: true
    red-proof:
      mode: source-base
      source-env: DARLING_SRC_ROOT
    runner: c-fixture
    script: tests/c_fixture_contract.c
    source-files: [tests/c_fixture_helper.c]
    fixture-include-dirs: [tests/fixtures/c-fixture/include]
    include-dirs: [src]
    stub-headers: [darling/example.h]
    generated-headers:
      darling/generated.h: |
        #pragma once
        #define WEST_GENERATED_HEADER 1
    compile-flags: [-std=gnu11, -Wall, -Wextra, -Werror]
- path: test/guest-c-fixture.patch
  module: darling-workspace
  tests:
  - name: west_guest_c_fixture_contract
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    timeout-seconds: 20
    compile-flags: [-std=gnu11, -Wall, -Wextra, -Werror]
    host-trace-files:
    - env: WEST_GUEST_TRACE_FILE
      prefix-relative-path: private/var/tmp/west-guest-trace.log
      contains:
      - WEST_GUEST_TRACE_OK
    host-temp-files:
    - env: WEST_GUEST_TEMP_FILE
      prefix-relative-path: private/var/tmp/west-guest-temp.flag
      contents: |
        WEST_GUEST_TEMP_OK
    guest-env-vars:
      WEST_GUEST_TEMP_FILE: /private/var/tmp/west-guest-temp.flag
    host-trace-oracle: true
- path: test/guest-command-fixture.patch
  module: darling-workspace
  tests:
  - name: west_guest_command_fixture_contract
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: guest-command-fixture
    guest-command: /usr/bin/true
    timeout-seconds: 20
    guest-env-vars:
      WEST_GUEST_COMMAND_CONTRACT: '1'
    dcc-cache:
      source-module: darling/src/external/darlingserver
      source-ref: HEAD
      tools-dir: tools/closure-cache
      builder: dcc5-builder.c
      closure-list: smoke2-list.txt
      install-root: guest-visible
      env: DARLING_DYLD_DCC2_PATH
      enable-env: DARLING_DYLD_DCC2
      stale: true
      soft: true
    expect:
      returncode: timeout
      output-contains:
      - 'DCC2: cache invalid/stale'
- path: test/guest-command-any-returncode.patch
  module: darling-workspace
  tests:
  - name: guest_command_any_returncode_contract
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: guest-command-fixture
    guest-command: /usr/bin/true
    expect:
      returncode: any
      output-contains:
      - 'contract marker'
- path: test/blocked-guest-command.patch
  module: darling-workspace
  tests:
  - name: blocked_guest_command_contract
    blocked: true
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: guest-command-fixture
    guest-command: /usr/bin/true
  test-exception:
    reason: blocked-contract
    note: Metadata-only fixture proving blocked tests do not count as coverage.
- path: test/eunion-prefix-resource.patch
  module: darling-workspace
  tests:
  - name: west_eunion_prefix_resource_contract
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    timeout-seconds: 20
    requires:
    - darling-eunion-prefix
    eunion-upper-files:
    - guest-path: /private/var/tmp/west-eunion-upper-contract.txt
      contents: |
        WEST_EUNION_UPPER_OK
- path: test/mixed-red-nonred.patch
  module: darling-workspace
  tests:
  - name: mixed_red_self_contract
    kind: contract
    env: host
    diag: bare
    red: true
    red-proof:
      mode: self
      why-self: Metadata fixture proving prove-red ignores adjacent non-red tests before requirement checks.
    command: ":"
  - name: mixed_nonred_guest_contract
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
- path: test/script-host-trace.patch
  module: darling-workspace
  tests:
  - name: west_script_host_trace_contract
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: script
    script: tests/run-west-test-metadata-contract.sh
    host-trace-files:
    - env: WEST_SCRIPT_TRACE_FILE
      prefix-relative-path: private/var/tmp/west-script-trace.log
      contains:
      - WEST_SCRIPT_TRACE_OK
    host-trace-oracle: true
    requires:
    - darling-prefix
- path: test/source-build-fixture.patch
  module: darling-workspace
  tests:
  - name: west_source_build_fixture_contract
    kind: contract
    env: host
    diag: bare
    runner: source-build-fixture
    script: tests/guest_c_fixture_contract.c
    build-commands: [":"]
    run-commands: [":"]
- path: test/source-script-fixture.patch
  module: darling
  tests:
  - name: west_source_script_fixture_contract
    kind: contract
    env: host
    diag: bare
    runner: source-script-fixture
    source-script: src/sandbox/sandbox-exec.sh
    cases:
    - name: passthrough
      args: [-q, /bin/sh, -c, "printf ok"]
      stdout: ok
    - name: second-case
      args: [-q, /bin/sh, -c, "printf second >/tmp/west-source-script-fixture-second-case"]
- path: test/cmake-configure-fixture.patch
  module: darling
  tests:
  - name: west_cmake_configure_fixture_contract
    kind: build
    env: host
    diag: bare
    runner: cmake-configure-fixture
    configure-args: [-DDARLING_SKIP_DRIFT_GATE=ON]
    expect:
      returncode: nonzero
- path: test/darling-cmake-target-fixture.patch
  module: darling
  tests:
  - name: west_darling_cmake_target_fixture_contract
    kind: contract
    coverage-tier: host
    env: host
    diag: bare
    runner: darling-cmake-target-fixture
    target: west_fixture_target
    source-dir: source
    fixture-files: [tests/west_fixture_target.c]
    fallback-executable-sources: [source/tests/west_fixture_target.c]
    fallback-include-dirs: [source/src]
    fallback-link-libraries: []
- path: test/object-symbol-fixture.patch
  module: darling
  tests:
  - name: object_symbol_fixture_contract
    kind: contract
    env: host
    diag: bare
    runner: object-symbol-fixture
    source-file: tests/c_fixture_helper.c
    fixture-include-dirs: [tests/fixtures/c-fixture/include]
    include-dirs: [src]
    compile-flags: [-std=gnu11, -Wall, -Wextra, -Werror]
    symbol-checks:
    - name: default
      absent-undefined-symbols: [definitely_not_a_real_symbol]
      absent-defined-symbols: [definitely_not_a_real_symbol]
YAML

cat >"$tmp_invalid_profile/patches.yml" <<'YAML'
patches:
- path: test/invalid-guest-red-proof.patch
  module: darling-workspace
  tests:
  - name: invalid_guest_source_base_red
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: source-base
      source-env: DARLING_SRC_ROOT
- path: test/invalid-guest-runtime-red-proof.patch
  module: darling-workspace
  tests:
  - name: invalid_guest_runtime_red
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: guest-runtime-deploy
- path: test/incomplete-guest-runtime-artifact.patch
  module: darling-workspace
  tests:
  - name: incomplete_guest_runtime_artifact
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: guest-runtime-deploy
      runtime-artifacts:
      - module: darling/src/external/xnu
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
- path: test/invalid-current-minus-skip-mode.patch
  module: darling-workspace
  tests:
  - name: invalid_current_minus_skip_mode
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: guest-runtime-deploy
      current-minus-skip-patches:
      - x/dependent.patch
      runtime-artifacts:
      - module: darling/src/external/xnu
        build-targets: [libsystem_kernel]
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
- path: test/invalid-current-minus-skip-list.patch
  module: darling-workspace
  tests:
  - name: invalid_current_minus_skip_list
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: guest-runtime-deploy
      bad-profile: current-minus-patch
      current-minus-skip-patches: x/dependent.patch
      runtime-artifacts:
      - module: darling/src/external/xnu
        build-targets: [libsystem_kernel]
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
- path: test/invalid-source-patches.patch
  module: darling-workspace
  tests:
  - name: invalid_source_patches
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: guest-runtime-deploy
      bad-profile: current-minus-patch
      source-patches:
      - /tmp/bad.patch
      runtime-artifacts:
      - module: darling/src/external/xnu
        build-targets: [libsystem_kernel]
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
- path: test/invalid-script-runtime-red-proof.patch
  module: darling-workspace
  tests:
  - name: invalid_script_runtime_red
    kind: guest
    env: darling
    diag: bare
    runner: script
    script: tests/run-west-test-metadata-contract.sh
    red: true
    red-proof:
      mode: guest-runtime-deploy
      runtime-artifacts:
      - module: darling/src/external/xnu
        build-targets: [libsystem_kernel]
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
- path: test/invalid-host-trace.patch
  module: darling-workspace
  tests:
  - name: invalid_host_trace
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    host-trace-files:
    - env: 1BAD
      prefix-relative-path: /private/var/tmp/trace.log
      contains: BAD_TRACE
    host-temp-files:
    - env: 1BAD_TEMP
      prefix-relative-path: ../bad
      contents:
      - not-a-string
    host-stat-deltas:
    - path: per-call.bad
      min-delta: 0
    guest-env-vars:
      1BAD_GUEST: x
- path: test/invalid-host-trace-oracle.patch
  module: darling-workspace
  tests:
  - name: invalid_host_trace_oracle
    kind: guest
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    host-trace-oracle: true
YAML

cat >"$tmp_runtime_red_profile/patches.yml" <<'YAML'
patches:
- path: test/guest-runtime-red-proof.patch
  module: darling-workspace
  tests:
  - name: guest_runtime_red
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: guest-c-fixture
    script: tests/guest_c_fixture_contract.c
    ok-marker: WEST_GUEST_C_FIXTURE_OK
    red: true
    red-proof:
      mode: guest-runtime-deploy
      runtime-artifacts:
      - module: darling/src/external/xnu
        build-targets: [libsystem_kernel]
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
- path: test/script-runtime-red-proof.patch
  module: darling-workspace
  tests:
  - name: script_runtime_red
    kind: guest
    coverage-tier: runtime
    env: darling
    diag: bare
    runner: script
    script: tests/run-west-test-metadata-contract.sh
    requires:
    - darling-prefix
    red: true
    red-proof:
      mode: guest-runtime-deploy
      runtime-artifacts:
      - module: darling/src/external/xnu
        build-targets: [libsystem_kernel]
        deploy:
        - usr/lib/system/libsystem_kernel.dylib
YAML

fail() {
	printf 'FAIL: %s\n' "$*" >&2
	exit 1
}

branch_of() {
	git -C "$1" branch --show-current
}

assert_no_temp_worktrees() {
	local repo_path
	for repo_path in \
		../darling \
		../darling/src/external/darlingserver \
		../darling/src/external/libpthread \
		../darling/src/external/xnu
	do
		if git -C "$repo_path" worktree list --porcelain |
			grep -Eq 'west-profile-|west-red-proof-'; then
			fail "temporary worktree leaked for $repo_path"
		fi
	done
}

guarded="$(
	west test --profile homebrew \
		--patch xnu/getattrlist-shared-packer.patch \
		--list
)"

printf '%s\n' "$guarded" | grep -q 'diag:guarded' ||
	fail 'guarded metadata did not resolve to diag:guarded'
printf '%s\n' "$guarded" | grep -q 'darling-debug-runner run ' ||
	fail 'guarded metadata was not wrapped in darling-debug-runner'

bare="$(
	west test --profile homebrew \
		--patch xnu/psynch-negative-errno.patch \
		--list
)"

printf '%s\n' "$bare" | grep -q 'diag:bare' ||
	fail 'host metadata did not resolve to diag:bare'
bare_host_display="$(
	printf '%s\n' "$bare" |
		awk '
			/psynch_return_contract \[red, env:host, diag:bare, kind:contract\]/ { getline; print; found=1 }
			END { if (!found) exit 1 }
		'
)" || fail 'bare host metadata line was not found'
if printf '%s\n' "$bare_host_display" | grep -q 'darling-debug-runner run '; then
	fail 'bare host metadata was unexpectedly wrapped'
fi

profile_bound="$(
	west test --profile arch \
		--patch libunwind/static-no-jump-tables-for-dyld.patch \
		--materialize-profile \
		--list
)"

printf '%s\n' "$profile_bound" | grep -q 'dyld_static_libunwind_link' ||
	fail 'profile-bound metadata was not listed'
if printf '%s\n' "$profile_bound" | grep -q 'materialize '; then
	fail 'list mode unexpectedly materialized a profile'
fi

assert_no_temp_worktrees
darling_branch="$(branch_of ../darling)"
dserver_branch="$(branch_of ../darling/src/external/darlingserver)"
libpthread_branch="$(branch_of ../darling/src/external/libpthread)"
xnu_branch="$(branch_of ../darling/src/external/xnu)"

west test --profile homebrew \
	--patch darlingserver/progress-metrics.patch \
	--materialize-profile \
	--prove-red >/dev/null

[ "$(branch_of ../darling)" = "$darling_branch" ] ||
	fail 'materialize-profile changed the live darling checkout'
[ "$(branch_of ../darling/src/external/darlingserver)" = "$dserver_branch" ] ||
	fail 'materialize-profile changed the live darlingserver checkout'
[ "$(branch_of ../darling/src/external/libpthread)" = "$libpthread_branch" ] ||
	fail 'materialize-profile changed the live libpthread checkout'
[ "$(branch_of ../darling/src/external/xnu)" = "$xnu_branch" ] ||
	fail 'materialize-profile changed the live xnu checkout'
assert_no_temp_worktrees

ctest_label="$(
	west test --profile __metadata_contract \
		--patch test/ctest-label.patch \
		--list
)"

printf '%s\n' "$ctest_label" | grep -q 'ctest .* -L bead:dar-gwn.5' ||
	fail 'ctest-label metadata did not resolve to a runnable ctest command'
if printf '%s\n' "$ctest_label" | grep -q 'list-only'; then
	fail 'ctest-label metadata is still reported as list-only'
fi

source_only_check="$(west patch check --profile __metadata_contract)"
printf '%s\n' "$source_only_check" | grep -q 'SOURCE    test/source-only.patch' ||
	fail 'source-contract-only patch was not reported as SOURCE'
printf '%s\n' "$source_only_check" | grep -q 'missing behavioral test' ||
	fail 'source-contract-only patch did not require behavioral coverage'
if printf '%s\n' "$source_only_check" | grep -q 'TESTED    test/source-only.patch'; then
	fail 'source-contract-only patch was incorrectly counted as TESTED'
fi
printf '%s\n' "$source_only_check" | grep -q 'EXCEPTION test/source-only-exception.patch (blocked-on-runtime-hook; 1 source-contract(s))' ||
	fail 'source-contract exception patch was not reported as an explicit exception'
printf '%s\n' "$source_only_check" | grep -q 'MODEL     test/model.patch' ||
	fail 'model-tier patch was not reported as MODEL'
printf '%s\n' "$source_only_check" | grep -q 'COMPILE   test/c-fixture.patch' ||
	fail 'c-fixture patch was not reported as COMPILE'
printf '%s\n' "$source_only_check" | grep -q 'COMPILE   test/object-symbol-fixture.patch' ||
	fail 'object-symbol-fixture patch was not reported as COMPILE'
printf '%s\n' "$source_only_check" | grep -q 'HOST      test/darling-cmake-target-fixture.patch' ||
	fail 'darling-cmake-target-fixture patch was not reported as HOST'
printf '%s\n' "$source_only_check" | grep -q 'RUNTIME   test/eunion-prefix-resource.patch' ||
	fail 'darling-eunion-prefix patch was not reported as RUNTIME'

c_fixture="$(
	west test --profile __metadata_contract \
		--patch test/c-fixture.patch \
		--list
)"

printf '%s\n' "$c_fixture" | grep -q \
	'cc -std=gnu11 -Wall -Wextra -Werror -I tests/fixtures/c-fixture/include -I src -I <generated-stubs> tests/c_fixture_helper.c tests/c_fixture_contract.c -o' ||
	fail 'c-fixture metadata did not resolve to a compile-and-run command'
object_symbol_fixture="$(
	west test --profile __metadata_contract \
		--patch test/object-symbol-fixture.patch \
		--list
)"
printf '%s\n' "$object_symbol_fixture" | grep -q \
	'cc -c -std=gnu11 -Wall -Wextra -Werror -I tests/fixtures/c-fixture/include -I src tests/c_fixture_helper.c -o <temp>/<variant>.o && nm -u <temp>/<variant>.o && nm -g <temp>/<variant>.o' ||
	fail 'object-symbol-fixture metadata did not resolve to a compile-and-nm command'
guest_command_fixture="$(
	west test --profile __metadata_contract \
		--patch test/guest-command-fixture.patch \
		--list
)"
printf '%s\n' "$guest_command_fixture" | grep -q \
	'darling shell /bin/bash --login -c /usr/bin/true' ||
	fail 'guest-command-fixture metadata did not resolve to a guest shell command'
printf '%s\n' "$source_only_check" | grep -q 'test metadata: 14 covered (runtime 6, compile 3, host 4, model 1), 2 exceptions, 1 missing' ||
	fail 'coverage-tier summary did not classify runtime/host/compile/model coverage'

invalid_guest_red_check="$(west patch check --profile __metadata_invalid_contract 2>&1)"
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-guest-red-proof.patch: tests\[1\] guest-c-fixture cannot use source-base red-proof' ||
	fail 'guest-c-fixture source-base red-proof metadata was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-guest-runtime-red-proof.patch: tests\[1\] red-proof guest-runtime-deploy needs runtime-artifacts' ||
	fail 'guest-runtime-deploy metadata without runtime-artifacts was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/incomplete-guest-runtime-artifact.patch: tests\[1\].red-proof.runtime-artifacts\[0\] needs build-targets' ||
	fail 'guest-runtime-deploy artifact without build-targets was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-current-minus-skip-mode.patch: tests\[1\] current-minus-skip-patches requires bad-profile: current-minus-patch' ||
	fail 'current-minus-skip-patches without current-minus bad-profile was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-current-minus-skip-list.patch: tests\[1\] current-minus-skip-patches must be a list of patch paths' ||
	fail 'invalid current-minus-skip-patches list was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-source-patches.patch: tests\[1\] source-patches must be a list of workspace-relative patch paths' ||
	fail 'invalid source-patches list was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-script-runtime-red-proof.patch: tests\[1\] red-proof guest-runtime-deploy script runner requires darling-prefix' ||
	fail 'script guest-runtime-deploy without darling-prefix was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-trace-files\[0\] env must be a shell variable name' ||
	fail 'invalid host-trace-files env was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-trace-files\[0\] path must be prefix-relative' ||
	fail 'invalid host-trace-files path was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-trace-files\[0\] contains must be a list of strings' ||
	fail 'invalid host-trace-files contains was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-temp-files\[0\] env must be a shell variable name' ||
	fail 'invalid host-temp-files env was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-temp-files\[0\] path must be prefix-relative' ||
	fail 'invalid host-temp-files path was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-temp-files\[0\] contents must be a string' ||
	fail 'invalid host-temp-files contents was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-stat-deltas\[0\] path must be a dotted JSON field path' ||
	fail 'invalid host-stat-deltas path was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\].host-stat-deltas\[0\] min-delta must be a positive integer' ||
	fail 'invalid host-stat-deltas min-delta was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace.patch: tests\[1\] guest-env-vars must be a mapping of shell variable names to scalar values' ||
	fail 'invalid guest-env-vars was not rejected'
printf '%s\n' "$invalid_guest_red_check" | grep -q \
	'INVALID   test/invalid-host-trace-oracle.patch: tests\[1\] host-trace-oracle requires host-trace-files' ||
	fail 'host-trace-oracle without host-trace-files was not rejected'

runtime_red_check="$(west patch check --profile __metadata_runtime_red_contract)"
printf '%s\n' "$runtime_red_check" | grep -q 'RUNTIME   test/guest-runtime-red-proof.patch' ||
	fail 'guest-runtime-deploy metadata was not accepted as runtime coverage'
printf '%s\n' "$runtime_red_check" | grep -q 'RUNTIME   test/script-runtime-red-proof.patch' ||
	fail 'script guest-runtime-deploy metadata was not accepted as runtime coverage'

runtime_red_list="$(west test --profile __metadata_runtime_red_contract \
	--patch test/guest-runtime-red-proof.patch \
	--prove-red --list)"
printf '%s\n' "$runtime_red_list" | grep -q \
	'guest-runtime-deploy: darling/src/external/xnu\[build:libsystem_kernel; deploy:usr/lib/system/libsystem_kernel.dylib\]' ||
	fail 'guest-runtime-deploy list mode did not show deploy plan'
script_runtime_red_list="$(west test --profile __metadata_runtime_red_contract \
	--patch test/script-runtime-red-proof.patch \
	--prove-red --list)"
printf '%s\n' "$script_runtime_red_list" | grep -q \
	'guest-runtime-deploy: darling/src/external/xnu\[build:libsystem_kernel; deploy:usr/lib/system/libsystem_kernel.dylib\]' ||
	fail 'script guest-runtime-deploy list mode did not show deploy plan'

west test --profile __metadata_contract \
	--patch test/mixed-red-nonred.patch \
	--prove-red >/dev/null

if west test --profile __metadata_invalid_contract \
	--patch test/invalid-guest-red-proof.patch \
	--prove-red >/tmp/west-test-invalid-guest-red-proof.out 2>&1
then
	fail 'guest-c-fixture source-base red-proof execution unexpectedly passed'
fi
grep -q 'guest-c-fixture cannot use source-base RED proof' \
	/tmp/west-test-invalid-guest-red-proof.out ||
	fail 'guest-c-fixture source-base red-proof execution did not report the proof model error'

if west test --profile __metadata_runtime_red_contract \
	--patch test/guest-runtime-red-proof.patch \
	--prove-red >/tmp/west-test-guest-runtime-red-proof.out 2>&1
then
	fail 'guest-runtime-deploy RED proof unexpectedly passed without a prefix'
fi
grep -q 'missing required environment .*darling-prefix' \
	/tmp/west-test-guest-runtime-red-proof.out ||
	fail 'guest-runtime-deploy RED proof did not report missing prefix clearly'

rm -rf "$guest_prefix"
mkdir -p \
	"$guest_prefix/private/var/tmp" \
	"$guest_prefix/libexec/darling/private/var/tmp" \
	"$guest_prefix/Library/Developer/CommandLineTools/usr/bin" \
	"$guest_prefix/libexec/darling/Library/Developer/CommandLineTools/usr/bin" \
	"$guest_prefix/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk" \
	"$guest_prefix/libexec/darling/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
chmod 1777 \
	"$guest_prefix/private/var/tmp" \
	"$guest_prefix/libexec/darling/private/var/tmp"
: >"$guest_prefix/Library/Developer/CommandLineTools/usr/bin/clang"
: >"$guest_prefix/libexec/darling/Library/Developer/CommandLineTools/usr/bin/clang"

guest_c_fixture="$(
	west test --profile __metadata_contract \
		--patch test/guest-c-fixture.patch \
		--prefix "$guest_prefix" \
		--list
)"

printf '%s\n' "$guest_c_fixture" | grep -q \
	'<upload> tests/guest_c_fixture_contract.c && darling shell' ||
	fail 'guest-c-fixture metadata did not resolve to a guest compile-and-run command'
eunion_prefix_resource="$(
	west test --profile __metadata_contract \
		--patch test/eunion-prefix-resource.patch \
		--prefix "$guest_prefix" \
		--list
)"
printf '%s\n' "$eunion_prefix_resource" | grep -q \
	'<upload> tests/guest_c_fixture_contract.c && darling shell' ||
	fail 'darling-eunion-prefix metadata did not resolve to a guest compile-and-run command'
printf '%s\n' "$source_only_check" | grep -q 'RUNTIME   test/guest-c-fixture.patch' ||
	fail 'guest-c-fixture patch was not reported as RUNTIME'
printf '%s\n' "$source_only_check" | grep -q 'HOST      test/source-build-fixture.patch' ||
	fail 'source-build-fixture patch was not reported as HOST'
printf '%s\n' "$source_only_check" | grep -q 'HOST      test/source-script-fixture.patch' ||
	fail 'source-script-fixture patch was not reported as HOST'
printf '%s\n' "$source_only_check" | grep -q 'COMPILE   test/cmake-configure-fixture.patch' ||
	fail 'cmake-configure-fixture patch was not reported as COMPILE'
printf '%s\n' "$source_only_check" | grep -q 'HOST      test/darling-cmake-target-fixture.patch' ||
	fail 'darling-cmake-target-fixture patch was not reported as HOST'

source_build_fixture="$(
	west test --profile __metadata_contract \
		--patch test/source-build-fixture.patch \
		--list
)"

printf '%s\n' "$source_build_fixture" | grep -q \
	'<archive-source> && : && :' ||
	fail 'source-build-fixture metadata did not resolve to an archive/build/run command'

source_script_fixture="$(
	west test --profile __metadata_contract \
		--patch test/source-script-fixture.patch \
		--list
)"

printf '%s\n' "$source_script_fixture" | grep -q \
	'<source-script-fixture> src/sandbox/sandbox-exec.sh (2 case(s))' ||
	fail 'source-script-fixture metadata did not resolve to a source script command'
rm -f "$source_script_marker"
west test --profile __metadata_contract \
	--patch test/source-script-fixture.patch >/dev/null
[ "$(cat "$source_script_marker" 2>/dev/null)" = second ] ||
	fail 'source-script-fixture did not execute every declared case'

cmake_configure_fixture="$(
	west test --profile __metadata_contract \
		--patch test/cmake-configure-fixture.patch \
		--list
)"

printf '%s\n' "$cmake_configure_fixture" | grep -q \
	'<cmake-configure-fixture> cmake -S <source> -B <temp>/build -DDARLING_SKIP_DRIFT_GATE=ON' ||
	fail 'cmake-configure-fixture metadata did not resolve to a cmake configure command'

darling_cmake_target_fixture="$(
	west test --profile __metadata_contract \
		--patch test/darling-cmake-target-fixture.patch \
		--list
)"

printf '%s\n' "$darling_cmake_target_fixture" | grep -q \
	'<darling-cmake-target-fixture> cmake -S <superproject> -B <temp>/build' ||
	fail 'darling-cmake-target-fixture metadata did not resolve to a cmake target command'
printf '%s\n' "$darling_cmake_target_fixture" | grep -q \
	'cmake --build <temp>/build --target west_fixture_target' ||
	fail 'darling-cmake-target-fixture metadata did not include the target build'

python3 - <<'PY' || fail 'darling-eunion-prefix prerequisite helper is wrong'
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, "west_commands")
from prefix_repair import eunion_prefix_prerequisite_problems

with tempfile.TemporaryDirectory() as tmp:
    prefix = Path(tmp)
    (prefix / "libexec/darling").mkdir(parents=True)
    kernel = prefix / "usr/lib/system/libsystem_kernel.dylib"
    kernel.parent.mkdir(parents=True)
    kernel.write_bytes(b"plain kernel")
    assert any(
        "lacks E-UNION markers" in problem
        for problem in eunion_prefix_prerequisite_problems(prefix)
    )
    kernel.write_bytes(b"/.union-work user.union.whiteout user.union.opaque")
    assert eunion_prefix_prerequisite_problems(prefix) == []
PY

fake_darling="$(mktemp)"
cat >"$fake_darling" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = shell ]; then
	shift
	if [ -n "${WEST_GUEST_TRACE_FILE:-}" ]; then
		mkdir -p "$(dirname "$WEST_GUEST_TRACE_FILE")"
		printf 'WEST_GUEST_TRACE_OK\n' >"$WEST_GUEST_TRACE_FILE"
	fi
	exec "$@"
fi
exit 64
SH
chmod +x "$fake_darling"
mkdir -p /tmp/west-test-guest-c-fixture-prefix
DARLING="$fake_darling" DPREFIX=/tmp/west-test-guest-c-fixture-prefix \
	west test --profile __metadata_contract \
		--patch test/guest-c-fixture.patch >/dev/null

printf 'PASS west-test-metadata-contract\n'
