#!/bin/sh
# dserver-crash-resolve.sh -- turn a `dserver-CRASH` line into a LOCATION and the CODE AROUND IT.
#
# WHY THIS EXISTS. The crash probe prints `sig=`, `addr=`, `self=` (the address of a known symbol in the same image)
# and `pc=` with raw writes only, because a server that dies silently is an instrument that cannot answer. What it does
# NOT do is resolve those numbers, and MEASURED: the same crash was resolved by hand with a throwaway python + llvm-nm
# snippet five times in one session -- each time re-deriving the delta and re-searching the symbol table, and each time
# risking a different (wrong) answer. The numbers are already unique; the derivation is what must not be improvised.
#
# `self` is what makes the runtime pc usable under ASLR: offset = pc - self + addr(self in file), and the offset in the
# file is what llvm-nm and objdump can be asked about.
#
# Usage:
#   dserver-crash-resolve.sh --binary PATH --log LOG [--context BYTES]
#   dserver-crash-resolve.sh --binary PATH --self 0x... --pc 0x...
# Prints: the crash line, the resolved symbol + offset, and the disassembly around the faulting instruction.
set -u

BINARY=""
LOG=""
SELFV=""
PCV=""
CTX=160

while [ $# -gt 0 ]; do
	case "$1" in
		--binary) BINARY="$2"; shift 2 ;;
		--log)    LOG="$2"; shift 2 ;;
		--self)   SELFV="$2"; shift 2 ;;
		--pc)     PCV="$2"; shift 2 ;;
		--context) CTX="$2"; shift 2 ;;
		*) echo "unknown argument: $1" >&2; exit 2 ;;
	esac
done
[ -n "$BINARY" ] || { echo "usage: $0 --binary PATH (--log LOG | --self 0x.. --pc 0x..)" >&2; exit 2; }
[ -f "$BINARY" ] || { echo "no such binary: $BINARY" >&2; exit 2; }

LINE=""
if [ -n "$LOG" ]; then
	LINE=$(grep -a -o 'dserver-CRASH[^|]*' "$LOG" | head -1)
	[ -n "$LINE" ] || { echo "NO CRASH LINE in $LOG"; exit 1; }
	echo "crash: $LINE"
	SELFV=$(printf '%s\n' "$LINE" | sed -n 's/.*self=\([0-9a-f]*\).*/\1/p')
	PCV=$(printf '%s\n' "$LINE" | sed -n 's/.*pc=0x\([0-9a-f]*\).*/\1/p')
fi
[ -n "$SELFV" ] && [ -n "$PCV" ] || { echo "need both self= and pc= (got self='$SELFV' pc='$PCV')" >&2; exit 2; }

python3 - "$BINARY" "$SELFV" "$PCV" "$CTX" "$LINE" <<'PY'
import re, subprocess, sys
binary, selfv, pcv, ctx = sys.argv[1], int(sys.argv[2], 16), int(sys.argv[3], 16), int(sys.argv[4])
line = sys.argv[5] if len(sys.argv) > 5 else ''

out = subprocess.run(['llvm-nm', '-n', binary], capture_output=True, text=True).stdout
syms = []
for line in out.splitlines():
    p = line.split()
    if len(p) >= 3:
        try:
            syms.append((int(p[0], 16), p[2]))
        except ValueError:
            continue

# the probe reports the address of a KNOWN symbol; find it in the table and use it as the origin
probe = [(a, n) for a, n in syms if 'dserver_crash_probe' in n]
if not probe:
    print('cannot find dserver_crash_probe in the symbol table: cannot derive the offset')
    sys.exit(3)
file_self = probe[0][0]
delta = pcv - selfv
target = file_self + delta

best = None
for a, n in syms:
    if a <= target and (best is None or a > best[0]):
        best = (a, n)
print('self=0x%x pc=0x%x delta=0x%x -> target=0x%x' % (selfv, pcv, delta, target))
print('LOCATION: %s + 0x%x' % (best[1], target - best[0]))

lo = max(0, target - ctx)
hi = target + ctx
dis = subprocess.run(['objdump', '-d', '--start-address=0x%x' % lo, '--stop-address=0x%x' % hi, binary],
                     capture_output=True, text=True).stdout
# mark the faulting line so the location inside the block is unambiguous
# the mini stack walk: every word that lies inside this image is a code address and resolves like `pc` does
words = []
for i in range(8):
    m = re.search(r',w%d=0x?([0-9a-f]+)' % i, line)
    if m:
        words.append((i, int(m.group(1), 16)))
if words:
    print('--- stack walk (words that resolve inside this image are callers) ---')
    for i, w in words:
        if w == 0:
            continue
        d = w - selfv
        if 0 <= d < 0x800000:
            t = file_self + d
            b = None
            for a, n in syms:
                if a <= t and (b is None or a > b[0]):
                    b = (a, n)
            print('  w%d=0x%x -> %s + 0x%x' % (i, w, b[1], t - b[0]))

print('--- disassembly around the faulting instruction ---')
for line in dis.splitlines():
    mark = ''
    m = line.strip().split(':')
    if m and m[0].strip():
        try:
            if int(m[0].strip(), 16) == target:
                mark = '   <=== FAULT HERE'
        except ValueError:
            pass
    print(line.replace('\t', ' ') + mark)
PY
