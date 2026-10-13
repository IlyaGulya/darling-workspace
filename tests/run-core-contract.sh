#!/bin/bash
# Contract for `dwdiag core`: it must read a real core and NAME the fault, and when there is no core it must say
# WHY rather than return nothing.
#
# Why both halves matter. A guest's ud2 runs natively, so Darling cannot see it as a syscall, and four other routes
# measured blind on the same crash: silent dmesg (the guest installs its own handler), an armed-but-silent fault
# witness, a guest that masks SIGSEGV on that path, and a gdb catch waiting for a signal the guest swallows. The
# core is the only artifact that still has the thread in it -- and it is OFF by default here (ulimit -c 0 plus a
# core_pattern that pipes to apport), so the no-core case is the COMMON case and the command must explain it.
#
# This contract generates its own core from a throwaway host process, with the core_pattern changed and restored
# around it, so it needs no guest, no prefix and no Darling build.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DWDIAG="$ROOT/scripts/dwdiag"
DIR=$(mktemp -d /tmp/dwdiag-core-contract-XXXX)
ORIG=$(cat /proc/sys/kernel/core_pattern)
cleanup() {
	sudo -n sh -c "echo '$ORIG' > /proc/sys/kernel/core_pattern" 2>/dev/null || true
	rm -rf "$DIR"
}
trap cleanup EXIT

fail=0
note() { echo "core-contract: $1"; }
check() {
	if echo "$3" | grep -qE "$1"; then note "ok: $2"; else note "FAIL: missing $2   (wanted: $1)"; fail=1; fi
}

# --- the no-core case, which is the DEFAULT state of this machine
empty=$(mktemp -d /tmp/dwdiag-core-empty-XXXX)
none_out=$("$DWDIAG" core --dir "$empty" 2>&1)
check "CORE-NONE" "a missing core is reported" "$none_out"
check "core_pattern=" "and the reason is named, not left to the reader" "$none_out"
rmdir "$empty" 2>/dev/null

# --- a real core
"$DWDIAG" cores on --dir "$DIR" >/dev/null 2>&1 || { note "SKIP: cannot set core_pattern (no passwordless sudo)"; exit 0; }
check "CORES-PATTERN $DIR/core" "the tool's own cores on sets the pattern" "$("$DWDIAG" cores on --dir "$DIR" 2>&1)"
( ulimit -c unlimited; sh -c 'kill -ILL $$' 2>/dev/null )
sleep 1
core=$(ls -t "$DIR"/core.* 2>/dev/null | head -1)
if [ -z "$core" ]; then
	note "FAIL: no core was written even with core_pattern pointing into $DIR"
	note "  pattern now: $(cat /proc/sys/kernel/core_pattern)"
	exit 1
fi

out=$("$DWDIAG" core --core "$core" 2>&1)
check "CORE file=.*size=[0-9]+" "the core is read and its size reported" "$out"
check "CORE-SIGNAL .*SIGILL" "the signal is read out of NT_SIGINFO" "$out"
check "CORE-REGISTERS rip=0x[0-9a-f]+ rsp=0x[0-9a-f]+" "the registers are read out of NT_PRSTATUS" "$out"
check "CORE-GENERATED-BY" "the generating command is reported" "$out"

# Mappings come from NT_FILE, and the image is named WITHOUT --prefix: the paths a guest reports are de-virtualized
# (`/Volumes/SystemRoot/...` -> prefix), and a host core's paths are already real. This replaced an expectation that
# resolution needed --prefix, which was true only while mappings were read from gdb -- and gdb lists NO mappings for
# a core whose image paths do not exist, which is exactly the guest case.
check "CORE-MAPPINGS source=NT_FILE count=[0-9]+" "mappings are read from NT_FILE, not gdb" "$out"
check "CORE-FAULT image=/.*file\+0x[0-9a-f]+ vm\+0x[0-9a-f]+" "the faulting image is opened and BOTH the file offset and the VM address are named" "$out"
check "CORE-STACK frames=[0-9]+" "the whole stack is symbolized, not just the top frame" "$out"
# A frame that resolves must carry BOTH offsets: a file offset alone is a confidently wrong function name. A host
# core may resolve no frames at all (the throwaway `sh` here has none), so this asserts the invariant rather than a
# count -- the number of frames with `file+` must equal the number with `file+` AND `vm+`.
frames_with_file=$(printf '%s\n' "$out" | grep -cE '^  #[0-9]+ .*file\+0x')
frames_with_both=$(printf '%s\n' "$out" | grep -cE '^  #[0-9]+ .*file\+0x[0-9a-f]+ vm\+0x')
check "^$" "every resolved frame carries both the file offset and the VM address (with_file=$frames_with_file with_both=$frames_with_both)" ""
if [ "$frames_with_file" != "$frames_with_both" ]; then
	note "FAIL: $frames_with_file frame(s) carry file+ but only $frames_with_both carry vm+"
	fail=1
