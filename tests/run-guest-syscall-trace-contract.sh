#!/usr/bin/env bash
# Contract for the guest Darwin-syscall tracer (`DARLING_GUEST_SYSCALL_TRACE=1`).
#
# The instrument lives in the guest library (the dispatcher hook in
# emulation/src/xnu_syscall/bsd/bsd_syscall.S and its C side in
# emulation/src/other/mach/lkm.c) and is driven here through `scripts/dwdiag verdict`. Each claim below
# is a defect that was MEASURED while building it, so the contract is the reason those defects cannot
# come back silently:
#
#   1. PRESERVES THE GUEST. With the hatch off, a known-good workload still verdicts PASS. MEASURED: an
#      entry trampoline that did not save the registers the Darwin syscall ABI uses turned EVERY run into
#      NO-RUN, hatch or not -- an instrument that breaks the guest is worse than no instrument.
#   2. TRACES FOR REAL. With the hatch on the same workload still PASSes AND its run log carries at least
#      MIN_TRACE_LINES `[bsys nr=` lines covering at least MIN_DISTINCT_NR distinct syscall numbers.
#      MEASURED: a print placed on the earliest bootstrap path produced 28 lines and then stopped, which
#      looks exactly like a working trace of a short process.
#   3. DOES NOT TRACE ITSELF. MEASURED: the first version printed through a Darwin syscall, so the trace
#      captured its own writes (21 of its first 28 lines were the emitter's `write`). A single number
#      dominating the trace is therefore a failure, not a workload property.
#
# Needs a booted prefix: pass --prefix PATH or set DPREFIX. It is a guest-runtime gate, so the host tier
# cannot run it unattended; it is registered as an excluded contract with that reason.
set -euo pipefail

PREFIX="${DPREFIX:-}"
while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="${2:?--prefix needs a value}"; shift 2 ;;
		*) echo "usage: $0 [--prefix PATH]  (or set DPREFIX)" >&2; exit 2 ;;
	esac
done
if [ -z "$PREFIX" ]; then
	echo "guest-syscall-trace: no prefix: pass --prefix PATH or set DPREFIX" >&2
	exit 2
fi

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

MIN_TRACE_LINES=40
MIN_DISTINCT_NR=6
MODE=sem_ready
ARGS='2'
REPEATS=2

failures=0
note() { printf 'CLAIM %s %s\n' "$1" "$2"; }
claim() { # name ok-detail
	if [ "$2" = ok ]; then note "$1" "OK $3"; else note "$1" "FAIL $3"; failures=$((failures + 1)); fi
}

run_arm() { # env-var-or-empty -> prints "verdicts=<space separated>|logs=<space separated>"
	local extra_env="$1"
	local out
	if [ -n "$extra_env" ]; then
		out="$(./scripts/dwdiag verdict --prefix "$PREFIX" --mode "$MODE" --args "$ARGS" \
			--wait 25 --repeat "$REPEATS" --env "$extra_env" 2>/dev/null)"
	else
		out="$(./scripts/dwdiag verdict --prefix "$PREFIX" --mode "$MODE" --args "$ARGS" \
			--wait 25 --repeat "$REPEATS" 2>/dev/null)"
	fi
	printf '%s\n' "$out" | awk '
		/^VERDICT(\[| )/ { for (i = 1; i <= NF; ++i) if ($i ~ /^verdict=/) { sub(/^verdict=/, "", $i); v = v " " $i } }
		/^LOG=/ { sub(/^LOG=/, "", $0); l = l " " $0 }
		END { printf "verdicts=%s|logs=%s\n", (v == "" ? "<none>" : v), (l == "" ? "<none>" : l) }'
}

# --- claim 1: the instrument must not change the guest when it is off -------------------------------------------------
off="$(run_arm '')"
off_verdicts="$(printf '%s' "$off" | sed -n 's/^verdicts=\([^|]*\)|.*$/\1/p')"
# MEASURED defect of this contract's first version: the verdict list arrives space-separated, so `NO-RUN PASS`
# satisfied a "no verdict without PASS" check and a run that never started counted as preserved. Every token must
# be exactly PASS now.
if [ -z "$off_verdicts" ] || printf '%s\n' $off_verdicts | grep -qvx 'PASS'; then
	claim "preserves-guest" fail "hatch off: verdicts=$off_verdicts (want only PASS)"
else
	claim "preserves-guest" ok "hatch off: verdicts=$off_verdicts"
fi

# --- claim 2 and 3: the trace must exist, and must not be its own subject ---------------------------------------------
on="$(run_arm 'DARLING_GUEST_SYSCALL_TRACE=1')"
on_verdicts="$(printf '%s' "$on" | sed -n 's/^verdicts=\([^|]*\)|.*$/\1/p')"
first_log="$(printf '%s' "$on" | sed -n 's/^[^|]*|logs= *\([^ ]*\).*$/\1/p')"

if [ -z "$on_verdicts" ] || printf '%s\n' $on_verdicts | grep -qvx 'PASS'; then
	claim "trace-preserves-guest" fail "hatch on: verdicts=$on_verdicts (want only PASS)"
else
	claim "trace-preserves-guest" ok "hatch on: verdicts=$on_verdicts"
fi

if [ -z "$first_log" ] || [ ! -f "$first_log" ]; then
	claim "traces-real-syscalls" fail "no run log found for the traced arm"
	claim "does-not-trace-itself" fail "no run log found for the traced arm"
else
	lines="$(grep -c '\[bsys nr=' "$first_log" || true)"
	distinct="$(grep -o '\[bsys nr=[0-9]*' "$first_log" | sort -u | wc -l)"
	top_share="$(grep -o '\[bsys nr=[0-9]*' "$first_log" | sort | uniq -c | sort -rn | awk 'NR==1 {print $1; exit}')"
	workload_ran="$(grep -c "$MODE" "$first_log" || true)"
	if [ "${lines:-0}" -ge "$MIN_TRACE_LINES" ] && [ "${distinct:-0}" -ge "$MIN_DISTINCT_NR" ]; then
		claim "traces-real-syscalls" ok "lines=$lines distinct_nr=$distinct"
	else
		claim "traces-real-syscalls" fail "lines=$lines distinct_nr=$distinct (want >=$MIN_TRACE_LINES and >=$MIN_DISTINCT_NR)"
	fi
	# The emitter's own writes may not dominate: with self-tracing the single most frequent number held 21 of 28.
	if [ "${lines:-0}" -gt 0 ] && [ "${top_share:-0}" -le $((lines / 2)) ] && [ "${workload_ran:-0}" -ge 1 ]; then
		claim "does-not-trace-itself" ok "top_nr_share=$top_share/$lines workload_line=yes"
	else
		claim "does-not-trace-itself" fail "top_nr_share=$top_share/$lines workload_line=${workload_ran:-0} (emitter may be tracing its own output)"
	fi
fi

if [ "$failures" -eq 0 ]; then
	echo "GUEST-SYSCALL-TRACE-CONTRACT VERDICT PASS"
	exit 0
fi
echo "GUEST-SYSCALL-TRACE-CONTRACT VERDICT FAIL failures=$failures"
exit 1
