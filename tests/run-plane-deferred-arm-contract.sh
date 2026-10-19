#!/usr/bin/env bash
# run-plane-deferred-arm-contract.sh -- RED/GREEN gate for the management-plane
# "refusal without disarm" defect (Thread::dispatchOrQueuePlaneCall).
#
# WHAT IT PROVES
#   A non-deferrable, process-scoped plane op (CONSOLE_OPEN, callnum 16) is
#   published by a worker thread while the process's MAIN thread is parked in a
#   blocking Mach receive (callnum 38). The plane stamps the synthesized Call
#   with `header.tid = pid`, `Call::callFromMessage` arms it on the parked
#   thread, and `dispatchOrQueuePlaneCall(..., queueIfBusy=false)` returns false
#   WITHOUT disarming it. The resulting server-side lifecycle is the defect:
#
#     call-deferred site=setPendingCall-parked ... number=16 ... suspended=1
#     setPendingCall rejected call (number 16)          <- re-publication meets the slot
#     call-deferred site=doWork-parked ... number=16 ... <- STILL armed at the next dispatch
#
#   and the abandoned Call finishes with no completion owner, so the plane
#   request is never completed (the guest falls back to the datagram) and the
#   operation is applied a second time when the deferred Call finally runs.
#
# ARMS
#   control -- the main thread spins (not parked); both opens are served
#              inline and must succeed. This separates "the defect" from
#              "/dev/console semantics on this prefix".
#   park    -- the main thread is parked; RED asserts the lifecycle above,
#              GREEN asserts the fixed behaviour (no arm-on-parked record, no
#              rejection, the plane request completes on its first publication).
#
# USAGE
#   run-plane-deferred-arm-contract.sh --prefix P --build-dir B [--expect red|green]
#                                      [--fixture F] [--run-dir D] [--wait S]
#
#   The fixture is a guest Mach-O. With --build-dir, this script extracts the
#   exact compile and link commands of the build's own `ring_mach_msg_test`
#   target from `ninja -t commands` and substitutes this test's source. That
#   keeps the recipe reproducible WITHOUT adding a target to the product build
#   (product sources are not modified by this gate).
#
# PRECONDITIONS
#   * A private prefix with a deployed darlingserver that carries the
#     `call-deferred` instrument (the RED signature requires it).
#   * No other run against that prefix. Use an isolation lane for concurrent
#     work (`west dev lane create NAME --base PREFIX`, then
#     `west dev lane exec NAME -- <this script> ...`).
#   * This gate never deploys; it boots the prefix, runs, and cleans up.

set -uo pipefail

prefix=""
build_dir=""
fixture=""
run_dir=""
expect="red"
wait_seconds=90
name="plane_deferred_arm_guest"

while [ $# -gt 0 ]; do
	case "$1" in
	--prefix) prefix="$2"; shift 2 ;;
	--build-dir) build_dir="$2"; shift 2 ;;
	--fixture) fixture="$2"; shift 2 ;;
	--run-dir) run_dir="$2"; shift 2 ;;
	--expect) expect="$2"; shift 2 ;;
	--wait) wait_seconds="$2"; shift 2 ;;
	-h|--help) sed -n '2,45p' "$0"; exit 0 ;;
	*) echo "plane-deferred-arm: unknown argument: $1" >&2; exit 2 ;;
	esac
done

here="$(cd "$(dirname "$0")" && pwd)"
workspace="$(cd "$here/.." && pwd)"
boot_run="$workspace/scripts/darling-boot-run.sh"
src="$here/plane_deferred_arm_guest.c"

[ -x "$boot_run" ] || { echo "plane-deferred-arm: missing $boot_run" >&2; exit 2; }
[ -n "$prefix" ] && [ -x "$prefix/bin/darling" ] || {
	echo "plane-deferred-arm: --prefix must name a booted Darling prefix" >&2; exit 2; }
[ -f "$src" ] || { echo "plane-deferred-arm: missing fixture source $src" >&2; exit 2; }
[ -n "$run_dir" ] || run_dir="$(mktemp -d "${TMPDIR:-/tmp}/plane-deferred-arm.XXXXXX")"
mkdir -p "$run_dir"

case "$expect" in red|green) ;; *) echo "plane-deferred-arm: --expect red|green" >&2; exit 2 ;; esac

