#!/bin/sh
# darling-suite-run.sh -- run a LIST of guest workloads, judge each by its own machine-readable line, and print ONE table.
#
# WHY THIS EXISTS. The workloads have to be judged one mode per boot (the boot itself is the fixture), and doing that by
# hand means a shell loop whose output has to be read line by line -- MEASURED: in one session a mode with NO result line
# was called PASS twice because the marker matched the workload's START line, and a mode that hung was reported as a pass
# by the harness's own VERDICT. The per-mode judge (darling-guest-verdict.sh) fixed the single case; this fixes the SET:
# one command, one table, one exit code, and the counters that decide acceptance printed next to every row.
#
# Acceptance for the set, when asked for:
#   --require-zero-creations   every row must report created=0
#   --nonpass-fails            any FAIL/HANG/NO-RUN makes the run exit non-zero (default: always)
#
# Usage:
#   darling-suite-run.sh --prefix P [--wait-base SECONDS] [--env K=V]... [--require-zero-creations] [--] MODE [ARGS...] :: ...
# Example:
#   darling-suite-run.sh --prefix /tmp/dr-on-matched --env DARLING_DISABLE_THREAD_RPC_UDS=1 \
#       -- require-zero-creations -- sem_ready 2 :: sem_block 100 1 :: sem_gap 5000 1
set -u

PREFIX=""
WAIT_BASE=70
ENVS=""
REQ_ZERO=0
MODES=""
SELF_DIR=$(dirname "$0")

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--wait-base) WAIT_BASE="$2"; shift 2 ;;
		--env) ENVS="$ENVS --env $2"; shift 2 ;;
		--require-zero-creations) REQ_ZERO=1; shift ;;
		--) shift; MODES="$*"; break ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ -n "$PREFIX" ] && [ -n "$MODES" ] || { echo "usage: see header" >&2; exit 2; }

fails=0
rows=0
printf '%-28s %-9s %-8s %-8s %s\n' MODE VERDICT denied created RESULT
printf '%s\n' "-------------------------------------------------------------------------------"

# modes are separated by '::' so a mode's own arguments survive
OLDIFS=$IFS
IFS=':'
set -- $MODES
IFS=$OLDIFS
for chunk in "$@"; do
	chunk=$(printf '%s' "$chunk" | sed 's/^ *//; s/ *$//')
	[ -n "$chunk" ] || continue
	mode=$(printf '%s' "$chunk" | cut -d' ' -f1)
	margs=$(printf '%s' "$chunk" | cut -s -d' ' -f2-)
	# the watchdog is the workload's own duration: base plus any milliseconds/multi-second argument
	extra=0
	for a in $margs; do case "$a" in ''|*[!0-9]*) ;; *) [ "$a" -gt 1000 ] && extra=$((a/1000)) ;; esac; done
	out=$("$SELF_DIR/darling-guest-verdict.sh" --prefix "$PREFIX" --mode "$mode" --args "$margs" \
		--wait $((WAIT_BASE + extra)) $ENVS 2>&1 | tail -1)
	verdict=$(printf '%s' "$out" | sed -n 's/.*verdict=\([A-Z-]*\).*/\1/p')
	denied=$(printf '%s' "$out" | sed -n 's/.*denied=\([0-9]*\).*/\1/p')
	created=$(printf '%s' "$out" | sed -n 's/.*created=\([0-9]*\).*/\1/p')
	line=$(printf '%s' "$out" | sed -n 's/.*:: //p')
	rows=$((rows + 1))
	if [ "$verdict" != "PASS" ]; then fails=$((fails + 1)); fi
	if [ "$REQ_ZERO" = "1" ] && [ "${created:-1}" != "0" ]; then fails=$((fails + 1)); fi
	printf '%-28s %-9s %-8s %-8s %s\n' "$mode $margs" "${verdict:-?}" "${denied:-?}" "${created:-?}" "${line:-<no result line>}"
done

printf '%s\n' "-------------------------------------------------------------------------------"
echo "SUITE rows=$rows failures=$fails require_zero_creations=$REQ_ZERO"
[ "$fails" = "0" ] && { echo "SUITE-VERDICT PASS"; exit 0; }
echo "SUITE-VERDICT FAIL"
exit 1
