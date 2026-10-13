#!/bin/sh
# run-sem-site-attribution-oracle.sh -- WHICH PROCESS'S STREAM CARRIES THE 30-SECOND SEMAPHORE WAIT.
#
# WHY THIS EXISTS (measured 2026-10-13, bead dar-liaq). The Xcode load-time ladder's wall was read for
# several runs as "the parked XPC reply depends on the 30-second semaphore handshake", and the handshake
# itself was attributed to the workload because the investigator expected it there. The deployed
# DARLING_SEM_SITE_TRACE probe had printed the line in the RUN LOG -- the harness log, which carries the
# loader's and LAUNCHD's share -- while the workload's own stream (`$PREFIX/private/var/tmp/
# xcodebuild-<workload>.out`, fd 1/2 of the workload) carries only a name-0 `semaphore_signal`. The two
# streams are different files, and a line read from the wrong one produced an inference (the reply path
# waits on launchd's 30 s poll) that a phase measurement then refuted: the workload's own step clock and
# launchd's `sleep(30)` are independent (they drift apart monotonically, ~0.5 s over 30 cycles).
#
# This is the same defect class the diagnostics skill records as "read the workload's OWN stream": an
# in-path instrument writes to the fd of the process it runs in, and the harness log is the PREFIX's log,
# not the workload's. The oracle below is the mechanical form of that rule for this ladder: given a
# (run-log, workload-stream) pair it must NAME the stream that holds the 30 s semaphore wait.
#
# THE ARMS (self-contained: the RED arm executes the same decision logic on a mis-attributed pair):
#   RED   -- the workload's own stream carries the 30 s timedwait (the wrong attribution this project
#            actually made). The oracle must answer `workload`, i.e. report the attribution as coming
#            from the workload, and this script requires that answer: a reader who stops here has been
#            given the bad arm's verdict, not a pass.
#   GREEN -- the measured pair: the 30 s timedwait is in the run log (launchd's update thread,
#            launchd.c:458-468 `sync(); sleep(launchd_sync_frequency)`, 30 at launchd.c:118) and the
#            workload's own stream has no semaphore wait at all. The oracle must answer `launchd`.
#   RECORDED -- when the recorded ladder evidence is present, the same logic runs on it and says so; when
#            it is absent the skip is PRINTED rather than passed over in silence.
#
# Usage: tests/regression/run-sem-site-attribution-oracle.sh
# Prints SEM_SITE_ATTRIBUTION_ORACLE_OK on success.
set -u

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)

fail() { echo "SEM-SITE-ATTRIBUTION-ORACLE: FAIL: $*" >&2; exit 1; }

# ---------------------------------------------------------------------------------------------------
# THE DECISION LOGIC. One question: which stream holds a SEM-SITE line whose operation is `timedwait`?
# A `timedwait` SEM-SITE line is the only shape that can be the 30-second semaphore wait; `op=signal`
# lines (including the benign name-0 pthread-exit signal) are NOT semaphore waits and must not be counted.
# Prints the stream name and the count: "<run-log|workload|none> <count>".
# ---------------------------------------------------------------------------------------------------
sem_site_timedwait_stream() {
	run_log=$1
	workload_stream=$2
	[ -f "$run_log" ] || fail "no run log at $run_log"
	[ -f "$workload_stream" ] || fail "no workload stream at $workload_stream"

	# grep -a: the streams carry guest writes and binary fragments; -c: one number, no pager.
	n_log=$(grep -ac 'SEM-SITE .* op=timedwait' "$run_log" 2>/dev/null || true)
	n_wl=$(grep -ac 'SEM-SITE .* op=timedwait' "$workload_stream" 2>/dev/null || true)
	[ -n "$n_log" ] || n_log=0
	[ -n "$n_wl" ] || n_wl=0

	if [ "$n_wl" -gt 0 ]; then
		echo "workload $n_wl"
	elif [ "$n_log" -gt 0 ]; then
		echo "run-log $n_log"
	else
		echo "none 0"
	fi
}

