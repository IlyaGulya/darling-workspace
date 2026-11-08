#!/usr/bin/env bash
# Contract for `dwdiag identity`: the per-thread identity census, from a core or from a live process.
#
# WHY THIS EXISTS AS A CONTRACT. The same census was taken by hand for the libdispatch SIGILL out of gdb's
# `thread apply all p/x` piped through greps, and one of those greps silently dropped lines and produced a WRONG
# count -- of a number that IS the answer (how many threads share one identity). Two properties therefore have to be
# pinned: the census counts every thread it lists, and a thread whose identity could NOT be read says so instead of
# printing a plausible number.
#
# WHAT THIS COVERS, and with what:
#   * the CORE path, on a real core this contract makes with `gdb -p <pid> -ex generate-core-file` -- which needs
#     ptrace (sudo here), but neither depends on nor changes the global core_pattern.
#   * the LIVE path, on a multi-threaded target this contract starts, sampled by two attaches.
#   * the honesty invariant on BOTH paths: every per-thread line carries either `slot3=0x…` or
#     `slot3=UNREADABLE (<reason>)`, and the JSON carries `value` XOR `read_error`.
#   * the count invariant: the number of per-thread lines equals the number the summary reports.
#
# WHAT THIS DOES NOT COVER. A GUEST core, i.e. the case the census was written for: a Darling guest sets $gs_base,
# so its slot 3 holds a real identity (26 of 28 threads at 0x103 on the core examined in bead dar-liaq), while a HOST
# process keeps its thread-local storage in FS and its $gs_base is 0. The guest case is an OPTIONAL input, because
# that core is 670 MB and lives in /tmp:
#
#   DWDIAG_GUEST_CORE=/tmp/t4cores/core.127004 bash tests/run-identity-contract.sh
#
# Without it this contract still fails on a dropped thread, a missing read failure, a broken JSON, or a live
# census that cannot attach -- it just cannot see a real GS-based identity.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DWDIAG="$ROOT/scripts/dwdiag"
DIR=$(mktemp -d /tmp/dwdiag-identity-contract-XXXXXX)
TARGET=""
cleanup() {
	[ -n "$TARGET" ] && kill "$TARGET" 2>/dev/null
	rm -rf "$DIR"
}
trap cleanup EXIT

fail=0
note() { echo "identity-contract: $1"; }
check() {
	if printf '%s\n' "$3" | grep -qE "$1"; then note "ok: $2"; else note "FAIL: missing $2   (wanted: $1)"; fail=1; fi
}
# The invariant the whole command rests on: a thread is either given a value or given a reason, never nothing and
# never a number that was not read.
all_threads_are_honest() { # label, output
	if printf '%s\n' "$2" | grep -E '^IDENTITY-THREAD ' | grep -qvE 'slot3=(0x[0-9a-f]+|UNREADABLE \()'; then
		note "FAIL: $1: a per-thread line carries neither a value nor a stated read failure"
		printf '%s\n' "$2" | grep -E '^IDENTITY-THREAD ' | grep -vE 'slot3=(0x[0-9a-f]+|UNREADABLE \()' | head -3
		fail=1
	else
		note "ok: $1: every per-thread line carries a value or a stated read failure"
	fi
}
thread_lines() { printf '%s\n' "$1" | grep -cE '^IDENTITY-THREAD '; }
counts_agree() { # label, output
	local lines summary
	lines=$(thread_lines "$2")
	summary=$(printf '%s\n' "$2" | grep -oE '^IDENTITY-SUMMARY distinct=[0-9]+ threads=[0-9]+' | awk '{print $3}' | cut -d= -f2)
	if [ -n "$summary" ] && [ "$lines" = "$summary" ]; then
		note "ok: $1: $lines per-thread line(s) and the summary reports the same number"
	else
		note "FAIL: $1: $lines per-thread line(s) but the summary reports '${summary:-<none>}'"
		fail=1
	fi
}
# The same three invariants over the machine-readable form, which is what a caller composes with.
check_json() { # label, json
	printf '%s\n' "$2" | python3 -c '
import json,sys
label=sys.argv[1]
d=json.load(sys.stdin)
for k in ("source","slot","threads_total","distinct","threads","summary","duplicates","zero","unreadable","unstable"):
    assert k in d, "missing key "+k
assert d["threads_total"] == len(d["threads"]), "threads_total must equal the thread list"
assert d["distinct"] == len(d["summary"]), "distinct must equal the number of values in the summary"
for t in d["threads"]:
    assert (t["slot3"] is None) != (t["read_error"] is None), \
        "a thread carries a VALUE or a REASON, never both and never neither"
    assert isinstance(t["zero"], bool) and t["zero"] == (t["slot3"] == "0x0"), "zero is derived from the value"
    for k in ("lwp","ip","image","image_addr","duplicate","unstable"):
        assert k in t, "missing thread key "+k
print("identity-contract: ok: "+label+": the JSON parses and its counts and flags agree with its threads")
' "$1" || fail=1
}

