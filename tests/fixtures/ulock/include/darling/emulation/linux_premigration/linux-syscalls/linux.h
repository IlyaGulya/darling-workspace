#pragma once
#include <asm/unistd.h>
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
