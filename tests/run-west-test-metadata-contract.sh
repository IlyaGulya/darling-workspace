#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
tmp_profile="patches/__metadata_contract"
trap 'rm -rf "$tmp_profile"' EXIT
mkdir -p "$tmp_profile"
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
    include-dirs: [src]
    stub-headers: [darling/example.h]
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
if printf '%s\n' "$bare" | grep -q 'darling-debug-runner run '; then
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
printf '%s\n' "$source_only_check" | grep -q 'MODEL     test/model.patch' ||
	fail 'model-tier patch was not reported as MODEL'
printf '%s\n' "$source_only_check" | grep -q 'COMPILE   test/c-fixture.patch' ||
	fail 'c-fixture patch was not reported as COMPILE'

c_fixture="$(
	west test --profile __metadata_contract \
		--patch test/c-fixture.patch \
		--list
)"

printf '%s\n' "$c_fixture" | grep -q \
	'cc -std=gnu11 -Wall -Wextra -Werror -I src -I <generated-stubs> tests/c_fixture_contract.c -o' ||
	fail 'c-fixture metadata did not resolve to a compile-and-run command'
printf '%s\n' "$source_only_check" | grep -q 'test metadata: 4 covered (runtime 1, compile 1, host 1, model 1)' ||
	fail 'coverage-tier summary did not classify runtime/host/compile/model coverage'

guest_c_fixture="$(
	west test --profile __metadata_contract \
		--patch test/guest-c-fixture.patch \
		--prefix /tmp/west-test-guest-c-fixture-prefix \
		--list
)"

printf '%s\n' "$guest_c_fixture" | grep -q \
	'<upload> tests/guest_c_fixture_contract.c && darling shell' ||
	fail 'guest-c-fixture metadata did not resolve to a guest compile-and-run command'
printf '%s\n' "$source_only_check" | grep -q 'RUNTIME   test/guest-c-fixture.patch' ||
	fail 'guest-c-fixture patch was not reported as RUNTIME'

fake_darling="$(mktemp)"
cat >"$fake_darling" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
if [ "$1" = shell ]; then
	shift
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
