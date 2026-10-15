#!/bin/bash
# Contract test for the xtrace inline excerpt (source: $DARLING/src/xtrace/tests/excerpt_contract.cpp).
#
# HOST-SIDE ON PURPOSE. The defect was in the CONVERSION the excerpt used: xtrace::String::append_format
# goes through __simple_vsnprintf, which implements NEITHER a width NOR a precision field, so the old
# `"%.*s"` rendered every excerpt as the literal debris `*s`. MEASURED: 33 of the 33 inline-data records
# in the parked arm's own capture (run fcd-fix18-bytes) end in `"*s"`. The test therefore has to run the
# real formatter, the real String and the real renderer, under the host compiler.
#
# WHAT IS REAL HERE, and what is not (there is no prefix-backed run in this script):
#   * REAL: src/xtrace/excerpt.cpp, src/xtrace/string.cpp, and libsystem_kernel's own
#     emulation/src/common/simple.c -- the formatter, compiled as C with the host gcc.
#   * STUBBED: only the guest-only HEADERS simple.c and string.cpp include, which the formatter does not
#     take behaviour from. Their contents are printed below and are: <stdint.h>/<stddef.h> for base.h,
#     <stdarg.h> plus the real prototype for simple.h, a no-op syscall macro for the Linux syscall
#     header, and empty files for the four Mach/rpc headers. The .c files themselves are untouched.
#   * The guest library cannot be linked on the host at all (Darwin ABI, allocator from _elfcalls), so the
#     test supplies xtrace_malloc/free/realloc -- a three-line allocator, not a behaviour.
set -u

DARLING=${DARLING:-/home/ilyagulya/work/wr-fresh/darling}
X="$DARLING/src/xtrace"
EMU="$DARLING/src/external/xnu/darling/src/libsystem_kernel/emulation"
OUT=${1:-$(mktemp -d /tmp/xtrace-excerpt-contract.XXXXXX)}
SHIM="$OUT/shim"; STUB="$OUT/stub"
rm -rf "$SHIM" "$STUB" "$OUT"/*.o "$OUT/contract"
mkdir -p "$SHIM/darling/emulation" "$STUB"

# The guest tree addresses its headers as <darling/emulation/...>; the Darling build satisfies that with an
# include root holding a `darling/` directory, so this one is a symlink tree into the REAL headers.
for d in "$EMU"/include/*/; do
	[ -d "$d" ] || continue
	ln -sfn "${d%/}" "$SHIM/darling/emulation/$(basename "${d%/}")"
done

stub() { mkdir -p "$STUB/$(dirname "$1")"; cat > "$STUB/$1"; }
# NOTE: base.h and common/simple.h are NOT stubbed -- the real ones are used, because the formatter's
# neighbours in simple.c need the type they declare (struct simple_readline_buf) and because the real
# prototype is part of what is compiled against.
stub darling/emulation/linux_premigration/linux-syscalls/linux.h <<'EOF'
/* stub: the real header only selects an arch syscall-number table, which the host does not have. The
   formatter under test needs no syscall number; __simple_printf (its only user here) is dropped by
   --gc-sections, so the macro may be a no-op. */
#define __NR_write 1
#define LINUX_SYSCALL0(...) 0
#define LINUX_SYSCALL1(...) 0
#define LINUX_SYSCALL2(...) 0
#define LINUX_SYSCALL3(...) 0
#define LINUX_SYSCALL4(...) 0
#define LINUX_SYSCALL5(...) 0
#define LINUX_SYSCALL6(...) 0
EOF
stub darling/emulation/other/mach/lkm.h <<'EOF'
/* stub: guest-only */
EOF
stub darling/emulation/xnu_syscall/bsd/impl/signal/kill.h <<'EOF'
/* stub: guest-only */
EOF
stub darling/emulation/xnu_syscall/bsd/impl/unistd/getpid.h <<'EOF'
/* stub: guest-only */
EOF
stub darlingserver/rpc.h <<'EOF'
/* stub: guest-only */
EOF

echo "STUBS (headers only; the .c files under test are the tree's own):"
(cd "$STUB" && find . -name '*.h' | sed 's|^\./|  |')

# The stub directory comes FIRST so that a stubbed guest-only header wins over the symlinked real tree;
# every header that is not stubbed still resolves to the real one through the symlinks. The four
# `-include`s are the C library headers the guest build reaches through its SDK headers (the tree's own
# base.h leaves them to the SDK); they are the host's own and change no behaviour under test.
CFLAGS="-O0 -w -ffunction-sections -fdata-sections -I$STUB -I$SHIM -include stdint.h -include stddef.h -include stdarg.h"
set -x
gcc -std=gnu11 $CFLAGS -c -o "$OUT/simple.o" "$EMU/src/common/simple.c" || exit 2
g++ -std=c++11 -DXTRACE_CPP $CFLAGS -c -o "$OUT/excerpt.o" "$X/excerpt.cpp" || exit 2
g++ -std=c++11 -DXTRACE_CPP $CFLAGS -c -o "$OUT/string.o" "$X/string.cpp" || exit 2
g++ -std=c++11 -DXTRACE_CPP $CFLAGS -c -o "$OUT/test.o" "$X/tests/excerpt_contract.cpp" || exit 2
g++ -o "$OUT/contract" "$OUT/test.o" "$OUT/excerpt.o" "$OUT/string.o" "$OUT/simple.o" -Wl,--gc-sections || exit 2
set +x

echo "artifact: $OUT/contract"
echo "formatter symbol taken from the tree's simple.c: $(nm --defined-only "$OUT/simple.o" | grep -c __simple_vsnprintf) definition(s)"
"$OUT/contract"
rc=$?
echo "CONTRACT-RC=$rc (0 = the excerpt agrees with the bytes it claims to render)"
exit $rc
