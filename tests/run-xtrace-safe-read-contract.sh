#!/usr/bin/env bash
# Host contract for the FAULT-SAFE READER the xtrace instrument uses to read the traced programme's memory.
#
# MEASURED DEFECT this pins (follow-up 8, 2026-10-15). A descriptor's address was dereferenced on a formatting path
# inside `print_mach_msg`, and the traced arm died with SIGSEGV three seconds in: arm `RC=139`, core
# `/tmp/t4cores/core.2915140`, and the run that was being measured was lost. The instrument killed its own subject.
# A bound on the LENGTH cannot prevent that -- the BASE can be stale -- so every read of foreign memory goes
# through `src/xtrace/safe_read.cpp`, which reads with a raw `process_vm_readv` (a syscall cannot raise) and reports
# `XTRACE_READ_UNREADABLE` when the range is not mapped.
#
# Three arms, and the third is the RED PROOF: the SAME address, read by a driver that dereferences it directly,
# must still die. Without that arm this contract would pass against a reader that never reads anything.
#
# The reader is compiled straight from the darling source forest (the same file the dylib builds), because a
# re-implementation here would prove nothing about the instrument. DARLING_SOURCE=/path/to/darling (default:
# /home/ilyagulya/work/wr-fresh/darling; the workspace's canonical forest).
set -uo pipefail

src="${DARLING_SOURCE:-/home/ilyagulya/work/wr-fresh/darling}"
reader="$src/src/xtrace/safe_read.cpp"
header="$src/src/xtrace/safe_read.h"
if [ ! -f "$reader" ] || [ ! -f "$header" ]; then
	# UNMEASURED, never a pass: a contract that cannot see the file it judges must not report success.
	echo "UNMEASURED: no xtrace safe reader at $reader (set DARLING_SOURCE); nothing about fault-safety was measured" >&2
	exit 77
