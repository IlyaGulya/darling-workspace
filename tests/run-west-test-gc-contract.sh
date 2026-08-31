#!/usr/bin/env bash
set -euo pipefail
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo"
export PYTHONDONTWRITEBYTECODE=1
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

create_root() {
	python3 -B scripts/owned-scratch.py --namespace "$tmp" create --kind runtime-proof --prefix "$1-"
}
old_one="$(create_root old-one)"
old_two="$(create_root old-two)"
new_one="$(create_root new-one)"
unmarked="$tmp/west-red-proof-runtime-unmarked"
mkdir -p "$old_one/build" "$old_two/build" "$new_one/build" "$unmarked/build"
dd if=/dev/zero of="$old_one/build/blob" bs=1024 count=8 status=none
dd if=/dev/zero of="$old_two/build/blob" bs=1024 count=4 status=none
printf 'unmarked\n' >"$unmarked/build/sentinel"

python3 -B - "$old_one" "$old_two" <<'PY'
import sys,time
from pathlib import Path
for offset, text in enumerate(sys.argv[1:]):
    marker=Path(text)/'.darling-scratch-v1'
    values=dict(line.split('=',1) for line in marker.read_text().splitlines())
    values['created_ns']=str(time.time_ns()-(100+offset)*3600*1_000_000_000)
    marker.write_text(''.join(f'{key}={values[key]}\n' for key in (
        'version','kind','id','created_ns'
    )))
    seconds=int(values['created_ns'])/1_000_000_000
    __import__('os').utime(marker.parent,(seconds,seconds),follow_symlinks=False)
PY

west test --gc --bundle-root "$tmp/bundles" --proof-scratch-root "$tmp" \
	--proof-scratch-max-age-hours 72 --proof-scratch-keep-last 2 --dry-run >"$tmp/dry.out"
grep -q 'would prune owned scratch' "$tmp/dry.out"
test -d "$old_one" && test -d "$old_two"

west test --gc --bundle-root "$tmp/bundles" --proof-scratch-root "$tmp" \
	--proof-scratch-max-age-hours 72 --proof-scratch-keep-last 1 >"$tmp/gc.out"
grep -q 'pruned owned scratch' "$tmp/gc.out"
test ! -e "$old_one" && test ! -e "$old_two"
test -d "$new_one"
test "$(cat "$unmarked/build/sentinel")" = unmarked

west test --gc --bundle-root "$tmp/bundles" --proof-scratch-root "$tmp" \
	--scratch-discard "$new_one" --dry-run >"$tmp/exact-dry.out"
test -d "$new_one"
west test --gc --bundle-root "$tmp/bundles" --proof-scratch-root "$tmp" \
	--scratch-discard "$new_one" >"$tmp/exact.out"
test ! -e "$new_one"
printf 'PASS west-test-gc-contract\n'
