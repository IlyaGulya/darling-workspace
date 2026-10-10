#!/usr/bin/env bash
# The vendored diagnostics tool's own tests, plus an EXECUTABLE TRUTH TABLE over the five states a run's instrument
# evidence can be in: measured, partial, unavailable, no-evidence, unscoped.
#
# WHY A CONTRACT: these tests existed and NOTHING ran them, so one of them was red (fourteen registered instruments
# had no sample line, and the census could not notice) -- a contract that nothing runs cannot fail.
#
# WHY THE TRUTH TABLE: an in-path instrument writes to the SAME fd as the measured output, so a census over a log the
# run does not own prints zeros that cannot be told from an instrument standing silent. The runner therefore selects a
# run's evidence from the RUN MANIFEST the launcher writes at job start (prefix, start/end, every log with its pre-run
# size) and reports which state it reached. This table drives the real CLI against a throwaway mirror: no real prefix,
# no guest boot. It FAILS if a state is reported as a measured zero, and if a state is not distinguished from its
# neighbour (partial vs unavailable vs no-evidence).
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT=$PWD
CRATE=tools/darling-debug-runner
export CARGO_NET_OFFLINE=true

# 1. The crate's own tests (the instrument-registry census and the deploy-receipt verdict merge).
cargo test --offline --locked --manifest-path "$CRATE/Cargo.toml"

# 2. The binary the truth table drives. It is the SAME crate the contract tests; the doc/CLI ship together.
cargo build --offline --locked --manifest-path "$CRATE/Cargo.toml"
BIN="$ROOT/$CRATE/target/debug/darling-debug-runner"
[ -x "$BIN" ] || { echo "contract: no binary at $BIN" >&2; exit 3; }

MIRROR=$(mktemp -d /tmp/dwdiag-evidence-contract-XXXXXX)
trap 'rm -rf "$MIRROR"' EXIT

# The fixture window is fixed in the year 2000 so every real /tmp/darling-boot-*.log on this host is OUTSIDE it: the
# table must not depend on what other runs happen to have left behind.
START=946684800
END=946771200
INSTRUMENT_LINE='[ulock-wait-park] obj=0x1000 op=wait owner_tid=2 waiter_tid=3'

fixture() { # name -> $MIRROR/<name>/{state,prefix/private/var/tmp}
	local d="$MIRROR/$1"
	mkdir -p "$d/state" "$d/prefix/private/var/tmp"
	: >"$d/state/log"
	printf '%s' "$d"
}

manifest() { # dir, then "path offset" per registered log
	local d=$1
	shift
	{
		printf 'version=1\nstart=%s\nend=%s\nprefix=%s\n' "$START" "$END" "$d/prefix"
		local entry
		for entry in "$@"; do
			printf 'file=%s\n' "$entry"
		done
	} >"$d/state/evidence-manifest"
}

state_of() { printf '%s' "$1" | grep -oE '"state":"[^"]+"' | head -1 | cut -d'"' -f4; }
rows_of() { printf '%s' "$1" | grep -oE '"rows_bytes":[0-9]+' | head -1 | cut -d: -f2; }
zero_measured() { printf '%s' "$1" | grep -oE '"zero_is_measured":(true|false)' | head -1 | cut -d: -f2; }

declare -a SEEN_STATES=()
declare -a SEEN_NAMES=()
declare -a SEEN_ROWS=()
declare -a SEEN_ZERO=()
declare -a TESTED_NAMES=()
declare -a TESTED_STATES=()

fail() { printf 'TRUTH-TABLE FAIL: %s\n' "$*" >&2; exit 1; }