# ---------------------------------------------------------------------------
# 1. Build the guest fixture without touching the product build graph.
# ---------------------------------------------------------------------------
if [ -z "$fixture" ]; then
	if [ -z "$build_dir" ]; then
		echo "plane-deferred-arm: need --fixture or --build-dir" >&2; exit 2
	fi
	[ -f "$build_dir/build.ninja" ] || { echo "plane-deferred-arm: $build_dir is not a ninja build dir" >&2; exit 2; }
	obj="$run_dir/$name.o"
	out="$run_dir/$name"
	cc_line="$(ninja -C "$build_dir" -t commands ring_mach_msg_test 2>/dev/null | grep 'ring_mach_msg_test.c.o' | head -1)"
	link_line="$(ninja -C "$build_dir" -t commands ring_mach_msg_test 2>/dev/null | tail -1)"
	[ -n "$cc_line" ] && [ -n "$link_line" ] || {
		echo "plane-deferred-arm: could not extract the guest toolchain commands from $build_dir" >&2; exit 2; }
	cc_new="$(printf '%s' "$cc_line" \
		| sed -e 's#-MD -MT [^ ]*\.o -MF [^ ]*\.o\.d ##' \
		      -e "s#-o [^ ]*ring_mach_msg_test\.c\.o#-o $obj#" \
		      -e "s#-c .*ring_mach_msg_test\.c#-c $src#")"
	link_new="$(printf '%s' "$link_line" \
		| sed -e "s#[^ ]*ring_mach_msg_test\.c\.o#$obj#" \
		      -e "s#-o [^ ]*ring_mach_msg_test#-o $out#")"
	( cd "$build_dir" && eval "$cc_new" ) || { echo "plane-deferred-arm: compile failed" >&2; exit 2; }
	( cd "$build_dir" && eval "$link_new" ) || { echo "plane-deferred-arm: link failed" >&2; exit 2; }
	fixture="$out"
fi
[ -x "$fixture" ] || { echo "plane-deferred-arm: fixture not executable: $fixture" >&2; exit 2; }

guest_path="/private/var/tmp/$name"
install -m755 "$fixture" "$prefix$guest_path"

# ---------------------------------------------------------------------------
# 2. Run one arm. The harness owns the boot/cleanup and writes a unique log.
# ---------------------------------------------------------------------------
run_arm() {
	local mode="$1"
	local log="$run_dir/$mode.log"
	local bundle="$log.bundle"
	"$boot_run" --prefix "$prefix" --wait "$wait_seconds" --marker 'PDA result' \
		--poll 'PDA end' --log "$log" --bundle "$bundle" --cmd "$guest_path $mode" \
		> "$run_dir/$mode.harness" 2>&1
	local rc=$?
	echo "$rc" > "$run_dir/$mode.rc"
	echo "$log" > "$run_dir/$mode.logpath"
	return $rc
}

# ---------------------------------------------------------------------------
# 3. Helpers.
# ---------------------------------------------------------------------------
guest_line() { # <mode> <field-regex>
	grep -aE "$2" "$run_dir/$1.log" 2>/dev/null | tail -1
}

run_pid() { # <mode>
	sed -n 's/.*PDA begin mode=[a-z]* pid=\([0-9]*\).*/\1/p' "$run_dir/$1.log" 2>/dev/null | tail -1
}

server_delta() { # <mode>  -> the server rows this run appended, pid-filtered when possible
	local mode="$1" bundle="$run_dir/$1.log.bundle" pid
	pid="$(run_pid "$mode")"
	[ -f "$bundle/server.pre.log" ] && [ -f "$bundle/server.post.log" ] || return 1
	local pre
	pre="$(wc -l < "$bundle/server.pre.log")"
	tail -n "+$((pre + 1))" "$bundle/server.post.log" > "$run_dir/$mode.server-delta.log"
	if [ -n "$pid" ]; then
		grep -a -- "$pid" "$run_dir/$mode.server-delta.log" > "$run_dir/$mode.server-pid.log" || true
	fi
	return 0
}

verdict_fail() { echo "PLANE-DEFERRED-ARM: FAIL: $*" >&2; FAILED=1; }
verdict_ok()   { echo "PLANE-DEFERRED-ARM: ok: $*"; }

