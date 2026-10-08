#!/bin/bash
# Contract for `dwdiag trap`: the kernel's own report of where a guest died must be parsed WHOLE, in both
# shapes the kernel writes, and symbolized only when a prefix is given.
#
# Why this exists. The command was added after a session in which one `dmesg` line named the faulting image,
# its load base and the instruction pointer while three custom probes, a gdb catch and a stack scanner had
# produced a story that was wrong twice. A parser that silently handles only the "traps: ... ip:HEX" shape and
# drops the "NAME[PID]: segfault at HEX ip HEX" shape would reintroduce exactly that silence, so the fixture
# carries one line of each and the assertions name both.
set -u

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
DWDIAG="$ROOT/scripts/dwdiag"
LOG=$(mktemp /tmp/dwdiag-trap-contract-XXXX.log)
trap 'rm -f "$LOG"' EXIT

cat >"$LOG" <<'EOF'
[  100.000000] unrelated kernel line about eth0
[  123.456789] traps: ld[4242] trap invalid opcode ip:7f0000001abc sp:7fff0000 error:0 in libsystem_kernel.dylib[7f0000000000+9d000]
[  124.000000] traps: other[7] trap invalid opcode ip:1000 sp:2000 error:0 in not_an_image[1000+10]
[  125.000000] ld[4243]: segfault at 7f0000002abc ip 7f0000002abc sp 7fff0000 error 4 in libsystem_kernel.dylib[7f0000000000+9d000]
EOF

fail=0
note() { echo "trap-contract: $1"; }
check() {
	if echo "$3" | grep -q "$1"; then
		note "ok: $2"
	else
		note "FAIL: missing $2   (wanted: $1)"
		fail=1
	fi
}
absent() {
	if echo "$3" | grep -q "$1"; then
		note "FAIL: unexpected $2   (matched: $1)"
		fail=1
	else
		note "ok: no $2"
	fi
}

out=$("$DWDIAG" trap --kernel-log "$LOG" 2>&1)
rc=$?
if [ "$rc" -ne 0 ]; then
	note "FAIL: dwdiag trap exited $rc"
	echo "$out" | tail -5
	exit 1
fi

check "TRAP-CENSUS .* reported=3" "all three fault lines are parsed" "$out"
check "TRAP pid=4242 name=ld reason=trap image=libsystem_kernel.dylib base=0x7f0000000000 .* off=0x1abc" \
	"the traps: shape is parsed whole, including image, base and offset" "$out"
check "TRAP pid=4243 name=ld reason=segfault .* off=0x2abc" \
	"the segfault shape -- no traps: prefix, pid followed by a colon, ip with no '=' -- is parsed too" "$out"
check "TRAP-NOTE" "the symbolizer's 'nearest symbol at or below' limit is stated, not left to the reader" "$out"

# An image that cannot be found under the given prefix must be REPORTED, never silently symbolized from
# whatever happened to match by name.
withpfx=$("$DWDIAG" trap --kernel-log "$LOG" --prefix /tmp/definitely-not-a-prefix 2>&1)
check "<image not found under the prefix>" "an unresolvable image is reported as such" "$withpfx"

sel=$("$DWDIAG" trap --kernel-log "$LOG" --process other 2>&1)
check "TRAP pid=7 name=other" "--process selects the named process" "$sel"
absent "TRAP pid=4242" "--process excludes the others" "$sel"

one=$("$DWDIAG" trap --kernel-log "$LOG" --limit 1 2>&1)
check "TRAP pid=4243" "--limit keeps the NEWEST line" "$one"
absent "TRAP pid=4242" "--limit drops the older ones" "$one"

# No prefix anywhere: the address must still be reported, because that is the fact that identifies the image.
noprefix=$(DWDIAG_PREFIX= "$DWDIAG" trap --kernel-log "$LOG" --limit 1 2>&1)
check "off=0x2abc" "without a prefix the offset is still reported" "$noprefix"
check "no prefix" "and the missing prefix is named rather than guessed" "$noprefix"

echo "$out" | tail -3
if [ "$fail" -ne 0 ]; then
	note "FAIL"
	exit 1
fi
note "PASS"
exit 0
