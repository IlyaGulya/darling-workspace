#pragma once
#define __NR_clone 56
#define __NR_fork 57
#define LINUX_ECHILD 10
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), __VA_ARGS__)
