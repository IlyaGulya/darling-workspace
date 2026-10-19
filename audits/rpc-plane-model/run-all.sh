#!/bin/bash
# Model-check the four audit questions across the variant matrix.
#
#   ./run-all.sh            -> prints a per-invariant verdict table
#   TLA_JAR=/path/tla2tools.jar ./run-all.sh
#
# Each cell is the verdict of ONE invariant under ONE configuration, so a
# "VIOLATED" cell names the exact variant (and therefore the exact production
# shape) that breaks that invariant. TLC runs are small (thousands of states).
set -u
JAR="${TLA_JAR:-/tmp/tla2tools.jar}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

CONFIGS="guarded unowned-arm no-gen-guard reclaim-race internal-cycle real-console-defer arm-after-queue"
INVS="Inv2NoWrongGeneration Inv3NoSilentLoss Inv4NoDuplicateApply Inv5NoForeignReply Inv6NoInternalCycle Inv7QueuedCallHasOwner"

write_cfg() {
  local name=$1 arm=$2 gen=$3 ext=$4 reclaim=$5 latearm=$6 inv=$7
  cat > cfg_$name.cfg <<CFG
SPECIFICATION Spec

CONSTANT Threads = {t1, t2}
CONSTANT Clients = {c1}
CONSTANT ArmWithoutOwner = $arm
CONSTANT GenGuard = $gen
CONSTANT ParkedCompletesExternally = $ext
CONSTANT ClientReclaimsOnTimeout = $reclaim
CONSTANT ArmAfterQueue = $6

INVARIANT $inv
CFG
}

run_cell() {
  local name=$1 inv=$2
  if java -cp "$JAR" tlc2.TLC -cleanup -config cfg_$name.cfg -workers 4 DServerPlane.tla 2>&1 \
       | grep -q "No error has been found"; then
    echo OK
  else
    echo VIOLATED
  fi
}

printf '%-16s %-8s %-8s %-8s %-8s' "config" "Arm" "Gen" "Ext" "Reclaim"; printf ' | %-26s %.5s\n' "invariant" ""
for cfg in $CONFIGS; do
  case $cfg in
    guarded)        set -- FALSE TRUE  TRUE  FALSE FALSE ;;
    unowned-arm)    set -- TRUE  TRUE  TRUE  FALSE FALSE ;;
    no-gen-guard)   set -- TRUE  FALSE TRUE  TRUE  FALSE ;;
    reclaim-race)   set -- FALSE TRUE  TRUE  TRUE  FALSE ;;
    internal-cycle) set -- FALSE TRUE  FALSE FALSE FALSE ;;
    real-console-defer) set -- TRUE TRUE TRUE TRUE FALSE ;;
    arm-after-queue) set -- FALSE TRUE TRUE FALSE TRUE ;;
  esac
  printf '\n%-20s %-8s %-8s %-8s %-8s %-8s\n' "$cfg" "$1" "$2" "$3" "$4" "$5"
  for inv in $INVS; do
    write_cfg "$cfg" "$1" "$2" "$3" "$4" "$5" "$inv"
    printf '    %-28s %s\n' "$inv" "$(run_cell "$cfg" "$inv")"
  done
done

# TLC byproducts: the counterexample traces are preserved as text in
# counterexamples/, so the binary/metadir artifacts are not kept.
rm -f DServerPlane_TTrace_*.bin DServerPlane_TTrace_*.tla
rm -rf states
