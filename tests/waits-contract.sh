#!/bin/bash
# Contract for `dwdiag waits`: the verdict on one ulock object must separate three
# DIFFERENT defects, not rank one. Fixture log below carries exactly one object of
# each kind:
#   pid=100 -- waiters and no wake event at all            -> NEVER-WOKEN + WAITS-STALL
#   pid=200 -- a wake was issued and found nobody (enoent) -> WAKE-MISSED-THE-WAITER
#   pid=300 -- a wake ran and moved one waiter             -> WOKEN, not stalling
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DWDIAG="$ROOT/scripts/dwdiag"
LOG=$(mktemp /tmp/dwdiag-waits-contract-XXXX.log)
trap 'rm -f "$LOG"' EXIT

cat >"$LOG" <<'EOF'
[boot] nothing to see
[ulock-wait-park] pid=100 op=0x1050002 addr=0x1000 word=0x102 owner_tid=100 owner_ptr=(nil) waiter_tid=101 nwaiters=1
[ulock-wait-park] pid=200 op=0x1050002 addr=0x2000 word=0x102 owner_tid=200 owner_ptr=(nil) waiter_tid=201 nwaiters=1
[ulock-wake-enoent] pid=200 addr=0x2000 op=0x1050002
[ulock-wait-park] pid=300 op=0x1050002 addr=0x3000 word=0x102 owner_tid=300 owner_ptr=(nil) waiter_tid=301 nwaiters=1
[ulock-wake] pid=300 addr=0x3000 op=0x1050002 nwaiters=1 woke=1 set_owner=1
EOF

out=$("$DWDIAG" waits --log "$LOG" 2>&1)
rc=$?
fail=0
note() { echo "waits-contract: $1"; }

if [ "$rc" -ne 0 ]; then
	note "FAIL: dwdiag waits exited $rc"
	echo "$out" | tail -5
	exit 1
fi

check() {
	if echo "$out" | grep -q "$1"; then
		note "ok: $2"
	else
		note "FAIL: missing $2   (wanted: $1)"
		fail=1
	fi
}
absent() {
	if echo "$out" | grep -q "$1"; then
		note "FAIL: unexpected $2   (matched: $1)"
		fail=1
	else
		note "ok: no $2"
	fi
}

check "OBJECT pid=100 .* verdict=NEVER-WOKEN" "pid=100 is NEVER-WOKEN"
check "WAITS-STALL pid=100 " "pid=100 is reported as stalling"
check "OBJECT pid=200 .* verdict=WAKE-MISSED-THE-WAITER" "pid=200 is WAKE-MISSED-THE-WAITER"
check "OBJECT pid=300 .* verdict=WOKEN" "pid=300 is WOKEN"
check "WAITS-VERDICT objects=3 .* stalling=1" "exactly one object is stalling"
absent "WAITS-STALL pid=200 " "pid=200 counted as stalling"
absent "WAITS-STALL pid=300 " "pid=300 counted as stalling"

echo "$out" | tail -4
if [ "$fail" -ne 0 ]; then
	note "FAIL"
	exit 1
fi
note "PASS"
exit 0