# --- a multi-threaded target. The census needs threads, and it needs TEN OR MORE of them: the gdb thread number is
# decimal, so a parser that reads it as hex starts losing rows at thread 10 -- which is exactly the dropped-thread
# failure this contract exists to catch. The guest case cannot be conjured here (see the header).
cat >"$DIR/target.py" <<'PY'
import threading, time
def work():
    time.sleep(120)
ts = [threading.Thread(target=work) for _ in range(12)]
[t.start() for t in ts]
print("READY", flush=True)
time.sleep(120)
PY
python3 "$DIR/target.py" >"$DIR/target.out" 2>&1 &
TARGET=$!
for _ in $(seq 1 40); do grep -q READY "$DIR/target.out" 2>/dev/null && break; sleep 0.25; done
tasks=$(ls /proc/"$TARGET"/task 2>/dev/null | wc -l)
if [ "$tasks" -lt 10 ]; then note "SKIP: the target did not come up with ten or more threads (tasks=$tasks)"; exit 0; fi
note "target pid=$TARGET with $tasks thread(s)"

# ptrace is required for BOTH halves here: gdb attaching to a live process, and gdb writing a core of one. On this
# host that means sudo (kernel.yama.ptrace_scope=1 allows only a tracer's own descendants, and the target is this
# script's child while dwdiag is not its ancestor).
SUDO=""
if sudo -n true 2>/dev/null; then SUDO="sudo -n"; else note "SKIP: no passwordless sudo, so no ptrace: the live and core halves of this contract cannot run"; exit 0; fi

# --- the LIVE census: two attaches with the target running in between.
live=$($SUDO "$DWDIAG" identity --pid "$TARGET" 2>&1)
check "^IDENTITY source=live pid=$TARGET threads=[0-9]+" "the live census names its source" "$live"
check "^IDENTITY-THREAD lwp=[0-9]+ slot3=" "every thread gets its own line" "$live"
check "^IDENTITY-LIVE threads_seen_in_both_samples=[0-9]+ churned=[0-9]+" "the two samples are joined and churn is reported" "$live"
check "^IDENTITY-UNSTABLE .+in between" "it says how instability was observed (the target ran between samples)" "$live"
check "^IDENTITY-SUMMARY distinct=[0-9]+ threads=[0-9]+ unreadable=[0-9]+ zero=[0-9]+" "the summary counts values, threads, failures and zeros" "$live"
all_threads_are_honest "live" "$live"
counts_agree "live" "$live"
# A host process has no GS-based TSD (its thread-locals live in FS), so every thread here MUST come back as a stated
# read failure -- and the command must say that is what it is looking at.
check "^IDENTITY-NOT-A-GUEST " "a host process is named as not a guest rather than reported as zeros" "$live"
check "^IDENTITY-READ-FAILURE reason=.+ lwps=[0-9]" "the failures are grouped by reason with the threads they hit" "$live"
live_json=$($SUDO "$DWDIAG" identity --pid "$TARGET" --json 2>/dev/null | tail -1)
check_json "live" "$live_json"
printf '%s\n' "$live_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert isinstance(d["unstable"], list), "a live census reports unstable as a LIST of threads"
assert d["not_a_guest_gs_based_tsd"] is True, "a host target is reported as not GS-based"
print("identity-contract: ok: the live JSON says instability is observable and the target is a host process")
' || fail=1

