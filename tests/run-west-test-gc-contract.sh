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

# A scratch directory is only collectable when its creator wrote the ownership
# marker before filling it; a name-match without one is reported, never deleted.
mark_owned() {
	cat >"$1/.west-test-scratch.json" <<EOF
{"schema": 1, "kind": "west-test-scratch", "key": "contract:$1"}
EOF
}

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
mark_owned "$tmp/west-red-proof-runtime-old"
mark_owned "$tmp/west-green-proof-runtime-old"
mark_owned "$tmp/west-red-proof-source-old"
mark_owned "$tmp/west-red-proof-deploy-old"
mark_owned "$tmp/west-ctest-runtime-homebrew-old"
mark_owned "$tmp/west-runtime-homebrew-old"

# An unmarked name-match and a guest-runner output: neither is provably ours, so
# both are reported with their size and survive the real run.
mkdir -p "$tmp/west-runtime-unmarked/build"
printf 'artifact\n' >"$tmp/west-runtime-unmarked/build/lib.dylib"
printf 'guest output\n' >"$tmp/west-ctest-guest-c.unmarked"
mkdir -p "$tmp/canonical-worktree"
printf 'outside artifact\n' >"$tmp/canonical-worktree/outside"
ln -s "$tmp/canonical-worktree" "$tmp/west-ctest-runtime-symlink"
ln -s "$tmp/canonical-worktree/outside" "$tmp/west-red-proof-runtime-old/build/outside-link"
git -C ../darling worktree add --quiet --detach "$source_worktree" HEAD
mark_owned "$tmp/west-red-proof-source-worktree"

# A bundle is a timestamp-named directory and nothing else. A west dev job
# directory and another workstream's experiment root live under the same root in
# practice, and selecting by age and count over every directory is how they
# became eligible for deletion.
mkdir -p \
	"$tmp/bundles/20260101T000000Z-oldest" \
	"$tmp/bundles/20260101T000001Z-middle" \
	"$tmp/bundles/20260101T000002Z-newest" \
	"$tmp/bundles/jobs/scenario-1" \
	"$tmp/bundles/another-workstream"
printf 'bundle\n' >"$tmp/bundles/20260101T000000Z-oldest/log"
printf 'bundle\n' >"$tmp/bundles/20260101T000001Z-middle/log"
printf 'bundle\n' >"$tmp/bundles/20260101T000002Z-newest/log"
printf 'job state\n' >"$tmp/bundles/jobs/scenario-1/state"
printf 'evidence\n' >"$tmp/bundles/another-workstream/notes.md"

# The plan names what it would prune and what it refuses to touch.
mkdir -p "$tmp/bundle-scratch"
west test --gc \
	--bundle-root "$tmp/bundles" \
	--proof-scratch-root "$tmp/bundle-scratch" \
	--proof-scratch-max-age-hours 9999 \
	--keep-last 1 \
	--dry-run >"$tmp/bundle-dry.out"
grep -q 'would prune (count' "$tmp/bundle-dry.out" ||
	{ cat "$tmp/bundle-dry.out" >&2; exit 1; }
grep -q 'not a west-test bundle' "$tmp/bundle-dry.out" ||
	{ cat "$tmp/bundle-dry.out" >&2; exit 1; }
grep -q 'stale-worktree gc: would prune' "$tmp/bundle-dry.out" ||
	{ cat "$tmp/bundle-dry.out" >&2; exit 1; }
test -d "$tmp/bundles/jobs/scenario-1" ||
	{ cat "$tmp/bundle-dry.out" >&2; exit 1; }

# The real run keeps the same set: timestamped bundles go, everything else stays.
west test --gc \
	--bundle-root "$tmp/bundles" \
	--proof-scratch-root "$tmp/bundle-scratch" \
	--proof-scratch-max-age-hours 9999 \
	--keep-last 1 >"$tmp/bundle-gc.out"
grep -q 'pruned (count' "$tmp/bundle-gc.out" ||
	{ cat "$tmp/bundle-gc.out" >&2; exit 1; }
grep -q 'stale-worktree gc: pruned' "$tmp/bundle-gc.out" ||
	{ cat "$tmp/bundle-gc.out" >&2; exit 1; }
# Which timestamped bundle survives is decided by mtime, not by its name, so the
# assertion is on the count: exactly the kept one remains, and the pass did
# delete the rest.
survivors=0
for bundle in "$tmp"/bundles/2026*Z-*; do
	[[ -d "$bundle" ]] || continue
	survivors=$((survivors + 1))
done
test "$survivors" = 1 ||
	{ cat "$tmp/bundle-gc.out" >&2; echo "expected one surviving bundle, saw $survivors" >&2; exit 1; }
test ! -e "$tmp/bundles/20260101T000000Z-oldest" ||
	test ! -e "$tmp/bundles/20260101T000001Z-middle" ||
	{ cat "$tmp/bundle-gc.out" >&2; exit 1; }
test -d "$tmp/bundles/jobs/scenario-1" ||
	{ cat "$tmp/bundle-gc.out" >&2; echo "gc deleted a west dev job directory" >&2; exit 1; }
test -f "$tmp/bundles/another-workstream/notes.md" ||
	{ cat "$tmp/bundle-gc.out" >&2; echo "gc deleted an unrelated directory" >&2; exit 1; }

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
test -d "$tmp/west-runtime-unmarked" ||
	{ cat "$tmp/gc.out" >&2; echo "gc deleted unmarked scratch" >&2; exit 1; }
test -f "$tmp/west-ctest-guest-c.unmarked" ||
	{ cat "$tmp/gc.out" >&2; echo "gc deleted an unowned guest output" >&2; exit 1; }
grep -q 'left alone (scratch without an ownership marker)' "$tmp/gc.out" ||
	{ cat "$tmp/gc.out" >&2; exit 1; }
grep -q 'left alone (guest runner output' "$tmp/gc.out" ||
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
mark_owned "$tmp/west-red-proof-runtime-count-old"
mark_owned "$tmp/west-runtime-count-new"
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
