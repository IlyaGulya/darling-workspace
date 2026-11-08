#pragma once
#include <stdint.h>
typedef struct {
	uint64_t __rax, __rbx, __rcx, __rdx, __rdi, __rsi, __rbp, __rsp;
	uint64_t __r8, __r9, __r10, __r11, __r12, __r13, __r14, __r15;
	uint64_t __rip, __rflags, __cs, __fs, __gs;
} x86_thread_state64_t;
typedef struct {
	/* Darwin osfmk/mach/i386/_structs.h, _STRUCT_X86_FLOAT_STATE64.
	 * Keep reserved bytes: production converts into the shared urgent frame. */
	int32_t __fpu_reserved[2];
	uint16_t __fpu_fcw, __fpu_fsw;
	uint8_t __fpu_ftw, __fpu_rsrv1;
	uint16_t __fpu_fop;
	uint32_t __fpu_ip;
	uint16_t __fpu_cs, __fpu_rsrv2;
	uint32_t __fpu_dp;
	uint16_t __fpu_ds, __fpu_rsrv3;
	uint32_t __fpu_mxcsr, __fpu_mxcsrmask;
	unsigned char __fpu_stmm0[128];
	unsigned char __fpu_xmm0[256];
	unsigned char __fpu_rsrv4[96];
	int32_t __fpu_reserved1;
} x86_float_state64_t;
typedef int thread_t;
int mach_thread_self(void);
