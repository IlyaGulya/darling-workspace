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
    ctest-label: bead:dar-gwn.5
YAML

fail() {
	printf 'FAIL: %s\n' "$*" >&2
	exit 1
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

printf 'PASS west-test-metadata-contract\n'
