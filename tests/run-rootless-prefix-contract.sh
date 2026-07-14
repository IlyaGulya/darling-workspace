#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf -- "$tmp"' EXIT

export RUNNER_TEMP="$tmp/runner"
export ROOTLESS_TIER_REPO="$repo"
export GITHUB_OUTPUT="$tmp/github-output"
. "$repo/ci/rootless-prefix.sh"

created="$(rootless_prefix_create smoke UNUSED_VARIABLE)"
case "$created" in
	"$RUNNER_TEMP"/darling-rootless-smoke.*) ;;
	*) echo "unexpected generated prefix: $created" >&2; exit 1 ;;
esac
rootless_prefix_assert_owned smoke "$created"
rootless_prefix_remove smoke "$created"
[ ! -e "$created" ]

requested="$RUNNER_TEMP/darling-rootless-regression-contract"
DARLING_REGRESSION_PREFIX="$requested"
created="$(rootless_prefix_create regression DARLING_REGRESSION_PREFIX)"
[ "$created" = "$requested" ]
rootless_prefix_remove regression "$created"

if RUNNER_TEMP=/ rootless_prefix_trusted_root >/dev/null 2>&1; then
	echo 'filesystem root was accepted as trusted root' >&2
	exit 1
fi

# GitHub-hosted Linux runners place RUNNER_TEMP below /home/runner. Accept that
# explicit runner contract while retaining the direct-child/name/owner guards.
mkdir -p "$tmp/home/work/_temp"
created="$(HOME="$tmp/home" RUNNER_TEMP="$tmp/home/work/_temp" \
	rootless_prefix_create smoke UNUSED_VARIABLE)"
HOME="$tmp/home" RUNNER_TEMP="$tmp/home/work/_temp" \
	rootless_prefix_remove smoke "$created"

if DARLING_SMOKE_PREFIX="$HOME" rootless_prefix_create smoke DARLING_SMOKE_PREFIX >/dev/null 2>&1; then
	echo 'HOME was accepted as a removable prefix' >&2
	exit 1
fi
if DARLING_SMOKE_PREFIX="$repo" rootless_prefix_create smoke DARLING_SMOKE_PREFIX >/dev/null 2>&1; then
	echo 'workspace was accepted as a removable prefix' >&2
	exit 1
fi

target="$RUNNER_TEMP/real-prefix"
mkdir -p "$target"
ln -s "$target" "$RUNNER_TEMP/darling-rootless-smoke-symlink"
if DARLING_SMOKE_PREFIX="$RUNNER_TEMP/darling-rootless-smoke-symlink" rootless_prefix_create smoke DARLING_SMOKE_PREFIX >/dev/null 2>&1; then
	echo 'symlink prefix was accepted' >&2
	exit 1
fi

owned="$(rootless_prefix_create regression UNUSED_VARIABLE)"
if rootless_prefix_remove smoke "$owned" >/dev/null 2>&1; then
	echo 'prefix with mismatched owner kind was removed' >&2
	exit 1
fi
[ -d "$owned" ]
rootless_prefix_remove regression "$owned"

toolchain_prefix="$(rootless_prefix_create toolchain UNUSED_VARIABLE)"
rootless_prefix_export_output prefix "$toolchain_prefix"
grep -F -x -q "prefix=$toolchain_prefix" "$GITHUB_OUTPUT"
mkdir -p \
	"$toolchain_prefix/Library/Developer/CommandLineTools/usr/bin" \
	"$toolchain_prefix/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk"
touch "$toolchain_prefix/Library/Developer/CommandLineTools/usr/bin/clang"
chmod +x "$toolchain_prefix/Library/Developer/CommandLineTools/usr/bin/clang"
rootless_prefix_assert_guest_toolchain toolchain "$toolchain_prefix"
rm -rf -- "$toolchain_prefix/Library"
rootless_prefix_assert_no_guest_toolchain toolchain "$toolchain_prefix"
mkdir -p "$toolchain_prefix/Library/Developer/CommandLineTools"
if rootless_prefix_assert_no_guest_toolchain toolchain "$toolchain_prefix" >/dev/null 2>&1; then
	echo 'partial CommandLineTools installation passed no-CLT assertion' >&2
	exit 1
fi
rootless_prefix_remove toolchain "$toolchain_prefix"

printf 'PASS rootless-prefix-contract\n'
