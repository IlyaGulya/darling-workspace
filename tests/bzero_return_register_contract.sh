#!/usr/bin/env bash
set -euo pipefail

src="${LIBPLATFORM_SRC_ROOT:?set LIBPLATFORM_SRC_ROOT}"
asm="$src/src/string/x86_64/bzero.S"
test -f "$asm"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

cat > "$tmp/harness.c" <<'C_EOF'
#include <stddef.h>
#include <stdio.h>
#include <string.h>

void *__platform_memset(void *s, int c, size_t n) {
	return memset(s, c, n);
}

int main(void) {
	unsigned char buf[64];
	memset(buf, 0xa5, sizeof(buf));

	void *ret;
	__asm__ __volatile__(
		"call __platform_bzero"
		: "=a"(ret)
		: "D"(buf), "S"(sizeof(buf))
		: "rdx", "rcx", "r8", "r9", "r10", "r11", "memory"
	);

	if (ret != buf) {
		fprintf(stderr, "__platform_bzero returned %p for %p\n", ret, (void *)buf);
		return 1;
	}
	for (size_t i = 0; i < sizeof(buf); ++i) {
		if (buf[i] != 0) {
			fprintf(stderr, "__platform_bzero left byte %zu as 0x%02x\n", i, buf[i]);
			return 1;
		}
	}
	return 0;
}
C_EOF

cc -std=gnu11 -Wall -Wextra -Werror "$tmp/harness.c" "$asm" -o "$tmp/bzero-contract"
"$tmp/bzero-contract"
