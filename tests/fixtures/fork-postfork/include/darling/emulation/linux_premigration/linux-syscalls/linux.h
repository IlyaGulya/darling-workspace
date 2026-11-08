#pragma once
#include <asm/unistd.h>
#define LINUX_ECHILD 10
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL6(n, a, b, c, d, e, f) test_linux_syscall((n), (long)(a), (long)(b), (long)(c), (long)(d), (long)(e), (long)(f))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
