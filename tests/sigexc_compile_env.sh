prepare_sigexc_compile_env() {
	local tmp="${1:?tmp dir}"
	local emu="${2:?libsystem_kernel emulation root}"

	mkdir -p \
		"$tmp/include/darling/emulation/common" \
		"$tmp/include/darling/emulation/conversion/signal" \
		"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
		"$tmp/include/darling/emulation/linux_premigration/resources" \
		"$tmp/include/darling/emulation/linux_premigration/signal" \
		"$tmp/include/darling/emulation/other/mach" \
		"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/mman" \
		"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal" \
		"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd" \
		"$tmp/include/sys" \
		"$tmp/include/darlingserver"

	cat > "$tmp/include/_libkernel_init.h" <<'H_EOF'
#pragma once
typedef struct { int unused; } _libkernel_functions_t;
H_EOF

	cat > "$tmp/include/rtsig.h" <<'H_EOF'
#pragma once
#define LINUX_SIGRTMIN 34
H_EOF

	cat > "$tmp/include/sys/signal.h" <<'H_EOF'
#pragma once
typedef unsigned int sigset_t;
struct bsd_siginfo;
typedef void (bsd_sig_handler)(int, struct bsd_siginfo*, void*);
#define SIG_IGN ((bsd_sig_handler*)1)
#define SIGSEGV 11
#define SIGTSTP 20
#define SIGSTOP 19
#define SIGTRAP 5
H_EOF

	cat > "$tmp/include/darling/emulation/common/base.h" <<'H_EOF'
#pragma once
#include <stdbool.h>
#ifdef __cplusplus
#define CPP_EXTERN_BEGIN extern "C" {
#define CPP_EXTERN_END }
#else
#define CPP_EXTERN_BEGIN
#define CPP_EXTERN_END
#endif
H_EOF

	cat > "$tmp/include/darling/emulation/common/simple.h" <<'H_EOF'
#pragma once
void __simple_printf(const char* message, ...);
void __simple_kprintf(const char* message, ...);
void __simple_abort(void);
H_EOF

	cat > "$tmp/include/darling/emulation/conversion/signal/duct_signals.h" <<'H_EOF'
#pragma once
typedef unsigned int sigset_t;
typedef unsigned long long linux_sigset_t;
struct bsd_siginfo;
typedef void (bsd_sig_handler)(int, struct bsd_siginfo*, void*);
#define SIG_IGN ((bsd_sig_handler*)1)
#define LINUX_SIGKILL 9
#define LINUX_SIGUSR1 10
#define LINUX_SIGSEGV 11
#define LINUX_SIGCHLD 17
#define LINUX_SIGSTOP 19
#define LINUX_SIGCONT 18
#define LINUX_SIGTTOU 22
#define LINUX_SIGURG 23
#define LINUX_SIGWINCH 28
#define LINUX_SA_SIGINFO 0x00000004u
#define LINUX_SA_ONSTACK 0x08000000u
#define LINUX_SA_RESTART 0x10000000u
#define LINUX_SA_NODEFER 0x40000000u
#define LINUX_SA_RESTORER 0x04000000
int signum_linux_to_bsd(int signum);
int signum_bsd_to_linux(int signum);
void sigset_linux_to_bsd(const linux_sigset_t* linux_set, sigset_t* bsd);
void sigset_bsd_to_linux(const sigset_t* bsd, linux_sigset_t* linux_set);
H_EOF

	mkdir -p "$tmp/include/darling/emulation/conversion"
	cat > "$tmp/include/darling/emulation/conversion/errno.h" <<'H_EOF'
#pragma once
int errno_linux_to_bsd(int err);
H_EOF

	cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_rt_sigprocmask 14
#define __NR_rt_sigaction 13
#define __NR_kill 62
#define __NR_getpid 39
#define __NR_gettid 186
#define __NR_tgkill 234
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL0(n) test_linux_syscall((n))
#define LINUX_SYSCALL1(n, a) test_linux_syscall((n), (long)(a))
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall((n), (long)(a), (long)(b))
#define LINUX_SYSCALL3(n, a, b, c) test_linux_syscall((n), (long)(a), (long)(b), (long)(c))
#define LINUX_SYSCALL4(n, a, b, c, d) test_linux_syscall((n), (long)(a), (long)(b), (long)(c), (long)(d))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
H_EOF

	cat > "$tmp/include/darling/emulation/other/mach/lkm.h" <<'H_EOF'
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
H_EOF

	cat > "$tmp/include/darlingserver/rpc.h" <<'H_EOF'
#pragma once
int dserver_rpc_interrupt_enter(void);
int dserver_rpc_interrupt_exit(void);
int dserver_rpc_thread_suspended(void* thread_state, void* float_state);
int dserver_rpc_s2c_perform(void);
int dserver_rpc_sigprocess(int bsd_signum_in, int linux_signum, int sender_pid,
	int code, void* fault_addr, void* thread_state, void* float_state,
	int* bsd_signum_out);
H_EOF

	cat > "$tmp/include/darling/emulation/linux_premigration/resources/dserver-ring.h" <<'H_EOF'
#pragma once
static inline int __dserver_ring_started_suspended(void* started, int* code) { (void)started; (void)code; return -1; }
static inline int __dserver_ring_get_tracer(void* tracer, int* code) { (void)tracer; (void)code; return -1; }
H_EOF

	cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/sigaltstack.h" <<'H_EOF'
#pragma once
#include <stddef.h>
struct linux_stack {
	void* ss_sp;
	int ss_flags;
	size_t ss_size;
};
struct bsd_stack {
	void* ss_sp;
	size_t ss_size;
	int ss_flags;
};
static inline long sys_sigaltstack(const struct bsd_stack* new_stack, struct bsd_stack* old_stack)
{
	(void)new_stack;
	if (old_stack) {
		old_stack->ss_sp = 0;
		old_stack->ss_size = 0;
		old_stack->ss_flags = 0;
	}
	return 0;
}
H_EOF

	for header in \
		darling/emulation/xnu_syscall/bsd/impl/unistd/exit.h \
		darling/emulation/xnu_syscall/bsd/impl/signal/kill.h
	do
		mkdir -p "$tmp/include/$(dirname "$header")"
		printf '#pragma once\n' > "$tmp/include/$header"
	done

	cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/mman/mman.h" <<'H_EOF'
#pragma once
static inline long sys_mprotect(void* addr, unsigned long len, int prot) { (void)addr; (void)len; (void)prot; return 0; }
static inline long sys_mmap(void* addr, unsigned long len, int prot, int flags, int fd, unsigned long off) { (void)addr; (void)len; (void)prot; (void)flags; (void)fd; (void)off; return 0; }
static inline long sys_munmap(void* addr, unsigned long len) { (void)addr; (void)len; return 0; }
H_EOF

	cat > "$tmp/include/darling/emulation/conversion/signal/sigaction.h" <<H_EOF
#pragma once
#include "$emu/include/conversion/signal/sigaction.h"
H_EOF

	cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/signal/sigaction.h" <<H_EOF
#pragma once
#include "$emu/include/xnu_syscall/bsd/impl/signal/sigaction.h"
H_EOF

	cat > "$tmp/include/darling/emulation/linux_premigration/signal/sigexc.h" <<H_EOF
#pragma once
#include "$emu/include/linux_premigration/signal/sigexc.h"
H_EOF
}

sigexc_contract_cflags() {
	printf '%s\n' \
		-std=gnu11 \
		-Wall \
		-Wextra \
		-Werror \
		-Wno-unused-parameter \
		-Wno-unused-variable \
		-Wno-unused-but-set-variable \
		-Wno-unused-function \
		-include \
		string.h
}
