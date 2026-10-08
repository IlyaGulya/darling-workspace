#!/bin/bash
# Contract for `dwdiag waits`: the verdict on one ulock object must separate three
# DIFFERENT defects, not rank one. Fixture log below carries exactly one object of
# each kind:
#   one -- waiters and no wake event at all            -> NEVER-WOKEN + WAITS-STALL
#   one -- a wake was issued and found nobody (enoent) -> WAKE-MISSED-THE-WAITER
#   one -- a wake ran and moved one waiter             -> WOKEN, not stalling
#
# Liveness is constructed, not assumed. A bare pid is not evidence of presence: this
# host HAS /proc/100, a kernel thread whose tid is also 100, so a fixture using pid 100
# was reported `proc=present` about a park logged by something else. The cases below
# therefore use the running shell's own pid for "the process is here", and thread ids
# that pid cannot have for "it is gone".
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DWDIAG="$ROOT/scripts/dwdiag"
LOG=$(mktemp /tmp/dwdiag-waits-contract-XXXX.log)
LOG2=$(mktemp /tmp/dwdiag-waits-contract-present-XXXX.log)
trap 'rm -f "$LOG" "$LOG2"' EXIT

fail=0
note() { echo "waits-contract: $1"; }
check() {
	if echo "$3" | grep -q "$1"; then
		note "ok: $2"
	else
		note "FAIL: missing $2   (wanted: $1)"
		fail=1
	fi
}
absent() {
	if echo "$3" | grep -q "$1"; then
		note "FAIL: unexpected $2   (matched: $1)"
		fail=1
	else
		note "ok: no $2"
	fi
}

# --- log 1: every stalling object belongs to a process that is gone -> history, not a stall
cat >"$LOG" <<EOF
[boot] nothing to see
[ulock-wait-park] pid=$$ op=0x1050002 addr=0x1000 word=0x102 owner_tid=0 owner_ptr=(nil) waiter_tid=1 nwaiters=1
[ulock-wait-park] pid=$$ op=0x1050002 addr=0x2000 word=0x102 owner_tid=2 owner_ptr=(nil) waiter_tid=3 nwaiters=1
[ulock-wake-enoent] pid=$$ addr=0x2000 op=0x1050002
[ulock-wait-park] pid=$$ op=0x1050002 addr=0x3000 word=0x102 owner_tid=4 owner_ptr=(nil) waiter_tid=5 nwaiters=1
[ulock-wake] pid=$$ addr=0x3000 op=0x1050002 nwaiters=1 woke=1 set_owner=1
EOF

out=$("$DWDIAG" waits --log "$LOG" 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
	note "FAIL: dwdiag waits exited $rc"
	echo "$out" | tail -5
	exit 1
fi

check "OBJECT pid=$$ .* verdict=NEVER-WOKEN" "the no-wake object is NEVER-WOKEN" "$out"
check "WAITS-STALL pid=$$ addr=0x1000 .* proc=absent" "a park logged by a process that is gone says so" "$out"
check "WAITS-NOTE all 1 stalling object" "an all-absent stall set is called history, not a stall" "$out"
check "WAITS-VERDICT objects=3 .* stalling=1" "exactly one object is stalling" "$out"
absent "WAITS-STALL pid=$$ addr=0x2000" "the wake-found-nobody object is not stalling" "$out"
absent "WAITS-STALL pid=$$ addr=0x3000" "the woken object is not stalling" "$out"

# --- log 2: the same park, but its threads ARE threads of that process -> a real stall
cat >"$LOG2" <<EOF
[ulock-wait-park] pid=$$ op=0x1050002 addr=0x1000 word=0x102 owner_tid=$$ owner_ptr=(nil) waiter_tid=$$ nwaiters=1
EOF
out2=$("$DWDIAG" waits --log "$LOG2" 2>&1)
check "WAITS-STALL pid=$$ .* proc=present" "a park whose threads are still there is present" "$out2"
absent "WAITS-NOTE" "a present stall is NOT dismissed as history" "$out2"

echo "$out" | tail -4
if [ "$fail" -ne 0 ]; then
	note "FAIL"
	exit 1
fi
note "PASS"
exit 0
