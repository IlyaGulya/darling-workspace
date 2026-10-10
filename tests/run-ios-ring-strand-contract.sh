#!/usr/bin/env bash
# Bounded, Xcode-free reproducer and gate for the T4 "invisible parking" strand.
#
# One command, two arms of the SAME fixture (tests/ios_ring_strand_guest.c), because the defect is a
# TRANSPORT question and only a comparison answers it:
#
#   arm=product    the transport the product selects. The blocking family must ride the ring lane, so
#                  the waits must land on the server-owned queue: this arm passes only on
#                  depth_contract=ok host_futex_wait=0. A silent fall back to the host futex
#                  (host_futex_wait=1, depth_contract=violated) FAILS this arm -- that fallback is the
#                  RED the fixture exists to report, not a tolerated state. MEASURED on the deployed
#                  runtime 2026-10-10: the product arm shows the futex signature.
#   arm=datagram   DARLING_GUEST_RING_BLOCKING=0, the tree's own A/B opt-out for the blocking family
#                  (dserver-ring.c:3345-3368): the caller takes the pre-migration per-thread datagram,
#                  mach_driver_get_fd declines it unconditionally, the RPC returns negative and
#                  sys_ulock_wait parks in the host futex -- the state the server cannot see. The
#                  parking is this arm's EXPECTED behaviour, so it passes on host_futex_wait=1 (with
#                  every waiter returned) and cannot pass without reproducing the parking.
#
# Bounded on both ends: the fixture's own watchdog prints `stranded=N` and exits after
# IOS_RING_STRAND_DEADLINE_MS (default 5000) with no progress, and the harness `--wait`
# (IOS_RING_STRAND_WAIT, default 25) is the outer bound. The harness stops as soon as the fixture's
# own `pass=` line appears, so a healthy arm costs seconds, not the whole --wait.
#
# Mechanism reused, nothing new: tests/run-ios-unfair-lock-contract.sh builds the fixture by
# replaying the build's recorded guest Mach-O commands, installs it into the prefix and boots it
# through scripts/darling-boot-run.sh (see tests/run-ios-call-retval-contract.sh for the same
# narrowing pattern).
#
# Inputs (required): DARLING_BUILD_DIR (a configured product build), DPREFIX/DARLING_PREFIX (a
# booted prefix whose libsystem_kernel + darlingserver are under test).
# Exit: 0 only when both arms' FIXTURE verdict is `pass=1` AND the harness's own end-of-run verdict is
# PASS. The two are reported separately (`fixture=` vs `harness=`) because the harness also fails on
# its own prefix-hygiene check -- an unclean prefix fails the harness while the fixture's transport
# verdict still stands, and conflating them hides which one needs fixing.
set -uo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
runner="$here/run-ios-unfair-lock-contract.sh"
fixture="$here/ios_ring_strand_guest.c"
wait_s="${IOS_RING_STRAND_WAIT:-25}"
deadline_ms="${IOS_RING_STRAND_DEADLINE_MS:-5000}"
# DIAGNOSTIC ONLY (default off): pass through to the harness's --freeze-on-fail, which leaves the
# failing incarnation (guest threads in their host futex, server idle) ALIVE for an external
# observer. Set IOS_RING_STRAND_FREEZE_ON_FAIL=1 and inspect /proc/<pid>/wchan, LLDB, or the
# server's own thread states; the harness prints the cleanup command when it freezes. The runner's
# log lives in its workdir, so add DARLING_KEEP_WORKDIR=1 to retain the fixture's lines.
export IOS_UNFAIR_LOCK_FREEZE_ON_FAIL="${IOS_RING_STRAND_FREEZE_ON_FAIL:-0}"

# The fixture's verdict line carries its own fields (the harness console also prints
# `MARKER ok IOS-UNFAIR-LOCK pass=1`, so the pattern must not accept that form).
verdict() { grep -aE 'IOS-UNFAIR-LOCK pass=[01] (waiters=|stranded=)' "$1" | tail -1; }
parking() { grep -a 'IOS-UNFAIR-LOCK ulock-park returns=' "$1" | tail -1; }
fixture_state() {
	if printf '%s' "$1" | grep -q 'pass=1'; then printf 'pass'
	elif printf '%s' "$1" | grep -q 'pass=0'; then printf 'fail'
	else printf 'missing'; fi
}