run_case() { # fixture-dir-name expected-state expected-rows-mode(any|zero)
	local name=$1 expected=$2 rows_mode=$3
	local json got rows zm
	json=$("$BIN" diag evidence --state-dir "$MIRROR/$name/state" --json)
	got=$(state_of "$json")
	rows=$(rows_of "$json")
	zm=$(zero_measured "$json")
	[ -n "$got" ] || fail "$name: no state in the JSON: $json"
	[ -n "$rows" ] || fail "$name: no rows_bytes in the JSON: $json"
	printf 'CASE %-22s state=%-12s rows_bytes=%-6s zero_is_measured=%s expected=%s\n' \
		"$name" "$got" "$rows" "$zm" "$expected"
	[ "$got" = "$expected" ] || fail "$name: reported state '$got', expected '$expected'"
	if [ "$rows_mode" = zero ]; then
		[ "$rows" = 0 ] || fail "$name: expected a measured zero (rows_bytes=0), got $rows"
	fi
	# The zero rule: `zero_is_measured` may be true ONLY in the measured state.
	if [ "$zm" = true ] && [ "$got" != measured ]; then
		fail "$name: state '$got' was reported as a measured zero"
	fi
	if [ "$rows" = 0 ] && [ "$got" != measured ] && [ "$zm" = true ]; then
		fail "$name: an empty census in state '$got' was reported as a measured zero"
	fi
	SEEN_NAMES+=("$name"); SEEN_STATES+=("$got"); SEEN_ROWS+=("$rows"); SEEN_ZERO+=("$zm")
	TESTED_NAMES+=("$name"); TESTED_STATES+=("$got")
}

# --- fixture 1: readable + scoped + complete, nothing written -> the ONLY state that may print 0 ---------------
d=$(fixture measured-zero)
: >"$d/state/reg.log"
manifest "$d" "$d/state/reg.log 0"
run_case measured-zero measured zero

# --- fixture 2: readable + scoped + complete, rows present ------------------------------------------------------
d=$(fixture measured-rows)
printf 'pre-run noise\n%s\n' "$INSTRUMENT_LINE" >"$d/state/reg.log"
manifest "$d" "$d/state/reg.log 0"
run_case measured-rows measured any
# The run's own bytes reach the slice (this is what the census then reads).
grep -aq 'ulock-wait-park' "$MIRROR/measured-rows/state/instruments-sliced.log" \
	|| fail "measured-rows: the run's own instrument line is missing from the slice"

# --- fixture 3: recorded pre-run offset is past the current size -> truncated/rotated -> partial ----------------
d=$(fixture truncated)
printf 'short\n' >"$d/state/reg.log"
manifest "$d" "$d/state/reg.log 500"
run_case truncated partial zero
grep -aq 'EVIDENCE-STATE PARTIAL' <("$BIN" diag evidence --state-dir "$d/state") \
	|| fail "truncated: the human output does not name the partial state"
if [ "$(rows_of "$( "$BIN" diag evidence --state-dir "$d/state" --json)")" != 0 ]; then
	fail "truncated: a truncated window produced rows (it must contribute none)"
fi

# --- fixture 4: a registered log is missing -> unavailable ------------------------------------------------------
d=$(fixture missing)
manifest "$d" "$d/state/gone.log 0"
run_case missing unavailable zero

# --- fixture 5: a registered log is unreadable (a directory, so this is root-proof) -> unavailable --------------
d=$(fixture unreadable)
mkdir "$d/state/not-a-file.log"
manifest "$d" "$d/state/not-a-file.log 0"
run_case unreadable unavailable zero

# --- fixture 6: instrument lines exist, but only another run's -> NO-EVIDENCE -----------------------------------
d=$(fixture foreign-only)
: >"$d/state/reg.log"
manifest "$d" "$d/state/reg.log 0"
printf '%s\n' "$INSTRUMENT_LINE" >"$d/prefix/private/var/tmp/other-run.log"
run_case foreign-only no-evidence zero
# The stale/other-run log must NOT contribute a single row to this run's slice.
if grep -aq 'ulock-wait-park' "$MIRROR/foreign-only/state/instruments-sliced.log"; then
	fail "foreign-only: another run's instrument line reached this run's slice"
fi

# --- fixture 7: no manifest at all -> unscoped, and nothing is guessed ------------------------------------------
d=$(fixture unscoped)
printf '%s\n' "$INSTRUMENT_LINE" >"$d/state/reg.log"
run_case unscoped unscoped zero
if [ -s "$MIRROR/unscoped/state/instruments-sliced.log" ]; then
	fail "unscoped: without a manifest the slice must be empty, not a guessed log"
fi

# --- the states must be distinguishable, and every one of the five must be exercised ----------------------------
distinct=$(printf '%s\n' "${SEEN_STATES[@]}" | sort -u | wc -l)
printf 'STATES observed=%s distinct=%s\n' "${SEEN_STATES[*]}" "$distinct"
[ "$distinct" -eq 5 ] || fail "the table did not distinguish five states (saw $distinct)"

