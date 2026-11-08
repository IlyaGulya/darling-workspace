#pragma once
#include <asm/unistd.h>
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL0(n) test_linux_syscall((n))
#define LINUX_SYSCALL1(n, a) test_linux_syscall((n), (long)(a))
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall((n), (long)(a), (long)(b))
#define LINUX_SYSCALL3(n, a, b, c) test_linux_syscall((n), (long)(a), (long)(b), (long)(c))
#define LINUX_SYSCALL4(n, a, b, c, d) test_linux_syscall((n), (long)(a), (long)(b), (long)(c), (long)(d))
#define LINUX_SYSCALL6(n, a, b, c, d, e, f) test_linux_syscall((n), (long)(a), (long)(b), (long)(c), (long)(d), (long)(e), (long)(f))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
