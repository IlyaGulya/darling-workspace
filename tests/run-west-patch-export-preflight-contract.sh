#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

profile_root="patches/__export_preflight_contract"
trap 'rm -rf "$profile_root"' EXIT
rm -rf "$profile_root"
mkdir -p "$profile_root/test"

darling_repo="$repo/../darling"
darlingserver_repo="$repo/../darling/src/external/darlingserver"
source_branch="fix/mldr-glibc-fork-reset"
source_commit="$(git -C "$darling_repo" rev-parse "$source_branch")"
source_base="$(git -C "$darling_repo" rev-parse "${source_branch}^")"
nonancestor_base="$(git -C "$darling_repo" rev-parse fix/sandbox-exec-pass-through)"
missing_base=ffffffffffffffffffffffffffffffffffffffff
missing_commit=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
stack_first_branch="fix/server-inline-fastpath"
stack_second_branch="fix/checkin-rwlock-across-suspend"
stack_first_commit="$(git -C "$darlingserver_repo" rev-parse "$stack_first_branch")"
stack_second_commit="$(git -C "$darlingserver_repo" rev-parse "$stack_second_branch")"
stack_stale_base="$(git -C "$darlingserver_repo" rev-parse "${stack_first_branch}^")"

write_profile() {
	local source_base_value="$1"
	local source_commit_value="$2"
	cat >"$profile_root/patches.yml" <<YAML
version: 1
integration-date: '2026-07-09T00:00:00Z'
patches:
- path: test/preflight.patch
  sha256sum: 0000000000000000000000000000000000000000000000000000000000000000
  module: darling
  source-branch: $source_branch
  source-base: $source_base_value
  source-commit: $source_commit_value
  bead: dar-jqlo
  publication-status: blocked
  publication-blocker: export preflight contract fixture
YAML
}

write_stack_profile() {
	cat >"$profile_root/patches.yml" <<YAML
version: 1
integration-date: '2026-07-09T00:00:00Z'
patches:
- path: test/first.patch
  sha256sum: 0000000000000000000000000000000000000000000000000000000000000000
  module: darling/src/external/darlingserver
  source-branch: $stack_first_branch
  source-commit: $stack_first_commit
  bead: dar-jqlo
  publication-status: blocked
  publication-blocker: export preflight contract fixture
- path: test/second.patch
  sha256sum: 0000000000000000000000000000000000000000000000000000000000000000
  module: darling/src/external/darlingserver
  source-branch: $stack_second_branch
  source-base: $stack_stale_base
  source-commit: $stack_second_commit
  bead: dar-jqlo
  publication-status: blocked
  publication-blocker: export preflight contract fixture
YAML
}

fail() {
	printf 'FAIL: %s\n' "$*" >&2
	exit 1
}

printf 'sentinel\n' >"$profile_root/test/preflight.patch"
write_profile "$missing_base" "$source_commit"
if west patch export --profile __export_preflight_contract >"$profile_root/base.out" 2>&1; then
	fail 'export with missing source-base unexpectedly passed'
fi
grep -q "test/preflight.patch: source-base $missing_base is not available" "$profile_root/base.out" ||
	fail 'missing source-base diagnostic did not name the patch and revision'
grep -q "source branch parent is $source_base" "$profile_root/base.out" ||
	fail 'missing source-base diagnostic did not suggest current branch parent'
test "$(cat "$profile_root/test/preflight.patch")" = "sentinel" ||
	fail 'export wrote patch output before missing source-base preflight failed'

printf 'sentinel\n' >"$profile_root/test/preflight.patch"
write_profile "$source_base" "$missing_commit"
if west patch export --profile __export_preflight_contract >"$profile_root/commit.out" 2>&1; then
	fail 'export with missing source-commit unexpectedly passed'
fi
grep -q "test/preflight.patch: source-commit $missing_commit is not available" "$profile_root/commit.out" ||
	fail 'missing source-commit diagnostic did not name the patch and revision'
grep -q "source branch currently points to $source_commit" "$profile_root/commit.out" ||
	fail 'missing source-commit diagnostic did not suggest current branch head'
test "$(cat "$profile_root/test/preflight.patch")" = "sentinel" ||
	fail 'export wrote patch output before missing source-commit preflight failed'

printf 'sentinel\n' >"$profile_root/test/preflight.patch"
write_profile "$nonancestor_base" "$source_commit"
if west patch export --profile __export_preflight_contract >"$profile_root/nonancestor.out" 2>&1; then
	fail 'export with non-ancestor source-base unexpectedly passed'