echo "== RED arm: the mis-attributed pair (the workload's own stream carries the wait) =="
red_dir=$(mktemp -d "${TMPDIR:-/tmp}/sem-site-oracle-red.XXXXXX") || fail "mktemp"
trap 'rm -rf "$red_dir"' EXIT INT TERM
cat >"$red_dir/run.log" <<'EOF'
SEM-SITE tcb=0x7E07AAAFB6C0 op=signal a=0x0 b=0x0 ra=0x11 sym0=semaphore_signal_trap_impl
EOF
cat >"$red_dir/workload.out" <<'EOF'
SEM-SITE tcb=0x715DF6BFF6C0 op=signal a=0x0 b=0x0 ra=0x22 sym0=semaphore_signal_trap_impl
SEM-SITE tcb=0x7E07AAAFB6C0 op=timedwait a=0x903 b=0x1E ra=0x7E07AB74DB03 sym0=semaphore_timedwait_trap_impl sym1=_darling_mach_syscall sym2=sys_semwait_signal sym3=_darling_bsd_syscall
EOF
red=$(sem_site_timedwait_stream "$red_dir/run.log" "$red_dir/workload.out")
red_where=${red%% *}
[ "$red_where" = "workload" ] || fail "RED arm: oracle answered '$red', expected the workload stream"
echo "   RED verdict: $red  (the 30 s wait reads as the workload's -- the wrong attribution)"
echo "(RED arm mis-attributed the wait to the workload as expected)"

echo "== GREEN arm: the measured pair (the wait is in the run log / launchd) =="
green_dir=$(mktemp -d "${TMPDIR:-/tmp}/sem-site-oracle-green.XXXXXX") || fail "mktemp"
cat >"$green_dir/run.log" <<'EOF'
SEM-SITE tcb=0x7CFD977FF6C0 op=timedwait a=0x903 b=0x1E ra=0x7CFD99582B03 sym0=semaphore_timedwait_trap_impl ra2=0x7CFD99581C90 sym1=_darling_mach_syscall ra3=0x7CFD995789D3 sym2=sys_semwait_signal ra4=0x7CFD9956184E sym3=_darling_bsd_syscall
[MACHMSG_TRANSPORT process=1123245 image=kernel host_tid=1123245 lane=118 seq=6 direction=request transport=RING callnum=38]
EOF
cat >"$green_dir/workload.out" <<'EOF'
SEM-SITE tcb=0x715DF6BFF6C0 op=signal a=0x0 b=0x0 ra=0x715DF8D404BC sym0=semaphore_signal_trap_impl sym1=sys_bsdthread_terminate sym2=_darling_bsd_syscall sym3=_pthread_terminate_invoke
[dthread-entry-tid=1132614@/Volumes/SystemRoot/home/ilyagulya/work/wr-fresh/prefi]
EOF
green=$(sem_site_timedwait_stream "$green_dir/run.log" "$green_dir/workload.out")
green_where=${green%% *}
[ "$green_where" = "run-log" ] || fail "GREEN arm: oracle answered '$green', expected the run log"
# The signal line must NOT be read as a wait: that confusion is what the name-0 convention costs.
[ "$(grep -ac 'SEM-SITE .* op=timedwait' "$green_dir/workload.out")" = 0 ] ||
	fail "GREEN arm: the workload stream was read as carrying a semaphore WAIT"
echo "   GREEN verdict: $green  (launchd's update thread, not the workload)"

echo "== RECORDED arm: the evidence this oracle was written from =="
rec_log="$REPO_ROOT/evidence/sync-reply-hang-diagnosis/ladder-long/minproj.log"
rec_wl="$REPO_ROOT/evidence/sync-reply-hang-diagnosis/ladder-long/minproj-own-stream.out"
if [ -f "$rec_log" ] && [ -f "$rec_wl" ]; then
	rec=$(sem_site_timedwait_stream "$rec_log" "$rec_wl")
	rec_where=${rec%% *}
	[ "$rec_where" = "run-log" ] || fail "RECORDED arm: oracle answered '$rec', expected the run log"
	echo "   RECORDED verdict: $rec (minproj, 40 min wall, prefix-verify14)"
else
	echo "   RECORDED arm SKIPPED: $rec_log or $rec_wl is absent; nothing was checked by this arm"
fi

echo "SEM_SITE_ATTRIBUTION_ORACLE_OK"
exit 0
