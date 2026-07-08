#pragma once
#include <stdint.h>
typedef struct {
	uint64_t __rax, __rbx, __rcx, __rdx, __rdi, __rsi, __rbp, __rsp;
	uint64_t __r8, __r9, __r10, __r11, __r12, __r13, __r14, __r15;
	uint64_t __rip, __rflags, __cs, __fs, __gs;
} x86_thread_state64_t;
typedef struct {
	uint16_t __fpu_fcw, __fpu_fsw;
	uint8_t __fpu_ftw;
	uint16_t __fpu_fop;
	uint16_t __fpu_cs, __fpu_ds;
	uint64_t __fpu_ip, __fpu_dp;
	uint32_t __fpu_mxcsr, __fpu_mxcsrmask;
	unsigned char __fpu_stmm0[128];
	unsigned char __fpu_xmm0[256];
} x86_float_state64_t;
typedef int thread_t;
int mach_thread_self(void);
