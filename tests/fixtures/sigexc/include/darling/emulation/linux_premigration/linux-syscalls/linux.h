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