FAILED=0

# ---------------------------------------------------------------------------
# 4. Control arm: the opens must succeed without a parked main thread.
# ---------------------------------------------------------------------------
run_arm control || true
ctl="$(guest_line control 'PDA result mode=control')"
if [ -n "$ctl" ] && printf '%s' "$ctl" | grep -q 'verdict=both-open'; then
	verdict_ok "control arm: $ctl"
else
	verdict_fail "control arm did not report both opens succeeding: ${ctl:-<no result line>}"
fi

# ---------------------------------------------------------------------------
# 5. Park arm: the RED/GREEN signature.
# ---------------------------------------------------------------------------
run_arm park || true
park="$(guest_line park 'PDA result mode=park')"
[ -n "$park" ] && verdict_ok "park arm guest: $park" \
	|| verdict_fail "park arm produced no result line (the workload did not complete)"

server_delta park || verdict_fail "park arm has no server pre/post log pair in its bundle"
delta="$run_dir/park.server-delta.log"
[ -f "$run_dir/park.server-pid.log" ] && delta="$run_dir/park.server-pid.log"
[ -f "$delta" ] || delta=/dev/null

# The rejection record (`call.cpp` callLog.error) carries the call number but
# NOT the pid, so it is counted from the run's whole server delta (which is
# already scoped to this run) rather than from the pid-filtered subset.
armed_hits=$(grep -acE 'call-deferred site=setPendingCall-parked .*number=16 .*suspended=1' "$delta" 2>/dev/null || true)
reject_hits=$(grep -acE 'setPendingCall rejected call \(number 16\)' "$run_dir/park.server-delta.log" 2>/dev/null || true)
defer_hits=$(grep -acE 'call-deferred site=doWork-parked .*number=16' "$delta" 2>/dev/null || true)
unowned_hits=$(grep -acE 'plane-deferred-completion' "$run_dir/park.server-delta.log" 2>/dev/null || true)
# the retained slot is the load-bearing fact: at least one publication found
# the slot occupied (`pending=1`)
retained_hits=$(grep -acE 'call-deferred site=setPendingCall-parked .*number=16 .*pending=1' "$delta" 2>/dev/null || true)

echo "PLANE-DEFERRED-ARM: trace armed=$armed_hits retained=$retained_hits rejected=$reject_hits deferred=$defer_hits unowned_completions=$unowned_hits"

if [ "$expect" = red ]; then
	[ "${armed_hits:-0}" -ge 1 ] || verdict_fail "no arm-on-parked record for CONSOLE_OPEN (callnum 16) in the server delta"
	[ "${retained_hits:-0}" -ge 1 ] || verdict_fail "no publication saw the slot occupied (pending=1): the armed Call was not retained"
	[ "${reject_hits:-0}" -ge 1 ] || verdict_fail "no setPendingCall rejection for callnum 16: the re-publication did not meet the retained slot"
	[ "${defer_hits:-0}" -ge 1 ] || verdict_fail "no doWork-parked record for callnum 16: the armed Call was not retained to the next dispatch"
	[ "${unowned_hits:-0}" -eq 0 ] || verdict_fail "a plane-deferred-completion record exists for this run: the abandoned Call had a completion owner"
else
	[ "${armed_hits:-0}" -eq 0 ] || verdict_fail "the fixed server still arms CONSOLE_OPEN on a parked thread"
	[ "${retained_hits:-0}" -eq 0 ] || verdict_fail "the fixed server still leaves a plane Call armed in a parked thread's pending slot"
	[ "${reject_hits:-0}" -eq 0 ] || verdict_fail "the fixed server still rejects a plane publication with the pending slot occupied"
	if [ -n "$park" ] && ! printf '%s' "$park" | grep -q 'verdict=both-open'; then
		verdict_fail "the fixed server did not complete both opens: $park"
	fi
fi

if [ "$FAILED" -ne 0 ]; then
	echo "PLANE-DEFERRED-ARM: VERDICT FAIL (expect=$expect, run-dir=$run_dir)"
	exit 1
fi
echo "PLANE-DEFERRED-ARM: VERDICT PASS (expect=$expect, run-dir=$run_dir)"
exit 0
