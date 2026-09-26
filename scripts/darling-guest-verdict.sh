#!/bin/sh
# darling-guest-verdict.sh -- run ONE guest workload mode and judge it by its OWN machine-readable line.
#
# WHY THIS EXISTS. The boot harness reports `VERDICT: PASS` when the MARKERS it was given appear anywhere in the
# log, and a caller who names a marker that matches the workload's START line ("[rmmt] start ... mode=sem_gap")
# gets PASS for a workload that never finished. MEASURED: `sem_gap 5000 1` and `basic 20` were reported PASS by
# exactly that mistake while their result lines were absent -- the workload HUNG. An acceptance that cannot
# distinguish "finished" from "started" is the "instrument that cannot answer" class, and this script is the fix:
#
#   the verdict is `RING_MACH_TEST mode=<mode> ... pass=1`, and its ABSENCE is FAIL/HANG, never PASS.
#
# Usage:
#   darling-guest-verdict.sh --prefix P --mode M [--args "A B"] [--wait SECONDS] [--env K=V]...
# Exit: 0 only when the result line says pass=1.
set -u

PREFIX=""
MODE=""
ARGS=""
WAIT=90
ENVS=""
SELF_DIR=$(dirname "$0")

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--mode)   MODE="$2"; shift 2 ;;
		--args)   ARGS="$2"; shift 2 ;;
		--wait)   WAIT="$2"; shift 2 ;;
		--env)    ENVS="$ENVS --env $2"; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ -n "$PREFIX" ] && [ -n "$MODE" ] || { echo "usage: $0 --prefix P --mode M [--args 'A B'] [--wait S] [--env K=V]..." >&2; exit 2; }

LOG="/tmp/guest-verdict-$$-${MODE}.log"
rm -f "$LOG"

# shellcheck disable=SC2086
"$SELF_DIR/darling-boot-run.sh" --prefix "$PREFIX" --wait "$WAIT" --log "$LOG" \
	$ENVS --cmd "/usr/bin/ring_mach_msg_test $MODE $ARGS" \
	--marker "__never_a_marker__" > "$LOG.runner" 2>&1

# The result line, and only the result line, is the verdict.
LINE=$(grep -a -o "RING_MACH_TEST mode=$MODE [^|]*" "$LOG" 2>/dev/null | head -1)
DENIED=$(grep -a -c "rpc-socket-DENIED" "$LOG" 2>/dev/null)
CREATED=$(grep -a -c "rpc-socket\] created" "$LOG" 2>/dev/null)
STARTED=$(grep -a -c "\[rmmt\] start .*mode=$MODE" "$LOG" 2>/dev/null)

if [ -z "$LINE" ]; then
	# started but no result is a HANG, which is a FAILURE; never started is a failure too (different kind)
	if [ "$STARTED" != "0" ]; then
		echo "GUEST-VERDICT mode=$MODE verdict=HANG denied=$DENIED created=$CREATED log=$LOG"
	else
		echo "GUEST-VERDICT mode=$MODE verdict=NO-RUN denied=$DENIED created=$CREATED log=$LOG"
	fi
	exit 1
fi

case "$LINE" in
	*pass=1*) echo "GUEST-VERDICT mode=$MODE verdict=PASS denied=$DENIED created=$CREATED :: $LINE"; exit 0 ;;
	*)        echo "GUEST-VERDICT mode=$MODE verdict=FAIL denied=$DENIED created=$CREATED :: $LINE"; exit 1 ;;
esac