# --- the CORE census. `generate-core-file` writes the core without touching core_pattern, so this file cannot
# interfere with tests/run-core-contract.sh when both run in the same tier.
if $SUDO gdb -q -batch -nx -p "$TARGET" -ex "generate-core-file $DIR/core.identity" >"$DIR/gcore.log" 2>&1 && [ -s "$DIR/core.identity" ]; then
	core_out=$($DWDIAG identity --core "$DIR/core.identity" 2>&1)
	check "^IDENTITY source=core=$DIR/core.identity threads=[0-9]+" "the core census names the core it read" "$core_out"
	check "^IDENTITY-THREAD lwp=[0-9]+ slot3=" "the core census lists every thread" "$core_out"
	check "^IDENTITY-UNSTABLE .*a core is ONE instant" "it refuses to claim a core can show an unstable value" "$core_out"
	check "^IDENTITY-DEFINITION slot=3 read=.*gs_base[+]24.*name=__TSD_MACH_THREAD_SELF" "the definition of the word it reads is printed with the census" "$core_out"
	all_threads_are_honest "core" "$core_out"
	counts_agree "core" "$core_out"
	check "^IDENTITY-READ-FAILURE reason=.+ lwps=[0-9]" "a core whose threads have no GS base states that per reason" "$core_out"
	core_json=$($DWDIAG identity --core "$DIR/core.identity" --json 2>/dev/null | tail -1)
	check_json "core" "$core_json"
	printf '%s\n' "$core_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["unstable"] is None, "a core cannot observe instability, so the summary key is null and not false"
assert all(t["unstable"] is None for t in d["threads"]), "per-thread unstable is null for a core"
print("identity-contract: ok: the core JSON reports instability as unobservable rather than as stable")
' || fail=1
else
	note "SKIP: gdb could not write a core of pid $TARGET (see $DIR/gcore.log)"
fi

# A file that is not a core must be REFUSED by name: the census of an ELF executable would otherwise be an empty
# thread list, which reads exactly like a process with no threads.
if "$DWDIAG" identity --core "$DIR/target.py" >"$DIR/bad.out" 2>&1; then
	note "FAIL: a text file was accepted as a core"; fail=1
else
	check "ERROR: .*(shorter than an ELF header|not an ELF file|not ET_CORE)" "a file that is not a core is refused with a reason" "$(cat "$DIR/bad.out")"
fi

# --- the guest case, as an OPTIONAL input (see the header).
if [ -n "${DWDIAG_GUEST_CORE:-}" ] && [ -r "$DWDIAG_GUEST_CORE" ]; then
	g=$($DWDIAG identity --core "$DWDIAG_GUEST_CORE" 2>&1)
	check "^IDENTITY-DUPLICATE value=0x103 count=26 " "one identity is shared by 26 threads and is flagged" "$g"
	check "^IDENTITY-SUMMARY value=0x103 count=26" "the summary counts per distinct value" "$g"
	check "^IDENTITY-SUMMARY distinct=3 threads=28 unreadable=0 zero=0" "28 threads, 3 distinct identities, nothing unread" "$g"
	check "^IDENTITY-THREAD lwp=127004 slot3=0x103 zero=false duplicate=true" "the faulting thread's own identity is 0x103 and marked a duplicate" "$g"
	g_json=$($DWDIAG identity --core "$DWDIAG_GUEST_CORE" --json 2>/dev/null | tail -1)
	check_json "guest core" "$g_json"
	printf '%s\n' "$g_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
assert d["threads_total"] == 28 and d["distinct"] == 3, d["threads_total"]
shared=[x for x in d["summary"] if x["count"] > 1]
assert len(shared) == 1 and shared[0]["value"] == "0x103" and shared[0]["count"] == 26, shared
assert d["duplicates"][0]["count"] == 26
print("identity-contract: ok: the guest census is 26 of 28 threads at 0x103")
' || fail=1
else
	note "SKIP: set DWDIAG_GUEST_CORE=<a guest core> to also check a real GS-based identity census (26 of 28 at 0x103)"
fi

if [ "$fail" -ne 0 ]; then note "FAIL"; exit 1; fi
note "PASS"
exit 0