i=0
for name in "${SEEN_NAMES[@]}"; do
	got=${SEEN_STATES[$i]}
	rows=${SEEN_ROWS[$i]}
	# A zero census is a measurement in NO state but `measured`, whatever the cause.
	if [ "$rows" = 0 ] && [ "$got" != measured ]; then
		printf 'ZERO-RULE %-22s state=%-12s rows=0 -> NOT a measurement\n' "$name" "$got"
	else
		printf 'ZERO-RULE %-22s state=%-12s rows=%s\n' "$name" "$got" "$rows"
	fi
	i=$((i + 1))
done

# Neighbour distinction: partial vs unavailable vs no-evidence must be three different strings.
p=$(state_of "$("$BIN" diag evidence --state-dir "$MIRROR/truncated/state" --json)")
u=$(state_of "$("$BIN" diag evidence --state-dir "$MIRROR/missing/state" --json)")
n=$(state_of "$("$BIN" diag evidence --state-dir "$MIRROR/foreign-only/state" --json)")
[ "$p" != "$u" ] || fail "partial and unavailable are not distinguished (both $p)"
[ "$u" != "$n" ] || fail "unavailable and no-evidence are not distinguished (both $u)"
[ "$p" != "$n" ] || fail "partial and no-evidence are not distinguished (both $p)"

# The futex trace: the wake half cannot come from a snapshot, and a trace that cannot be shown to belong to the
# bundle's run must be REFUSED with its reason -- a trace from another run reads exactly like this one, because a
# recycled pid is still a pid, and the window is the only thing that separates them.
FTRACE=$(mktemp -d "$MIRROR/futex-XXXXXX")
mkdir -p "$FTRACE/capture-tree/pid-100" "$FTRACE/guarded-tree/pid-300"
printf '1000-2000 rw-s 00000000 00:01 42 /memfd:x (deleted)\n' >"$FTRACE/capture-tree/pid-100/proc-maps.txt"
printf '9000-a000 rw-s 00000000 00:01 42 /memfd:x (deleted)\n' >"$FTRACE/guarded-tree/pid-300/proc-maps.txt"
printf 'version=1\nstart=%s\nend=%s\n' "$START" "$END" >"$FTRACE/evidence-manifest"
T0=$((START * 1000000000))
{
	printf 'WAIT pid=100 tid=101 uaddr=0x1080 ts_ns=%s\n' "$((T0 + 10))"
	printf 'WAKE pid=300 tid=301 uaddr=0x9080 woke=1 ts_ns=%s\n' "$((T0 + 20))"
} >"$FTRACE/good.trace"
# 0x1080 in pid-100 and 0x9080 in pid-300 are the same byte of the same memfd: the waker is in a different address
# space, so a pairing that compared virtual addresses could not find it.
out=$("$BIN" diag futex --bundle "$FTRACE" --futex-trace "$FTRACE/good.trace") \
	|| fail "the pairing command failed on a trace inside the run window"
grep -aq 'WAKER pid=300 tid=301 woke=1' <<<"$out" || fail "a same-object wake in another address space was not paired: $out"
json=$("$BIN" diag futex --bundle "$FTRACE" --futex-trace "$FTRACE/good.trace" --json) \
	|| fail "the pairing command failed in --json mode"
grep -aq '"waker_pid":300' <<<"$json" || fail "the machine-readable pairing did not name the waker: $json"
grep -aq '"window_open_ended":false' <<<"$json" || fail "a manifest with both ends must not report an open-ended window: $json"
# The absence of a trace is a named absence, never a silent zero.
grep -aq 'FUTEX-TRACE-ABSENT' <("$BIN" diag futex --bundle "$FTRACE") \
	|| fail "no trace was given and the absence was not named"
printf 'WAIT pid=999 tid=999 uaddr=0x1080 ts_ns=%s\n' "$((T0 + 30))" >"$FTRACE/foreign-pid.trace"
if out=$("$BIN" diag futex --bundle "$FTRACE" --futex-trace "$FTRACE/foreign-pid.trace" 2>&1); then
	fail "a trace naming a pid the bundle never saw was ACCEPTED: $out"
fi
grep -aq 'did not capture' <<<"$out" || fail "the refusal did not name the reason: $out"
printf 'WAIT pid=100 tid=101 uaddr=0x1080 ts_ns=%s\n' "$((( END + 10) * 1000000000))" >"$FTRACE/foreign-window.trace"
if out=$("$BIN" diag futex --bundle "$FTRACE" --futex-trace "$FTRACE/foreign-window.trace" 2>&1); then
	fail "a trace from outside the run window was ACCEPTED: $out"
