#pragma once
#include <stdint.h>
typedef unsigned long long linux_sigset_t;
#define __NR_rt_sigprocmask 14
#define __NR_rt_sigaction 13
#define __NR_mprotect 10
#define LINUX_SIG_BLOCK 0
#define LINUX_SIG_UNBLOCK 1
#define LINUX_SA_RESTORER 0x04000000
#define LINUX_SA_SIGINFO 4
#define LINUX_SA_RESTART 0x10000000
#define LINUX_SA_ONSTACK 0x08000000
#define LINUX_SIGSTOP 19
#define LINUX_SIGKILL 9
#define LINUX_SIGCHLD 17
#define SIGNAL_SIGEXC_SUSPEND 34
#define SIGNAL_S2C 35
#define LINUX_SYSCALL(n, ...) (-1)
struct linux_siginfo { int si_signo; int si_code; int si_pid; int si_uid; };
struct linux_gregset { unsigned long long gregs[23]; };
typedef void *linux_fpregset_t;
struct linux_ucontext { int unused; };
typedef void linux_sig_handler(int, struct linux_siginfo *, struct linux_ucontext *);
struct linux_sigaction {
	linux_sig_handler *sa_sigaction;
	linux_sigset_t sa_mask;
	unsigned long sa_flags;
	void (*sa_restorer)(void);
};
