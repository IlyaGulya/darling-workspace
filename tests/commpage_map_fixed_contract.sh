#!/usr/bin/env bash
set -euo pipefail

src="${DARLING_SRC_ROOT:?set DARLING_SRC_ROOT}"
commpage_c="$src/src/startup/mldr/commpage.c"
test -f "$commpage_c"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/include/i386"

cat > "$tmp/include/i386/cpu_capabilities.h" <<'H_EOF'
#pragma once
#define _COMM_PAGE_START_ADDRESS        0x00007fffffe00000ULL
#define _COMM_PAGE64_BASE_ADDRESS       0x00007fffffe00000ULL
#define _COMM_PAGE64_AREA_LENGTH        0x1000
#define _COMM_PAGE32_BASE_ADDRESS       0xffff0000U
#define _COMM_PAGE32_AREA_LENGTH        0x1000
#define _COMM_PAGE_SIGNATURE            (_COMM_PAGE_START_ADDRESS + 0x000)
#define _COMM_PAGE_VERSION              (_COMM_PAGE_START_ADDRESS + 0x020)
#define _COMM_PAGE_CPU_CAPABILITIES64   (_COMM_PAGE_START_ADDRESS + 0x028)
#define _COMM_PAGE_CPU_CAPABILITIES     (_COMM_PAGE_START_ADDRESS + 0x030)
#define _COMM_PAGE_NCPUS                (_COMM_PAGE_START_ADDRESS + 0x034)
#define _COMM_PAGE_ACTIVE_CPUS          (_COMM_PAGE_START_ADDRESS + 0x035)
#define _COMM_PAGE_PHYSICAL_CPUS        (_COMM_PAGE_START_ADDRESS + 0x036)
#define _COMM_PAGE_LOGICAL_CPUS         (_COMM_PAGE_START_ADDRESS + 0x037)
#define _COMM_PAGE_MEMORY_SIZE          (_COMM_PAGE_START_ADDRESS + 0x038)
#define _COMM_PAGE_USER_PAGE_SHIFT_64   (_COMM_PAGE_START_ADDRESS + 0x040)
#define _COMM_PAGE_USER_PAGE_SHIFT_32   (_COMM_PAGE_START_ADDRESS + 0x041)
#define _COMM_PAGE_KERNEL_PAGE_SHIFT    (_COMM_PAGE_START_ADDRESS + 0x042)
#define _COMM_PAGE_THIS_VERSION         13
#define kHasMMX 0x00000001ULL
#define kHasSSE 0x00000002ULL
#define kHasSSE2 0x00000004ULL
#define kHasSSE3 0x00000008ULL
#define kHasSupplementalSSE3 0x00000100ULL
#define k64Bit 0x00000200ULL
#define kHasSSE4_1 0x00000400ULL
#define kHasSSE4_2 0x00000800ULL
#define kHasAES 0x00001000ULL
#define kUP 0x00008000ULL
#define kHasAVX1_0 0x01000000ULL
#define kHasRDRAND 0x02000000ULL
#define kHasF16C 0x04000000ULL
#define kHasENFSTRG 0x08000000ULL
#define kHasFMA 0x10000000ULL
#define kHasAVX2_0 0x20000000ULL
#define kHasBMI1 0x40000000ULL
#define kHasBMI2 0x80000000ULL
#define kHasRTM 0x0000000100000000ULL
#define kHasHLE 0x0000000200000000ULL
#define kHasADX 0x0000000400000000ULL
#define kHasRDSEED 0x0000000800000000ULL
#define kHasMPX 0x0000001000000000ULL
#define kHasSGX 0x0000002000000000ULL
#define kHasAVX512F 0x0000004000000000ULL
#define kHasAVX512DQ 0x0000008000000000ULL
#define kHasAVX512IFMA 0x0000010000000000ULL
#define kHasAVX512PF 0x0000020000000000ULL
#define kHasAVX512ER 0x0000040000000000ULL
#define kHasAVX512CD 0x0000080000000000ULL
#define kHasAVX512BW 0x0000100000000000ULL
#define kHasAVX512VL 0x0000200000000000ULL
#define kHasSHA 0x0000400000000000ULL
#define kHasAVX512VBMI 0x0000800000000000ULL
H_EOF

cat > "$tmp/harness.c" <<'C_EOF'
#include <stdint.h>
#include <stdio.h>
#include <sys/mman.h>
#include <unistd.h>

#include "src/startup/mldr/commpage.h"
#include <i386/cpu_capabilities.h>

int main(void) {
	void *want = (void *)_COMM_PAGE64_BASE_ADDRESS;
	void *pre = mmap(want, _COMM_PAGE64_AREA_LENGTH, PROT_READ | PROT_WRITE,
	    MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
	if (pre != want) {
		perror("premap canonical commpage");
		return 2;
	}

	((uint8_t *)want)[_COMM_PAGE_PHYSICAL_CPUS - _COMM_PAGE_START_ADDRESS] = 0;
	commpage_setup(1);

	uint8_t physical =
	    ((uint8_t *)want)[_COMM_PAGE_PHYSICAL_CPUS - _COMM_PAGE_START_ADDRESS];
	uint16_t version =
	    *(uint16_t *)((uint8_t *)want + (_COMM_PAGE_VERSION - _COMM_PAGE_START_ADDRESS));

	if (physical == 0) {
		fprintf(stderr, "canonical commpage was not populated\n");
		return 1;
	}
	if (version != _COMM_PAGE_THIS_VERSION) {
		fprintf(stderr, "bad commpage version %u\n", version);
		return 1;
	}
	return 0;
}
C_EOF

cc -std=gnu11 -Wall -Wextra -Werror \
	-I "$tmp/include" \
	-I "$src" \
	-I "$src/src/startup/mldr" \
	"$tmp/harness.c" "$commpage_c" -o "$tmp/commpage-contract"
"$tmp/commpage-contract"