fi

xml=$("$DWDIAG" core --core "$core" --json 2>&1)
check '"rip":"0x' 'the machine-readable form carries the registers' "$xml"

# --- what ELSE the core says: the faulting thread, the instruction, the operands, and the caller chain.
#
# Every one of these was derived by hand for the libdispatch SIGILL (a python script over NT_FILE, a gdb round for
# the register file, `readelf -n` REFUSING the note on this host), and two of those hand steps produced a wrong answer
# once. The list is checked on a core THIS contract makes, so a regression fails here and not in an investigation.
check "CORE-THREAD faulting=lwp [0-9]+ of [0-9]+ thread" "the faulting thread is named" "$out"
check "CORE-MAPPINGS source=NT_FILE count=[0-9]+ range-scale=[a-z]+ .*file-offset-unit=" "the mapping table reports the unit it normalized" "$out"
check "CORE-INSN " "the instruction at the faulting address is reported" "$out"
check "CORE-STACK frames=[0-9]+ .*core's own NT_FILE" "the walk says it mapped through the core's own table" "$out"
check "CORE-STACK-STOP .+" "the walk reports WHY it stopped instead of ending silently" "$out"
# The operands are BEST-EFFORT by construction, so the contract accepts either answer -- but never an empty one: a
# `CORE-OPERANDS unavailable:` with no reason would be the silent guess this checking exists to prevent.
check "CORE-OPERANDS (source=registers-at-trap|unavailable: .+)" "the once operands are either located with their rule or refused with a reason" "$out"
if printf '%s\n' "$out" | grep -q 'CORE-OPERANDS source=registers-at-trap'; then
	check "CORE-OPERANDS predicate_reg=.*once_word=0x[0-9a-f]+ .*masked_owner=0x[0-9a-f]+" "a located operand set names every register it came from" "$out"
fi
check "CORE-OPERANDS-TSD3 gs_base=0x[0-9a-f]+ slot=3 value=(0x[0-9a-f]+|UNREADABLE \()" "the thread's TSD slot 3 is read or its failure is stated" "$out"

# The machine-readable form has to be PARSEABLE, not merely greppable: a consumer that cannot parse it is worse than
# no JSON at all, and the previous hand-built object is exactly where an unquoted value got in. The last line is the
# JSON (`$xml` also carries the shim's own stderr line, which is useful in a failure message and fatal to a parser).
printf '%s\n' "$xml" | tail -1 | python3 -c '
import json,sys
d=json.load(sys.stdin)
for k in ("core","rip","rsp","signal","siginfo","faulting_lwp","threads","insn","trap","operands","tsd_self","frames","callers","callers_stop","mappings","range_scale","generated_by"):
    assert k in d, "missing key "+k
assert isinstance(d["callers"], list) and len(d["callers"]) == len(d["frames"]), "callers must be one per addressed frame"
assert all((c["image"] is None) == (c["image_addr"] is None) for c in d["callers"]), "an image and its in-image address travel together"
assert isinstance(d["tsd_self"], dict) and d["tsd_self"]["slot"] == 3, "tsd_self names the slot it read"
assert d["operands"] is None or "once_word" in d["operands"], "operands are either null or a located set"
print("core-contract: ok: the JSON parses and its arrays agree with each other")
' || fail=1

# --- the LOG and the CORE must be cross-checked, not assumed to agree. The core's own ip and lwp are read out of
# its output, so this pair of assertions cannot pass by hardcoding an agreement.
rip=$(printf '%s\n' "$out" | grep -oE 'CORE-REGISTERS rip=0x[0-9a-f]+' | head -1 | cut -d= -f2)
flwp=$(printf '%s\n' "$out" | grep -oE 'faulting=lwp [0-9]+' | head -1 | cut -d' ' -f2)
if [ -n "$rip" ] && [ -n "$flwp" ]; then
	agree="$DIR/agree.log"
	printf '[sigexc-fatal sig=4 code=2 addr=%s pid=1 tid=%s]\n' "$rip" "$flwp" >"$agree"
	check "CORE-LOG .*AGREE" "a log whose mark names the core's own ip and lwp is confirmed to agree" "$("$DWDIAG" core --core "$core" --log "$agree" 2>&1)"
	check "CORE-THREAD faulting=lwp $flwp .*the log's own tid=" "the log's tid is what selects the thread" "$("$DWDIAG" core --core "$core" --log "$agree" 2>&1)"
	other="$DIR/other.log"
	printf '[sigexc-fatal sig=11 code=1 addr=0xdeadbeef pid=1 tid=1]\n' >"$other"
	check "CORE-LOG .*DIFFER" "a log whose mark names a DIFFERENT fault is reported as not describing this core" "$("$DWDIAG" core --core "$core" --log "$other" 2>&1)"