fi
grep -q "test/preflight.patch: source-base $nonancestor_base is not an ancestor" "$profile_root/nonancestor.out" ||
	fail 'non-ancestor source-base diagnostic did not name the patch and revision'
grep -q "source branch parent is $source_base" "$profile_root/nonancestor.out" ||
	fail 'non-ancestor source-base diagnostic did not suggest current branch parent'
test "$(cat "$profile_root/test/preflight.patch")" = "sentinel" ||
	fail 'export wrote patch output before non-ancestor source-base preflight failed'

printf 'first-sentinel\n' >"$profile_root/test/first.patch"
printf 'second-sentinel\n' >"$profile_root/test/second.patch"
write_stack_profile
if west patch export --profile __export_preflight_contract >"$profile_root/stale-stack-base.out" 2>&1; then
	fail 'export with stale linear-stack source-base unexpectedly passed'
fi
grep -q "test/second.patch: source-base $stack_stale_base is stale for the linear darling/src/external/darlingserver stack" "$profile_root/stale-stack-base.out" ||
	fail 'stale linear-stack source-base diagnostic did not name the patch and revision'
grep -q "previous profile patch is $stack_first_commit" "$profile_root/stale-stack-base.out" ||
	fail 'stale linear-stack source-base diagnostic did not name the previous patch commit'
test "$(cat "$profile_root/test/first.patch")" = "first-sentinel" ||
	fail 'stale linear-stack source-base preflight wrote first patch output before failing'
test "$(cat "$profile_root/test/second.patch")" = "second-sentinel" ||
	fail 'stale linear-stack source-base preflight wrote second patch output before failing'

printf 'first-sentinel\n' >"$profile_root/test/first.patch"
printf 'second-sentinel\n' >"$profile_root/test/second.patch"
write_stack_profile
west patch export \
	--profile __export_preflight_contract \
	--patch test/first.patch >"$profile_root/focused-first.out" 2>&1
grep -q 'exported test/first.patch' "$profile_root/focused-first.out" ||
	fail 'focused export did not write the selected patch'
if grep -q 'test/second.patch' "$profile_root/focused-first.out"; then
	fail 'focused export reported the unselected patch'
fi
if test "$(cat "$profile_root/test/first.patch")" = "first-sentinel"; then
	fail 'focused export left the selected patch unchanged'
fi
test "$(cat "$profile_root/test/second.patch")" = "second-sentinel" ||
	fail 'focused export wrote the unselected patch'
west patch export \
	--profile __export_preflight_contract \
	--patch test/first.patch \
	--check >"$profile_root/focused-first-check.out" 2>&1
grep -q 'export-check OK test/first.patch' "$profile_root/focused-first-check.out" ||
	fail 'focused export --check did not verify the selected patch'
if grep -q 'test/second.patch' "$profile_root/focused-first-check.out"; then
	fail 'focused export --check reported the unselected patch'
fi

printf 'sentinel\n' >"$profile_root/test/preflight.patch"
write_profile "$source_base" "$source_commit"
if WEST_PATCH_EXPORT_MAX_LINES=1 west patch export \
	--profile __export_preflight_contract >"$profile_root/large.out" 2>&1; then
	fail 'large export unexpectedly passed without explicit override'
fi
grep -q 'pass --allow-large-output to write it' "$profile_root/large.out" ||
	fail 'large export diagnostic did not advertise explicit override'
test "$(cat "$profile_root/test/preflight.patch")" = "sentinel" ||
	fail 'large export guard wrote patch output before failing'

WEST_PATCH_EXPORT_MAX_LINES=1 west patch export \
	--profile __export_preflight_contract \
	--allow-large-output >"$profile_root/allow.out" 2>&1
grep -q 'exported test/preflight.patch' "$profile_root/allow.out" ||
	fail 'explicit large export override did not write the patch'
if test "$(cat "$profile_root/test/preflight.patch")" = "sentinel"; then
	fail 'explicit large export override left the patch unchanged'
fi

if west patch export \
	--profile __export_preflight_contract \
	--patch test/does-not-exist.patch \
	--check >"$profile_root/focused-missing.out" 2>&1; then
	fail 'focused export accepted an unknown patch selector'
fi
grep -q 'test/does-not-exist.patch: patch not found in profile' "$profile_root/focused-missing.out" ||
	fail 'focused export did not report an unknown patch selector clearly'

west patch export --help | grep -q -- '--patch' ||
	fail 'west patch export help does not advertise --patch'

printf 'PASS west-patch-export-preflight-contract\n'
