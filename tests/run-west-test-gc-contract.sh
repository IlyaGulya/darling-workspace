#!/usr/bin/env bash
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"

export PYTHONDONTWRITEBYTECODE=1

tmp="$(mktemp -d)"
source_worktree="$tmp/west-red-proof-source-worktree/darling"
cleanup() {
	git -C ../darling worktree remove --force "$source_worktree" >/dev/null 2>&1 || true
	rm -rf "$tmp"
}
trap cleanup EXIT

mkdir -p \
	"$tmp/west-red-proof-runtime-old/build" \
	"$tmp/west-green-proof-runtime-old/build" \
	"$tmp/west-red-proof-source-old/build" \
	"$tmp/west-red-proof-deploy-old/build" \
	"$tmp/west-ctest-runtime-homebrew-old/build" \
	"$tmp/west-runtime-homebrew-old/build" \
	"$tmp/not-west-red-proof-runtime"
printf 'artifact\n' >"$tmp/west-red-proof-runtime-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-green-proof-runtime-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-red-proof-source-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-red-proof-deploy-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-ctest-runtime-homebrew-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-runtime-homebrew-old/build/lib.dylib"
mkdir -p "$tmp/canonical-worktree"
printf 'outside artifact\n' >"$tmp/canonical-worktree/outside"
ln -s "$tmp/canonical-worktree" "$tmp/west-ctest-runtime-symlink"
ln -s "$tmp/canonical-worktree/outside" "$tmp/west-red-proof-runtime-old/build/outside-link"
git -C ../darling worktree add --quiet --detach "$source_worktree" HEAD

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
test -d "$tmp/west-red-proof-source-old" ||
	{ cat "$tmp/dry.out" >&2; exit 1; }
test -d "$tmp/west-red-proof-deploy-old" ||
	{ cat "$tmp/dry.out" >&2; exit 1; }
test -d "$tmp/west-ctest-runtime-homebrew-old" ||
	{ cat "$tmp/dry.out" >&2; exit 1; }
test -d "$tmp/west-runtime-homebrew-old" ||
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
test ! -e "$tmp/west-red-proof-source-old" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test ! -e "$tmp/west-red-proof-deploy-old" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test ! -e "$tmp/west-ctest-runtime-homebrew-old" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test ! -e "$tmp/west-runtime-homebrew-old" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
if git -C ../darling worktree list --porcelain |
	grep -F -x -q "worktree $source_worktree"; then
	cat "$tmp/gc.out" >&2
	echo "gc left source-proof worktree metadata: $source_worktree" >&2
	exit 1
fi
test -d "$tmp/not-west-red-proof-runtime" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test -L "$tmp/west-ctest-runtime-symlink" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
test -f "$tmp/canonical-worktree/outside" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }

mkdir -p \
	"$tmp/west-red-proof-runtime-count-old/build" \
	"$tmp/west-runtime-count-new/build"
printf 'artifact\n' >"$tmp/west-red-proof-runtime-count-old/build/lib.dylib"
printf 'artifact\n' >"$tmp/west-runtime-count-new/build/lib.dylib"
touch -d '2 hours ago' "$tmp/west-red-proof-runtime-count-old"

west test --gc \
	--bundle-root "$tmp/bundles" \
	--proof-scratch-root "$tmp" \
	--proof-scratch-max-age-hours 9999 \
	--proof-scratch-keep-last 1 >"$tmp/count.out"

grep -q 'pruned proof scratch' "$tmp/count.out" ||
	{ cat "$tmp/count.out" >&2; exit 1; }
grep -q 'retained proof scratch' "$tmp/count.out" ||
	{ cat "$tmp/count.out" >&2; exit 1; }
test ! -e "$tmp/west-red-proof-runtime-count-old" ||
	{ cat "$tmp/count.out" >&2; exit 1; }
test -d "$tmp/west-runtime-count-new" ||
	{ cat "$tmp/count.out" >&2; exit 1; }

printf 'PASS west-test-gc-contract\n'