else
	note "SKIP: could not read rip/lwp out of the core's output to build the log cross-check"
fi

# --- `crash` on a log whose marks show a signal death must NAME the core the run left, DISCOVERED from core_pattern
# (this contract points the pattern at its own $DIR, so a hardcoded /tmp/t4cores or a guessed name fails here).
BIN="$ROOT/tools/darling-debug-runner/target/release/darling-debug-runner"
if [ -x "$BIN" ]; then
	crashlog="$DIR/crash.log"
	printf '[sigexc-fatal sig=4 code=2 addr=0x7f3d00001234 pid=1234 tid=1234]\n[sigexc-fault-map 7f3d00000000-7f3d00020000 r-xp 00000000 08:01 1 %s]\n' "$BIN" >"$crashlog"
	crash_out=$("$DWDIAG" crash --log "$crashlog" 2>&1)
	check "CRASH-CORE pattern=$DIR/core" "the core pattern is read, not assumed" "$crash_out"
	check "CRASH-CORE newest=$DIR/core\." "the core found is the one under the pattern's own directory" "$crash_out"
	check "CRASH-CORE-NEXT .*dwdiag core --core $DIR/core\..*--log $crashlog" "the next command names that core and passes the log through" "$crash_out"
	check "CRASH-CORE newest=.*(newer|OLDER) than the log" "whether the core can be this run's is stated, not assumed" "$crash_out"
	crash_json=$("$DWDIAG" crash --log "$crashlog" --json 2>/dev/null | tail -1)
	printf '%s\n' "$crash_json" | python3 -c '
import json,sys
d=json.load(sys.stdin)
c=d["core"]
assert c["pattern"].endswith("core.%p"), c
assert c["newest"].startswith(c["dir"]+"/"), c
assert isinstance(c["newest_is_newer_than_the_log"], bool)
print("core-contract: ok: the crash JSON carries the discovered core")
' || fail=1
else
	note "SKIP: no release binary at $BIN to symbolize the fake log against"
fi

# --- the artifact this tool was built for: a GUEST core. It is 670 MB and lives in /tmp, so it is NEVER committed:
# it is an OPTIONAL input, and everything above runs without it.
#
#   DWDIAG_GUEST_CORE=/tmp/t4cores/core.127004 bash tests/run-core-contract.sh
#
# What follows asserts the values the hand diagnosis recorded for that crash (bead dar-liaq). WITHOUT the variable
# this contract covers everything except that one artifact's arithmetic and symbolization.
if [ -n "${DWDIAG_GUEST_CORE:-}" ] && [ -r "$DWDIAG_GUEST_CORE" ]; then
	gout=$("$DWDIAG" core --core "$DWDIAG_GUEST_CORE" 2>&1)
	check "CORE-FAULT image=.*libdispatch\.dylib .*image_addr=0x6c1dd symbol=_dispatch_once_wait \+ 0x17d" "the guest fault resolves to libdispatch at image address 0x6c1dd" "$gout"
	check "CORE-INSN 0f 0b .*ud2" "the instruction at that address is the deliberate ud2" "$gout"
	check "CORE-OPERANDS .*once_word=0x101 .*masked_owner=0x100" "the once word and the masked owner are read off the trap" "$gout"
	check "CORE-OPERANDS-TSD3 .*value=0x103" "the faulting thread's TSD slot 3 is read" "$gout"
	check "dispatch_once_f \+ 0x122" "the caller chain reaches dispatch_once_f" "$gout"
	check "dispatch_once \+ 0x25" "and dispatch_once, its caller" "$gout"
	check "DVTFoundation .*-\[DVTToolsInfo \+ 0x1b" "and on into DVTFoundation, as the hand diagnosis found" "$gout"
else
	note "SKIP: set DWDIAG_GUEST_CORE=<a guest core> to also check the real crash's image address, ud2, operands and caller chain"
fi

echo "$out" | head -4
if [ "$fail" -ne 0 ]; then note "FAIL"; exit 1; fi
note "PASS"
exit 0
