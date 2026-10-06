#!/usr/bin/env bash
# dar-dles focused guest runtime gate: bounded management-plane progress under continuous ring load.
#
# The defect (dar-dles): the server's ring spin phase drained rings and then did
# `if (serviced > 0) continue;`, so the work quantum was consulted only when a drain served
# NOTHING. `_drainRings()` has no internal bound (it walks every attached ring thread and runs
# the full Call path per thread), so under continuous ring load the outer loop body -- which
# services the process-control (management) plane and the deferred plane calls -- was never
# reached. With ~50 simultaneously live guest threads the plane scan collapsed to a few scans
# per second, a published management request (op=31 THREAD_SELF_BOOTSTRAP / op=3 ATTACH_LANE)
# waited seconds behind op=7 cancellation traffic, exhausted its bounded retries, and the
# pthread_create that published it never returned.
#
# What this gate measures: N guest pthreads created sequentially and held simultaneously live
# on one release barrier, with each live thread parked at a cancellation point (continuous
# management-plane traffic). Bounded progress requires every pthread_create to RETURN, every
# worker to START, and every worker to JOIN, at every requested N. RED: a server that skips the
# quantum while ring work keeps arriving leaves at least one pthread_create waiting, so the
# `RING_MACH_TEST mode=pthread_live ... pass=1` line never appears (the mode line reports the
# attempt that stopped) and the harness verdict is FAIL. The measured RED arm is recorded in
# dar-dles; this script is the behavioral oracle, not a source-text check.
#
# MEASURED (current product, darlingserver a384108): n=32 PASS; n=64 FAIL - and the failure is a
# SERVER PANIC, not plane starvation: "Lock assertion failed (not owned but expected to be owned)"
# at duct-tape/src/locks.c:48, reached from Thread::microthreadWorker -> Call::SemaphoreTimedwait
# -> semaphore_wait_internal -> waitq_unlock -> lck_mtx_unlock -> dtape_mutex_unlock. dar-dles
# stays OPEN for that. At 32 the gate is the plane-fairness regression oracle and passes.
#
# It does NOT build the product and does NOT compile in the guest: the fixture is a guest Mach-O
# built by the Darling build's own cross-toolchain, and the prefix only executes it.
#
# Inputs (required):
#   DPREFIX / DARLING_PREFIX or --prefix   a booted prefix whose darlingserver is under test
#   --fixture PATH (default <prefix>/private/var/tmp/ring_mach_msg_test)
# Optional:
#   --counts "32 64"     simultaneously live thread counts to gate (default: 32 64)
#   --stack-kib 256      per-thread stack size passed to the fixture
#   --repeats 1          repetitions per count
#   --wait 60            harness bound per run, in seconds
#   --workspace PATH     workspace root holding scripts/darling-boot-run.sh (default: this repo)
set -uo pipefail

workspace_root="$(cd "$(dirname "$0")/.." && pwd)"
prefix="${DPREFIX:-${DARLING_PREFIX:-}}"
fixture=""
counts="32 64"
stack_kib=256
repeats=1
wait_seconds=60

while [ $# -gt 0 ]; do
	case "$1" in
	--prefix) prefix="$2"; shift 2 ;;
	--fixture) fixture="$2"; shift 2 ;;
	--counts) counts="$2"; shift 2 ;;
	--stack-kib) stack_kib="$2"; shift 2 ;;
	--repeats) repeats="$2"; shift 2 ;;
	--wait) wait_seconds="$2"; shift 2 ;;
	--workspace) workspace_root="$2"; shift 2 ;;
	*)
		echo "dar-dles-plane-fairness: unknown argument: $1" >&2
		exit 2
		;;
	esac
done