fi
cxx="${CXX:-c++}"
command -v "$cxx" >/dev/null || { echo "UNMEASURED: no C++ compiler ($cxx) to build the reader with" >&2; exit 77; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/driver.cpp" <<'DRIVER'
// Exercises safe_read.cpp against REAL mappings: one mapped page, one unmapped page, and a string that ends
// exactly where an unmapped page begins.
#include "safe_read.h"
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <sys/mman.h>
#include <unistd.h>

static void* unmapped_address() {
	// A page the process does not own. 0x1000 is unmapped in every Linux process (the loader refuses such a base),
	// and a /proc/self/maps check in the driver would only test the driver.
	void* p = mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (p == MAP_FAILED) { perror("mmap"); exit(3); }
	munmap(p, 4096);
	return p;
}

int main(int argc, char** argv) {
	if (argc > 1 && strcmp(argv[1], "--raw") == 0) {
		// THE RED ARM: the same read, done the way the instrument used to do it.
		const char* p = (const char*) unmapped_address();
		volatile char c = *p;
		printf("raw dereference returned %d (this must never be reached)\n", (int) c);
		return 0;
	}

	char* scratch = xtrace_safe_scratch();
	if (scratch == nullptr) { printf("FAIL scratch\n"); return 1; }

	// 1. MAPPED: the bytes come back.
	const char* text = "hello-from-a-mapped-page";
	const size_t text_len = strlen(text);
	int err = 0;
	size_t n = xtrace_safe_copy(scratch, text, text_len, &err);
	if (n != text_len || memcmp(scratch, text, text_len) != 0) { printf("FAIL mapped n=%zu\n", n); return 1; }
	printf("OK mapped bytes=%zu\n", n);

	// 2. UNMAPPED: reported, and the process is still here to report it.
	err = 0;
	n = xtrace_safe_copy(scratch, unmapped_address(), 64, &err);
	if (n != XTRACE_READ_UNREADABLE) { printf("FAIL unmapped n=%zu (a load from it would have killed this process)\n", n); return 1; }
	printf("OK unmapped errno=%d\n", err);

	// 3. PARTLY MAPPED: the readable prefix is reported as a short copy, never as the whole range.
	void* two = mmap(nullptr, 8192, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (two == MAP_FAILED) { perror("mmap"); return 3; }
	memset(two, 'A', 8192);
	munmap((char*) two + 4096, 4096);
	err = 0;
	n = xtrace_safe_copy(scratch, two, 8192, &err);
	if (n == XTRACE_READ_UNREADABLE || n >= 8192) { printf("FAIL partial n=%zu\n", n); return 1; }
	printf("OK partial readable=%zu errno=%d\n", n, err);

	// 4. A STRING WHOSE END IS NOT MAPPED: unreadable, not a truncated string. Taken BEFORE the boundary mapping
	//    below, because a freed page can be handed straight back by the next mmap.
	err = 0;
	n = xtrace_safe_read_c_string(scratch, XTRACE_STRING_MAX, (const char*) two, &err);
	if (n != XTRACE_READ_UNREADABLE) { printf("FAIL unterminated n=%zu\n", n); return 1; }
	printf("OK unterminated errno=%d\n", err);

	// 5. A STRING THAT ENDS ON A PAGE BOUNDARY: the reader must not refuse it because the NEXT page is unmapped.
	void* boundary = mmap(nullptr, 8192, PROT_READ | PROT_WRITE, MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
	if (boundary == MAP_FAILED) { perror("mmap"); return 3; }
	munmap((char*) boundary + 4096, 4096);
	const char* tail = "boundary";
	const size_t tail_len = strlen(tail);
	memcpy((char*) boundary + 4096 - tail_len - 1, tail, tail_len + 1); // NUL is the last byte of the mapped page
	err = 0;
	n = xtrace_safe_read_c_string(scratch, XTRACE_STRING_MAX, (const char*) boundary + 4096 - tail_len - 1, &err);
	if (n != tail_len || strcmp(scratch, tail) != 0) { printf("FAIL boundary n=%zu err=%d\n", n, err); return 1; }
	printf("OK boundary string=%s\n", scratch);

	printf("SAFE-READ-CONTRACT PASS\n");
	return 0;
}
DRIVER

# `-idirafter`, NOT `-I`: the xtrace directory holds its own `string.h`, and putting it on the normal include path
# makes the host's <cstring> include the tracer's header instead of the system one.
"$cxx" -std=c++11 -O1 -idirafter "$src/src/xtrace" -o "$tmp/driver" "$tmp/driver.cpp" "$reader" || {
	echo "contract: cannot compile the reader ($reader) on this host" >&2; exit 3; }

fail=0
check() { # label expected actual
	if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: expected $2, got $3"; fail=1; fi
}

out="$("$tmp/driver" 2>&1)"; rc=$?
check "arm 1-5: the reader reports every real mapping shape" 0 "$rc"
check "arm 1: a mapped range is copied" yes "$(case "$out" in *"OK mapped bytes="*) echo yes ;; *) echo no ;; esac)"
check "arm 2: an unmapped range is REPORTED, not loaded" yes "$(case "$out" in *"OK unmapped errno=14"*) echo yes ;; *) echo no ;; esac)"
check "arm 3: a partly mapped range is a short copy" yes "$(case "$out" in *"OK partial"*) echo yes ;; *) echo no ;; esac)"
check "arm 4: a string whose end is unmapped is UNREADABLE" yes "$(case "$out" in *"OK unterminated"*) echo yes ;; *) echo no ;; esac)"
check "arm 5: a string ending on a page boundary is read to its NUL" yes "$(case "$out" in *"OK boundary string=boundary"*) echo yes ;; *) echo no ;; esac)"

# THE RED ARM: the SAME address, dereferenced the way the instrument used to dereference it, must still kill the
# process. If this arm ever passes, the contract above proves nothing (it would be measuring a reader that refuses
# everything).
red_out="$("$tmp/driver" --raw 2>&1)"; red_rc=$?
check "red arm: a raw dereference of the same address DIES (118=SIGSEGV, 135=SIGBUS)" yes \
	"$(case "$red_rc" in 139|135) echo yes ;; *) echo no ;; esac)"

if [ "$fail" -eq 0 ]; then
	echo "XTRACE-SAFE-READ-CONTRACT: PASS"
else
	echo "XTRACE-SAFE-READ-CONTRACT: FAIL" >&2
	exit 1
fi
