#!/bin/sh
# darling-describe-artifact.sh -- answer, from the BINARY alone, the questions that cost this investigation the
# most rounds:
#
#   1. WHERE does this image start? (LC_MAIN -> file offset -> the symbol it lands on)
#   2. Is the symbol I care about actually in this image, and where in the FILE?
#   3. Is the probe/tag I added really inside this artifact, and in which byte range?
#   4. Does the runtime load THIS file, or another copy of it?
#
# WHY THIS EXISTS. Two rounds were lost to probes that were not in the artifact under test, and one whole boot
# failure came from a probe that clobbered a live register -- in both cases the answer was visible by disassembling
# the SHIPPED binary, and in neither case could another run have produced it. Every question above is answerable
# from the file; the tool exists so that asking is one command instead of an improvised pipeline.
#
# Usage:
#   darling-describe-artifact.sh [--prefix PATH] FILE [SYMBOL]
#   darling-describe-artifact.sh --prefix PATH --tag '[my-tag]'   # tag presence in every runtime copy
#
# llvm tools are looked up on PATH and in the LLVM install prefix, because llvm-otool is usually NOT on PATH while
# llvm-nm and llvm-objdump are.

set -u

PREFIX=""
TAG=""
FILE=""
SYMBOL=""

while [ $# -gt 0 ]; do
	case "$1" in
		--prefix) PREFIX="$2"; shift 2 ;;
		--tag) TAG="$2"; shift 2 ;;
		-h|--help) sed -n '2,22p' "$0"; exit 0 ;;
		*) if [ -z "$FILE" ]; then FILE="$1"; elif [ -z "$SYMBOL" ]; then SYMBOL="$1"; else echo "unexpected: $1" >&2; exit 2; fi; shift ;;
	esac
done

find_tool() {
	for p in "$1" /usr/lib/llvm-*/bin/"$1" /usr/bin/"$1"; do
		[ -x "$p" ] && { echo "$p"; return 0; }
	done
	return 1
}

OTOOL=$(find_tool llvm-otool 2>/dev/null || true)
NM=$(find_tool llvm-nm || true)
OBJDUMP=$(find_tool llvm-objdump || true)

[ -n "$FILE" ] && [ -f "$FILE" ] || { echo "no such file: ${FILE:-<none>}" >&2; exit 2; }

echo "artifact: $FILE"
echo "sha256:   $(sha256sum "$FILE" | cut -c1-64)"
echo "size:     $(stat -c%s "$FILE")"

if [ -n "$TAG" ]; then
	# A tag assembled at runtime from two literals cannot be found this way -- that is exactly why the probe
	# header requires a SINGLE literal. Offsets are given so the tag can be located relative to the code region.
	# -F is mandatory here: a tag contains '[' and ']', so as a regex it is a bracket expression and grep
	# rejects it with "Invalid range end" -- which, wrapped in `|| echo 0`, reads as "not in this artifact".
	out=$(LC_ALL=C grep -a -c -F -- "$TAG" "$FILE" 2>&1)
	rc=$?
	if [ "$rc" -ge 2 ]; then
		echo "grep ERROR (rc=$rc): $out" >&2
		exit 1
	fi
	n=$out
	echo "tag:      $TAG"
	if [ "$n" = 0 ]; then
		echo "tag count: 0  <-- NOT IN THIS ARTIFACT: a probe on this tag cannot fire, whatever the source says"
	else
		echo "tag count: $n"
		LC_ALL=C grep -a -b -o -F -- "$TAG" "$FILE" 2>/dev/null | while IFS=: read -r off _; do
			printf 'tag at file offset 0x%x\n' "$off"
		done
	fi
fi

if [ -n "$OTOOL" ]; then
	hdr=$($OTOOL -hv "$FILE" 2>/dev/null | sed -n '3p')
	[ -n "$hdr" ] && echo "mach-o:  $hdr"

	# __TEXT geometry: LC_MAIN's entryoff is a FILE offset, so the address is vmaddr + (entryoff - fileoff).
	# Parse the FIRST segment named exactly __TEXT from its own block. A flat pattern match over the whole
	# LC_SEGMENT list picks up a later segment's vmaddr/fileoff and can still produce a self-consistent-looking
	# address when the two errors cancel -- which is worse than being wrong loudly.
	text=$($OTOOL -l "$FILE" 2>/dev/null | awk '
		/^Load command/ {seg=""; vmaddr=""; fileoff=""}
		/segname __TEXT$/ && seg=="" {seg="want"}
		seg=="want" && /^ *segname/ {seg="field"}
		seg=="field" && /^ *vmaddr/ {vmaddr=$2}
		seg=="field" && /^ *fileoff/ {print vmaddr, $2; exit}')
	entry=$($OTOOL -l "$FILE" 2>/dev/null | awk '/LC_MAIN/{m=1; next} m && /entryoff/ {print $2; m=0}')
	if [ -n "$entry" ]; then
		printf 'LC_MAIN: entryoff %s (0x%x)\n' "$entry" "$entry"
		if [ -n "$text" ]; then
			set -- $text
			printf '  -> address 0x%x (__TEXT vmaddr %s + entryoff - fileoff %s)\n' \
				"$(($1 + $entry - $2))" "$1" "$2"
		fi
	fi
else
	echo "(no llvm-otool: Mach-O header not decoded)"
fi

if [ -n "$SYMBOL" ] && [ -n "$NM" ]; then
	line=$($NM -n "$FILE" 2>/dev/null | awk -v s="$SYMBOL" '$3 == s {print; exit}')
	if [ -z "$line" ]; then
		echo "symbol:  $SYMBOL NOT FOUND in this artifact"
	else
		addr=$(echo "$line" | awk '{print $1}')
		printf 'symbol:  %s at 0x%s  (%s)\n' "$SYMBOL" "$addr" "$line"
		if [ -n "$OBJDUMP" ]; then
			echo "--- disassembly around $SYMBOL ---"
			$OBJDUMP --macho -d --start-address=0x"$addr" --stop-address=$((0x$addr + 160)) "$FILE" 2>/dev/null | tail -n +7
		fi
	fi
fi

if [ -n "$PREFIX" ]; then
	echo "--- copies under $PREFIX ---"
	found=0
	for f in $(find "$PREFIX" -name "$(basename "$FILE")" -type f 2>/dev/null); do
		found=1
		s=$(sha256sum "$f" | cut -c1-16)
		if [ "$(sha256sum "$f" | cut -c1-64)" = "$(sha256sum "$FILE" | cut -c1-64)" ]; then
			echo "  SAME as this file: $f"
		else
			echo "  DIFFERENT ($s):   $f  <-- a runtime path holding a stale component"
		fi
	done
	[ "$found" = 0 ] && echo "  none (this component is not deployed under the prefix)"
fi