boot_run="$workspace_root/scripts/darling-boot-run.sh"
[ -f "$boot_run" ] || {
	echo "dar-dles-plane-fairness: harness not found: $boot_run" >&2
	exit 2
}
[ -n "$prefix" ] && [ -x "$prefix/bin/darling" ] || {
	echo "dar-dles-plane-fairness: --prefix/DPREFIX must name a booted Darling prefix" >&2
	exit 2
}
[ -n "$fixture" ] || fixture="$prefix/private/var/tmp/ring_mach_msg_test"
[ -x "$fixture" ] || {
	echo "dar-dles-plane-fairness: fixture not found or not executable: $fixture" >&2
	echo "  build it with: (cd BUILD_DIR && ninja ring_mach_msg_test) && install -m755 BUILD_DIR/src/tools/ring_mach_msg_test <prefix>/private/var/tmp/" >&2
	exit 2
}

failures=0

# The command runs INSIDE the guest, so the fixture must be named by its guest path; the host
# prefix directory is only what this script verifies. dar-dles' measured runs used
# /private/var/tmp/ring_mach_msg_test (an emulated Darwin pid is not a host pid, and /usr/bin
# resolves to the HOST's /usr/bin, so a fixture deployed elsewhere is invisible to the guest).
fixture_guest="/private/var/tmp/$(basename "$fixture")"

# One bounded run at a fixed N. Deterministic: the verdict is the workload's own mode line, the
# script never sleeps and never retries a stall. The guest path is the fixture's own deployment
# path, because /usr/bin resolves to the host's /usr/bin inside the guest.
run_one() {
	local n="$1" tag="$2"
	local out log verdict all created started joined pass rc=0
	out="$(bash "$boot_run" --prefix "$prefix" --wait "$wait_seconds" \
		--marker 'RING_MACH_TEST mode=pthread_live' --marker 'pass=1' \
		--cmd "exec $fixture_guest pthread_live $n $stack_kib" 2>&1)" || rc=$?
	log="$(printf '%s\n' "$out" | sed -n 's/^log: //p' | tail -1)"
	verdict=FAIL
	if printf '%s\n' "$out" | grep -q 'VERDICT: PASS'; then verdict=PASS; fi
	all=""; [ -n "$log" ] && all="$(grep -h 'PTHREAD_LIVE_ALL' "$log" | tail -1)"
	created="$(printf '%s' "$all" | sed -n 's/.*created=\([0-9]*\).*/\1/p')"
	started="$(printf '%s' "$all" | sed -n 's/.*started=\([0-9]*\).*/\1/p')"
	joined=""; pass=""
	if [ -n "$log" ]; then
		local mode_line
		mode_line="$(grep -h "RING_MACH_TEST mode=pthread_live n=$n " "$log" | tail -1)"
		joined="$(printf '%s' "$mode_line" | sed -n 's/.*joined=\([0-9]*\).*/\1/p')"
		pass="$(printf '%s' "$mode_line" | sed -n 's/.*pass=\([0-9-]*\).*/\1/p')"
	fi
	local ok=1
	[ "$verdict" = PASS ] || ok=0
	[ "$created" = "$n" ] || ok=0
	[ "$started" = "$n" ] || ok=0
	[ "$joined" = "$n" ] || ok=0
	[ "$pass" = "1" ] || ok=0
	printf 'RESULT n=%s %s verdict=%s rc=%s created=%s started=%s joined=%s pass=%s ok=%s log=%s\n' \
		"$n" "$tag" "$verdict" "$rc" "${created:-?}" "${started:-?}" "${joined:-?}" "${pass:-?}" "$ok" "${log:-none}"
	[ "$ok" = 1 ] || failures=$((failures + 1))
}

for n in $counts; do
	for a in $(seq 1 "$repeats"); do
		run_one "$n" "repeat$a"
	done
done

if [ "$failures" -ne 0 ]; then
	echo "dar-dles-plane-fairness: FAIL ($failures run(s) made no bounded progress)"
	exit 1
fi
echo "dar-dles-plane-fairness: PASS (every requested create returned, every worker started and joined)"
