#!/bin/bash
# Contract for `dwdiag loops`: it must separate a spinning tail from a varied one, and it must recognise a
# repetition that differs only in its numbers.
#
# Why each assertion exists, from this command's own first runs:
#   * normalization: the first version started a number at ANY hex digit, so "pc-entry" became "p#-#ntry" and two
#     different markers could collapse into one string. It must start at a DIGIT and continue through hex.
#   * the verdict: the first metric compared the repeat count with the SPAN IN LINES, which is thousands whether or
#     not anything else happened; then it compared with the tail's variety over 200 lines, which was still too
#     wide. The measurement that separated a real spin from a working run was the DISTINCT count over the last 30
#     lines: 5-6 for the spin, 14 for the run whose process exited.
#   * the wording: a varied tail RULES OUT a spin and nothing more. The run where the linker exited has a varied
#     tail too, so claiming "progressing" would be the exact inference-from-a-log mistake this session kept making.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DWDIAG="$ROOT/scripts/dwdiag"
SPIN=$(mktemp /tmp/dwdiag-loops-spin-XXXX.log)
VARIED=$(mktemp /tmp/dwdiag-loops-varied-XXXX.log)
trap 'rm -f "$SPIN" "$VARIED"' EXIT

# --- spinning: a setup phase, then the same four markers over and over, differing only in numbers
{
	echo "[boot] start"
	for i in $(seq 1 40); do echo "[setup] step=$i alpha=$RANDOM"; done
	for i in $(seq 1 40); do
		echo "[pc-entry sp=$(printf '%x' $((0x70000 + i)))]"
		echo "[pc-nothreads sp=$(printf '%x' $((0x70000 + i)))]"
		echo "[pc-postplane st=$(printf '%x' $i)- sp=$(printf '%x' $((0x70000 + i)))]"
		echo "[pc-return-9+ sp=$(printf '%x' $((0x70000 + i)))]"
	done
} >"$SPIN"

# --- varied: the tail keeps producing lines it has not produced before
{
	echo "[boot] start"
	for i in $(seq 1 40); do echo "[setup] step=$i alpha=$RANDOM"; done
	# The tail must differ in WORDS. A tail whose lines differ only in their numbers normalizes to ONE pattern and
	# is a spin by this command's definition -- which is the point, and the first version of this fixture got it
	# wrong by looking varied to a human while normalizing to a single line.
	for w in alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau 	         upsilon phi chi psi omega alpha2 beta2 gamma2 delta2 epsilon2; do
		echo "[stage] mark-$w value=$RANDOM"
	done
} >"$VARIED"

fail=0
note() { echo "loops-contract: $1"; }
check() {
	# -E, because these patterns use '+' and BRE would read it as a literal -- a check that can never match is
	# the same class of silence this whole command exists to remove.
	if echo "$3" | grep -qE "$1"; then note "ok: $2"; else note "FAIL: missing $2   (wanted: $1)"; fail=1; fi
}
absent() {
	if echo "$3" | grep -q "$1"; then note "FAIL: unexpected $2   (matched: $1)"; fail=1; else note "ok: no $2"; fi
}

spin_out=$("$DWDIAG" loops --log "$SPIN" 2>&1)
check "tail=spin" "a repeated tail is reported as a spin" "$spin_out"
check "tail_distinct=[0-9]+" "the deciding measurement is printed, not just the verdict" "$spin_out"
# Normalization: 40 repetitions that differ only in their stack words must be ONE pattern, not 160. And the marker
# text must survive, which is what the earlier normalize broke.
check "LOOP .* line=\[pc-postplane st=#- sp=#\]" "numbers collapse, marker text survives" "$spin_out"
check "line=\[pc-entry sp=#\]" "a marker whose letters are hex digits is not eaten" "$spin_out"

varied_out=$("$DWDIAG" loops --log "$VARIED" 2>&1)
check "tail=varied" "a tail of new lines is not called a spin" "$varied_out"
check "LOOPS-NOTE a varied tail RULES OUT a spin" "and the claim is limited to what a log can support" "$varied_out"

echo "$spin_out" | tail -2
if [ "$fail" -ne 0 ]; then note "FAIL"; exit 1; fi
note "PASS"
exit 0
