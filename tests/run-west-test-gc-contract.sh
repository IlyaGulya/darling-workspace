#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

export PYTHONDONTWRITEBYTECODE=1

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/west-red-proof-runtime-old/build" \
	"$tmp/west-green-proof-runtime-old/build" \
	"$tmp/west-red-proof-source-keep" \
	"$tmp/not-west-red-proof-runtime"
printf 'artifact\n' >"$tmp/west-red-proof-runtime-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-green-proof-runtime-old/build/lib.dylib"

west test --gc \
	--bundle-root "$tmp/bundles" \
	--proof-scratch-root "$tmp" \
	--proof-scratch-max-age-hours 0 \
	--dry-run >"$tmp/dry.out"

grep -q 'would prune proof scratch' "$tmp/dry.out" ||
	{ cat "$tmp/dry.out" >&2; exit 1; }
test -d "$tmp/west-red-proof-runtime-old" ||
	{ cat "$tmp/dry.out" >&2; exit 1; }
test -d "$tmp/west-green-proof-runtime-old" ||
	{ cat "$tmp/dry.out" >&2; exit 1; }

west test --gc \
	--bundle-root "$tmp/bundles" \
	--proof-scratch-root "$tmp" \
	--proof-scratch-max-age-hours 0 >"$tmp/gc.out"

grep -q 'pruned proof scratch' "$tmp/gc.out" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test ! -e "$tmp/west-red-proof-runtime-old" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test ! -e "$tmp/west-green-proof-runtime-old" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test -d "$tmp/west-red-proof-source-keep" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test -d "$tmp/not-west-red-proof-runtime" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }

printf 'PASS west-test-gc-contract\n'
