#!/usr/bin/env bash
set -euo pipefail

tmp="$(mktemp -d)"
missing_commit=1111111111111111111111111111111111111111

cleanup() {
	rm -rf "$tmp"
}
trap cleanup EXIT

fail() {
	echo "$*" >&2
	exit 1
}

upstream="$tmp/upstream"
super="$tmp/super"
git init -q "$upstream"
git -C "$upstream" config user.email test@example.invalid
git -C "$upstream" config user.name 'rtk contract'
touch "$upstream/README"
git -C "$upstream" add README
git -C "$upstream" commit -qm 'upstream base'

git init -q "$super"
git -C "$super" config user.email test@example.invalid
git -C "$super" config user.name 'rtk contract'
git -C "$super" config -f .gitmodules submodule.missing.path sub
git -C "$super" config -f .gitmodules submodule.missing.url "file://$upstream"
git -C "$super" add .gitmodules
git -C "$super" update-index --add \
	--cacheinfo "160000,$missing_commit,sub"
git -C "$super" commit -qm 'missing submodule object'

if rtk bash -c '
	rtk git -C "$1" -c protocol.file.allow=always submodule update --init sub
	exit $?
' bash "$super" >"$tmp/normal.out" 2>&1; then
	fail 'nested rtk turned a missing submodule object into success'
fi
grep -F -q "$missing_commit" "$tmp/normal.out" ||
	fail 'nested rtk did not run the missing-object Git fixture'

if rtk bash -c '
	rtk proxy git -C "$1" -c protocol.file.allow=always submodule update --init sub
	exit $?
' bash "$super" >"$tmp/proxy.out" 2>&1; then
	fail 'nested rtk proxy turned a missing submodule object into success'
fi
grep -F -q "$missing_commit" "$tmp/proxy.out" ||
	fail 'nested rtk proxy did not run the missing-object Git fixture'

printf 'PASS rtk-exit-status-contract\n'
