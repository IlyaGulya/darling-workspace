#!/usr/bin/env bash
# Tight E-UNION semantics confirmation: lower-read, copy-up, whiteout, readdir,
# each via ONE bounded guest command with daemon spam suppressed. Assumes the
# zero-copy boot already works (proven separately: HELLO_EUNION rc=0).
set -u
D=/usr/local/bin/darling
PREFIX=/tmp/dp/prefix
LIBEXEC=/usr/local/libexec/darling
mkdir -p /tmp/dp; rm -rf "$PREFIX"
run() { timeout 40 env DARLING_ROOTLESS=1 DPREFIX="$PREFIX" "$D" shell "$@" 2>/dev/null; }
pass=0; fail=0
ok(){ echo "  ok   $1"; pass=$((pass+1)); }
bad(){ echo "  FAIL $1"; fail=$((fail+1)); }

echo "== boot + zero-copy =="
out=$(run echo HELLO_EUNION)
[ "$out" = "HELLO_EUNION" ] && ok "guest pipeline runs (echo -> HELLO_EUNION)" || bad "guest pipeline (got: '$out')"
sz=$(du -sk "$PREFIX" 2>/dev/null | awk '{print $1}')
[ "${sz:-999999}" -lt 51200 ] && ok "zero-copy: prefix ${sz}KB < 50MB" || bad "prefix too big: ${sz}KB"

echo "== lower-layer read (template-only file visible in guest) =="
# /usr/lib/dyld exists only in the template, never copied to the empty upper
out=$(run sh -c 'test -f /usr/lib/dyld && echo LOWER_OK')
[ "$out" = "LOWER_OK" ] && ok "lower read: /usr/lib/dyld visible via union" || bad "lower read (got: '$out')"

echo "== copy-up (writing a template-only file materializes an upper copy) =="
# pick a small template-only file, append in guest, check an upper copy appears
run sh -c 'echo MARK >> /etc/profile' >/dev/null
if [ -f "$PREFIX/private/etc/profile" ] || [ -f "$PREFIX/etc/profile" ]; then
  ok "copy-up: upper copy of /etc/profile created after write"
else
  bad "copy-up: no upper copy of /etc/profile"
fi

echo "== whiteout (deleting a template file hides it across boots) =="
run sh -c 'rm -f /etc/profile' >/dev/null
out=$(run sh -c 'test -e /etc/profile && echo STILL_THERE || echo GONE')
[ "$out" = "GONE" ] && ok "whiteout: /etc/profile stays gone after delete" || bad "whiteout (got: '$out')"

echo "== readdir-merge (System/Library/LaunchDaemons shows template plists) =="
n=$(run sh -c 'ls /System/Library/LaunchDaemons | wc -l')
tn=$(ls "$LIBEXEC/System/Library/LaunchDaemons" | wc -l)
[ "${n:-0}" -ge 1 ] && [ "${n:-0}" -eq "$tn" ] && ok "readdir-merge: $n entries == template $tn" || bad "readdir-merge: guest=$n template=$tn"

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
