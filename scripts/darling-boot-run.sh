#!/bin/sh
# darling-boot-run.sh -- one measured boot run, with the hygiene that a measured run needs.
#
# WHY THIS EXISTS. Four separate time sinks in one investigation cycle came from running without a harness:
#
#   * launching while the previous run's server was still alive -- the reused server produced a ONE-LINE log and
#     the "measurement" drawn from it was worthless;
#   * stale guest processes accumulating because a cleanup matched /proc/<pid>/cmdline and a guest's cmdline is
#     the IN-GUEST path with no prefix in it (893 processes accumulated that way);
#   * two runs writing one log path, truncating each other;
#   * reading a diagnostic line as a verdict, when the verdict only exists after the workload's own completion.
#
# What it guarantees, in order:
#   1. a clean start: shutdown, then every prefix-owned process by /proc/<pid>/exe AND cmdline, then verify zero;
#   2. a UNIQUE log path per run (never shared, never appended);
#   3. the workload's real duration: the caller states how long the workload needs, and the harness waits it;
#   4. a single VERDICT block at the end: the markers the caller asked for, plus the counters that matter here
#      (per-thread RPC socket creations, denials, urgent timeouts, courier misses);
#   5. cleanup afterwards, and a non-zero exit if the prefix is not clean.
#
# Usage:
#   darling-boot-run.sh --prefix PATH --wait SECONDS [--marker NAME]... [--hatch] [--cmd 'shell command']
# Exit: 0 if every --marker appeared in the log, 1 otherwise (or if cleanup failed).

set -u

PREFIX=""
WAIT=""
CMD="echo HELLO=1; echo FINAL=1"
MARKERS="HELLO=1 FINAL=1"
HATCH=0
LOG=""

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--wait) WAIT="$2"; shift 2 ;;
		--marker) MARKERS="$MARKERS $2"; shift 2 ;;
		--cmd) CMD="$2"; shift 2 ;;
		--hatch) HATCH=1; shift ;;
		--log) LOG="$2"; shift 2 ;;
		-h|--help) sed -n '2,22p' "$0"; exit 0 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done

[ -n "$PREFIX" ] && [ -n "$WAIT" ] || { echo "usage: $0 --prefix PATH --wait SECONDS [--marker NAME]..." >&2; exit 2; }
[ -x "$PREFIX/bin/darling" ] || { echo "no launcher at $PREFIX/bin/darling" >&2; exit 2; }

[ -n "$LOG" ] || LOG="/tmp/darling-boot-$(date +%H%M%S)-$$.log"

# Enumerate prefix-owned processes. Two traps, both measured: a guest process's cmdline is the IN-GUEST path
# and contains no prefix (so exe is the arm that finds it), and THIS SCRIPT's own cmdline contains the prefix when
# it is invoked with --prefix <that prefix> (so the self and the parent must be excluded, or the harness kills
# itself -- the same class of defect as a probe that modifies what it measures).
SELF_NAME=${0##*/}

owned_pids() {
	self=$$
	parent=$PPID
	for d in /proc/[0-9]*; do
		pid=${d#/proc/}
		[ "$pid" = "$self" ] && continue
		[ "$pid" = "$parent" ] && continue
		exe=$(readlink "$d/exe" 2>/dev/null)
		case "$exe" in "$PREFIX"|"$PREFIX"/*) echo "$pid"; continue ;; esac
		cmd=$(tr "\0" " " < "$d/cmdline" 2>/dev/null)
		# The cmdline arm must NOT match this script or its pipeline subshells: they are invoked WITH --prefix,
		# so their own cmdline contains the prefix, and a `kill` loop then kills the very loop doing the killing
		# ("Killed" printed by the script itself). A subshell has a different PID but the same cmdline, so the
		# guard is on the script name, not on the PID.
		case "$cmd" in *"$SELF_NAME"*) continue ;; esac
		case "$cmd" in *"$PREFIX"*) echo "$pid" ;; esac
	done
}
count_owned() { owned_pids | wc -l; }

echo "== clean start =="
DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	"$PREFIX/bin/darling" --rootless shutdown >/dev/null 2>&1
sleep 3
# Kill, then SETTLE: a process that has just been signalled is still enumerable for a moment (and a zombie whose
# parent has not reaped it stays visible). Declaring failure on the first count produced a false failure the first
# time this script was used -- so re-check in bounded rounds instead of racing the kernel.
owned_pids | while read -r pid; do kill -9 "$pid" 2>/dev/null; done
left=1
i=0
while [ "$i" -lt 10 ]; do
	sleep 2
	left=$(count_owned)
	[ "$left" -eq 0 ] && break
	owned_pids | while read -r pid; do kill -9 "$pid" 2>/dev/null; done
	i=$((i + 1))
done
if [ "$left" -gt 0 ]; then
	echo "clean start FAILED: $left prefix-owned processes remain after $i settle rounds" >&2
	owned_pids | head -5 >&2
	exit 1
fi
echo "clean: 0 prefix-owned processes, starting"

echo "== run =="
echo "log: $LOG"
if [ "$HATCH" = 1 ]; then
	HATCH_ENV="DARLING_DISABLE_THREAD_RPC_UDS=1"
else
	HATCH_ENV=""
fi
: > "$LOG"
# shellcheck disable=SC2086
env DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	$HATCH_ENV \
	nohup timeout "$((WAIT + 60))" "$PREFIX/bin/darling" --rootless shell /bin/sh -c "$CMD" >> "$LOG" 2>&1 &

echo "waiting ${WAIT}s (the workload's own duration; diagnostic lines before this are not verdicts)"
sleep "$WAIT"

echo "== verdict =="
verdict=0
for m in $MARKERS; do
	if grep -q -- "$m" "$LOG"; then
		echo "MARKER ok   $m"
	else
		echo "MARKER MISS $m"
		verdict=1
	fi
done
echo "sockets created:   $(grep -c 'rpc-socket. created' "$LOG" 2>/dev/null)"
echo "socket denials:    $(grep -c 'rpc-socket-DENIED' "$LOG" 2>/dev/null)"
echo "urgent timeouts:   $(grep -c 'urgent-wait-TIMEOUT' "$LOG" 2>/dev/null)"
echo "courier misses:    $(grep -c 'fd-courier-recv. MISS' "$LOG" 2>/dev/null)"
echo "log lines:         $(wc -l < "$LOG")"

echo "== cleanup =="
DPREFIX="$PREFIX" DARLING_PREFIX="$PREFIX" DARLING_ROOTLESS=1 DARLING_NOOVERLAYFS=1 DARLING_EUNION=1 \
	"$PREFIX/bin/darling" --rootless shutdown >/dev/null 2>&1
sleep 3
owned_pids | while read -r pid; do kill -9 "$pid" 2>/dev/null; done
final=1
i=0
while [ "$i" -lt 10 ]; do
	sleep 2
	final=$(count_owned)
	[ "$final" -eq 0 ] && break
	owned_pids | while read -r pid; do kill -9 "$pid" 2>/dev/null; done
	i=$((i + 1))
done
mounts=$(mount 2>/dev/null | grep -c "$PREFIX")
echo "prefix processes after: $final, mounts: $mounts"
if [ "$final" -gt 0 ] || [ "$mounts" -gt 0 ]; then
	echo "cleanup FAILED" >&2
	verdict=1
fi

if [ "$verdict" = 0 ]; then
	echo "VERDICT: PASS"
else
	echo "VERDICT: FAIL"
fi
exit "$verdict"
