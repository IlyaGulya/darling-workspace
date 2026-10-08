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

echo "$out" | head -4
if [ "$fail" -ne 0 ]; then note "FAIL"; exit 1; fi
note "PASS"
exit 0