fi
grep -aq 'run end' <<<"$out" || fail "the window refusal did not name the reason: $out"
printf 'FUTEX-TRACE-DISCIPLINE PASS\n'

# The ring decoder: the record schema is fixed at six words, a slot read while it is being written is NOT a
# measurement, and a silent ring is not an empty one. The producer stores its fields with plain stores after a
# single atomic reservation, so a torn record is a real thing to see -- and the dump's own `shown=` is how
# overwritten slots can be measured instead of being passed off as absence.
RDIR=$(mktemp -d "$MIRROR/ring-XXXXXX")
{
	printf 'dtape.ering dump events=4 shown=4\n'
	printf 'dtape.ering seq=0 tag=recv_park a=0x1 b=0x2 c=0x3 d=0x4\n'
	printf 'dtape.ering seq=1 tag=recv_done a=0x5 b=0x6 c=0x7 d=0x8\n'
	printf 'dtape.ering seq=2 tag=recv_park a=0x9 b=0xa c=0xb\n'
} >"$RDIR/torn.log"
out=$("$BIN" diag ring --log "$RDIR/torn.log") || fail "the ring decoder failed on a dump with one torn record"
grep -aq 'parsed=2' <<<"$out" || fail "a torn record was counted as a measurement: $out"
grep -aq 'RING-MALFORMED 1' <<<"$out" || fail "the torn record was not reported as malformed: $out"
# shown=4, parsed=2, torn=1: the producer PRINTED three of the four slots and skipped one, and a torn record is a
# different fact from a skipped one -- collapsing them is how a half-written event becomes a missing event.
grep -aq 'RING-TORN 1 slot' <<<"$out" || fail "a printed-but-incomplete record was not reported as torn: $out"
grep -aq 'RING-SKIPPED 1 slot' <<<"$out" || fail "the one slot the producer skipped was not measured: $out"
json=$("$BIN" diag ring --log "$RDIR/torn.log" --json) || fail "the ring decoder failed in --json mode"
grep -aq '"parsed":2' <<<"$json" || fail "the json did not carry the parsed count: $json"
grep -aq '"torn":1' <<<"$json" || fail "the json did not carry the torn count: $json"
grep -aq '"skipped":1' <<<"$json" || fail "the json did not carry the skipped count: $json"
{
	printf 'dtape.ering dump events=3 shown=3\n'
	printf 'dtape.ering seq=0 tag=recv_park a=0x1 b=0x2 c=0x3 d=0x4\n'
	printf 'dtape.ering seq=1 tag=recv_done a=0x5 b=0x6 c=0x7 d=0x8\n'
	printf 'dtape.ering seq=2 tag=? a=0x9 b=0xa c=0xb d=0xc\n'
} >"$RDIR/complete.log"
out=$("$BIN" diag ring --log "$RDIR/complete.log") || fail "the ring decoder failed on a complete dump"
grep -aq 'RING-COMPLETE' <<<"$out" || fail "a complete dump was not reported complete: $out"
grep -aq 'UNKNOWN-TAG' <<<"$out" || fail "an unknown tag was not named as unrecoverable: $out"
{
	printf 'dtape.ering dump events=3000 shown=10\n'
	i=0
	while [ "$i" -lt 8 ]; do
		printf 'dtape.ering seq=%s tag=recv_park a=0x1 b=0x2 c=0x3 d=0x4\n' "$i"
		i=$((i + 1))
	done
} >"$RDIR/omitting.log"
out=$("$BIN" diag ring --log "$RDIR/omitting.log") || fail "the ring decoder failed on an omitting dump"
grep -aq 'RING-SKIPPED 2 slot' <<<"$out" || fail "overwritten slots were not measured as skips: $out"
printf 'no ring here\n' >"$RDIR/silent.log"
grep -aq 'RING-NO-DUMP' <("$BIN" diag ring --log "$RDIR/silent.log") || fail "a log with no dump was not reported as absent"
grep -aq '"present":false' <("$BIN" diag ring --log "$RDIR/silent.log" --json) || fail "the json absence was not reported"
printf 'RING-DECODER PASS\n'

printf 'EVIDENCE-TRUTH-TABLE PASS cases=%s states=%s\n' "${#SEEN_NAMES[@]}" "$distinct"