product_out="$(mktemp)"
datagram_out="$(mktemp)"
trap 'rm -f "$product_out" "$datagram_out"' EXIT

# The fixture's own result line is the poll line: the harness then stops on it instead of sleeping
# out --wait. The guest knob is set on the guest leaf command, exactly like the census runner sets
# DARLING_GUEST_LANE_STATS (tests/lane_lifecycle_census/run-lane-lifecycle-census.sh:589).
IOS_UNFAIR_LOCK_FIXTURE="$fixture" \
IOS_UNFAIR_LOCK_WAIT="$wait_s" \
IOS_UNFAIR_LOCK_POLL='IOS-UNFAIR-LOCK pass=' \
IOS_UNFAIR_LOCK_GUEST_ENV="IOS_RING_STRAND_DEADLINE_MS=$deadline_ms" \
	bash "$runner" "$@" >"$product_out" 2>&1
product_rc=$?

# Under --freeze-on-fail the failing incarnation is deliberately left alive; starting the second arm
# would make the harness's own clean-start tear it down. Stop here and let the caller inspect it.
if [ "$product_rc" -ne 0 ] && [ "$IOS_UNFAIR_LOCK_FREEZE_ON_FAIL" = 1 ]; then
	pline="$(verdict "$product_out")"
	echo "IOS-RING-STRAND arm=product fixture=$(fixture_state "$pline") harness=frozen ${pline:-verdict-missing}"
	echo "IOS-RING-STRAND RESULT pass=0 frozen=product (the datagram arm was skipped so the frozen incarnation survives; clean up as the harness instructed)"
	exit 1
fi

# The ulock stage alone: the psynch arm does not depend on the datagram knob, and leaving it in
# would mix its verdict into this arm.
IOS_UNFAIR_LOCK_FIXTURE="$fixture" \
IOS_UNFAIR_LOCK_WAIT="$wait_s" \
IOS_UNFAIR_LOCK_POLL='IOS-UNFAIR-LOCK pass=' \
IOS_UNFAIR_LOCK_GUEST_ENV="IOS_RING_STRAND_DEADLINE_MS=$deadline_ms DARLING_GUEST_RING_BLOCKING=0 IOS_RING_STRAND_ARMS=ulock" \
	bash "$runner" "$@" >"$datagram_out" 2>&1

# The harness's own end-of-run verdict (marker match + prefix hygiene) is reported separately from
# the fixture's transport verdict: an unclean prefix fails the harness while the fixture's verdict
# still stands, and conflating the two hides which one needs fixing.
harness_verdict() { grep -ao 'VERDICT: [A-Z]*' "$1" | tail -1; }

fixture_fails=0
harness_fails=0
for arm in product datagram; do
	case "$arm" in
		product) out="$product_out" ;;
		datagram) out="$datagram_out" ;;
	esac
	line="$(verdict "$out")"
	fixture="$(fixture_state "$line")"
	hv="$(harness_verdict "$out")"
	[ -n "$hv" ] || hv='VERDICT: ?'
	printf 'IOS-RING-STRAND arm=%s fixture=%s harness=%s %s\n' \
		"$arm" "$fixture" "${hv#VERDICT: }" "${line:-verdict-missing}"
	obs="$(parking "$out")"
	[ -n "$obs" ] && printf 'IOS-RING-STRAND arm=%s %s\n' "$arm" "$obs"
	[ "$fixture" = pass ] || fixture_fails=$((fixture_fails + 1))
	printf '%s' "$hv" | grep -q PASS || harness_fails=$((harness_fails + 1))
	if [ "$fixture" != pass ]; then
		echo "IOS-RING-STRAND arm=$arm: the fixture's own transport verdict is not pass; runner output:"
		tail -25 "$out" | cut -c1-200
	elif printf '%s' "$hv" | grep -q FAIL; then
		echo "IOS-RING-STRAND arm=$arm: fixture=pass but the harness reported '$hv' -- its own marker/prefix-hygiene check (look for 'cleanup FAILED' and the mount count in the log), not the fixture's verdict"
	fi
done

fail=0
[ "$fixture_fails" -eq 0 ] || fail=1
[ "$harness_fails" -eq 0 ] || fail=1
echo "IOS-RING-STRAND RESULT pass=$((1 - fail)) fixture_failures=$fixture_fails harness_failures=$harness_fails"
exit "$fail"
