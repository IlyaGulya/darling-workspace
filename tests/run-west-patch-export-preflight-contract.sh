#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

profile_root="patches/__export_preflight_contract"
trap 'rm -rf "$profile_root"' EXIT
rm -rf "$profile_root"
mkdir -p "$profile_root/test"

darling_repo="$repo/../darling"
source_branch="fix/mldr-glibc-fork-reset"
source_commit="$(git -C "$darling_repo" rev-parse "$source_branch")"
source_base="$(git -C "$darling_repo" rev-parse "${source_branch}^")"
nonancestor_base="$(git -C "$darling_repo" rev-parse fix/sandbox-exec-pass-through)"
missing_base=ffffffffffffffffffffffffffffffffffffffff
missing_commit=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee

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

printf 'PASS west-patch-export-preflight-contract\n'
