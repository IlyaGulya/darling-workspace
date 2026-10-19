#!/usr/bin/env bash
# run-plane-deferred-arm-oracle-selftest.sh -- host-only proof of the RED/GREEN oracle.
#
# The live gate (`run-plane-deferred-arm-contract.sh`) analyses a run's server delta.
# This self-test exercises that analysis with deterministic inputs, so the oracle can be
# trusted before any prefix is booted, and it pins the cases that must never look alike:
#
#   1. the real captured RED delta                       -> expect red: PASS
#   2. the same delta judged as GREEN                    -> FAIL (it does not pass by accident)
#   3. synthetic fixed delta + product diagnostics       -> expect green: PASS
#   4. synthetic fixed delta WITHOUT the disarm record   -> expect green (existing): PASS
#      ... and with --oracle product                    -> INVALID (instrument absent)
#   5. synthetic fixed delta + a plane-call-unowned      -> expect green (product): FAIL
#   6. empty delta                                       -> FAIL under both expectations
#                                                          (an absent workload is never PASS)
#
# No prefix, no boot, no build, no product change.

set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
gate="$here/run-plane-deferred-arm-contract.sh"
real_red="$here/../evidence/plane-deferred-arm/verify14-run/park.server-delta.log"
real_pid=2882424

[ -x "$gate" ] || { echo "oracle-selftest: missing gate $gate" >&2; exit 2; }
tmp="$(mktemp -d "${TMPDIR:-/tmp}/plane-deferred-oracle.XXXXXX")"
trap 'rm -rf "$tmp"' EXIT

fail=0
check() { # <name> <expected-rc> <command...>
	local name="$1" want="$2"; shift 2
	local out rc=0
	out="$("$@" 2>&1)" || rc=$?
	if [ "$rc" -eq "$want" ]; then
		echo "ORACLE-SELFTEST: ok: $name (rc=$rc)"
	else
		echo "ORACLE-SELFTEST: FAIL: $name: expected rc=$want, got rc=$rc" >&2
		printf '%s\n' "$out" >&2
		fail=1
	fi
}

# 1. the real RED delta
check "real RED delta -> red" 0 \
	"$gate" --analyse "$real_red" --pid "$real_pid" --expect red --oracle existing

# 2. the real RED delta must not pass as GREEN
check "real RED delta -> green must FAIL" 1 \
	"$gate" --analyse "$real_red" --pid "$real_pid" --expect green --oracle existing

# --- synthetic fixed (GREEN) deltas -------------------------------------------------
pid=424242
make_fixed() { # <file> [extra lines]
	local f="$1"; shift
	{
		echo "[1.0](call-deferred, Error) call-deferred site=setPendingCall-parked tid=$pid number=16 active_call=38 pending=0 suspended=1 interrupts=0"
		echo "[1.1](plane-call-disarmed, Error) plane-call-disarmed tid=$pid call=16 pending_before=1"
		echo "[2.0](call-deferred, Error) call-deferred site=setPendingCall-parked tid=$pid number=16 active_call=38 pending=0 suspended=1 interrupts=0"
		echo "[2.1](plane-call-disarmed, Error) plane-call-disarmed tid=$pid call=16 pending_before=1"
		for line in "$@"; do echo "$line"; done
	} > "$f"
}
make_nodisarm() { # the shape without the requested product diagnostic
	local f="$1"
	echo "[1.0](call-deferred, Error) call-deferred site=setPendingCall-parked tid=$pid number=16 active_call=38 pending=0 suspended=1 interrupts=0" > "$f"
}

make_fixed "$tmp/fixed.log"
check "fixed delta -> green (existing)" 0 \
	"$gate" --analyse "$tmp/fixed.log" --pid "$pid" --expect green --oracle existing
check "fixed delta -> green (product)" 0 \
	"$gate" --analyse "$tmp/fixed.log" --pid "$pid" --expect green --oracle product

make_nodisarm "$tmp/nodisarm.log"
check "fixed delta without disarm record -> green (existing)" 0 \
	"$gate" --analyse "$tmp/nodisarm.log" --pid "$pid" --expect green --oracle existing
check "fixed delta without disarm record -> green (product) INVALID" 2 \
	"$gate" --analyse "$tmp/nodisarm.log" --pid "$pid" --expect green --oracle product

make_fixed "$tmp/unowned.log" \
	"[3.0](plane-call-unowned, Error) plane-call-unowned call=16 pid=$pid seq=7 status=0"
check "fixed delta with an unowned completion -> green (product) FAIL" 1 \
	"$gate" --analyse "$tmp/unowned.log" --pid "$pid" --expect green --oracle product

: > "$tmp/empty.log"
check "empty delta -> red FAIL (never PASS)" 1 \
	"$gate" --analyse "$tmp/empty.log" --pid "$pid" --expect red --oracle existing
check "empty delta -> green FAIL (never PASS)" 1 \
	"$gate" --analyse "$tmp/empty.log" --pid "$pid" --expect green --oracle existing

# 3. the fixed delta must not pass as RED
check "fixed delta -> red must FAIL" 1 \
	"$gate" --analyse "$tmp/fixed.log" --pid "$pid" --expect red --oracle existing

if [ "$fail" -ne 0 ]; then
	echo "ORACLE-SELFTEST: VERDICT FAIL"
	exit 1
fi
echo "ORACLE-SELFTEST: VERDICT PASS"
