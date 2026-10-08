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
sudo -n sh -c "echo '$DIR/core.%p' > /proc/sys/kernel/core_pattern" || { note "SKIP: cannot set core_pattern (no passwordless sudo)"; exit 0; }
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

# Without --prefix the faulting image cannot be opened, and that must be SAID rather than answered with silence.
check "CORE-FAULT unresolved" "an unresolvable image says so" "$out"

xml=$("$DWDIAG" core --core "$core" --json 2>&1)
check '"rip":"0x' 'the machine-readable form carries the registers' "$xml"

echo "$out" | head -4
if [ "$fail" -ne 0 ]; then note "FAIL"; exit 1; fi
note "PASS"
exit 0
