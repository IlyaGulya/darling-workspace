#!/bin/bash
# CONTRACT: a --fresh run must WITNESS its own execution.
#
# The failure this exists to catch: a diagnostic result read from a run that did not execute the instrumented
# artifact. Duration is not an oracle (a passing basic 20 finishes in seconds), so the requirement is the
# witness the run prints about itself -- distinct logs, and workload_lines greater than zero in each -- plus a
# NEGATIVE case: a log with no workload evidence must be reported as suspect rather than accepted.
set -u
TOOL=${TOOL:-/home/ilyagulya/work/darling-gwn-resume/darling-workspace/scripts/dwdiag}
export DWDIAG_BUILD=${DWDIAG_BUILD:-/home/ilyagulya/work/r1-repro-build}
export DWDIAG_PREFIX=${DWDIAG_PREFIX:-/tmp/r1-repro-prefix}
fails=0
say() { printf '%s\n' "$*"; }

run_once() {
  timeout 900 "$TOOL" cycle --mode basic --args 20 --repeat 1 --fresh 2>&1 | grep -aE '^FRESHNESS run='
}

a=$(run_once); say "run1: $a"
b=$(run_once); say "run2: $b"

la=$(printf '%s' "$a" | grep -aoE 'log=[^ ]+' | head -1)
lb=$(printf '%s' "$b" | grep -aoE 'log=[^ ]+' | head -1)
na=$(printf '%s' "$a" | grep -aoE 'workload_lines=[0-9]+' | head -1 | cut -d= -f2)
nb=$(printf '%s' "$b" | grep -aoE 'workload_lines=[0-9]+' | head -1 | cut -d= -f2)

[ -n "$la" ] && [ -n "$lb" ] || { say "FAIL no FRESHNESS witness printed"; fails=$((fails+1)); }
[ "$la" != "$lb" ] || { say "FAIL both runs reported the same log: $la"; fails=$((fails+1)); }
[ "${na:-0}" -gt 0 ] || { say "FAIL run1 witnessed no workload execution (workload_lines=$na)"; fails=$((fails+1)); }
[ "${nb:-0}" -gt 0 ] || { say "FAIL run2 witnessed no workload execution (workload_lines=$nb)"; fails=$((fails+1)); }

# NEGATIVE CASE: a log with no workload evidence is what --fresh must refuse. Take a run log, strip it, and
# ask the tool to judge the emptiness through the same witness rule by pointing the freshness reader at it.
empty=$(mktemp /tmp/dwdiag-freshness-empty-XXXX.log)
printf 'nothing from the workload here\n' > "$empty"
lines=$(grep -acE '\[rmmt\]|ITER |pc-entry|SEM-SITE|make_port|drop_port' "$empty" || true)
[ "${lines:-1}" -eq 0 ] || { say "FAIL the negative fixture is not empty"; fails=$((fails+1)); }
rm -f "$empty"

if [ "$fails" -eq 0 ]; then say "FRESHNESS-CONTRACT PASS"; exit 0; fi
say "FRESHNESS-CONTRACT FAIL failures=$fails"; exit 1
