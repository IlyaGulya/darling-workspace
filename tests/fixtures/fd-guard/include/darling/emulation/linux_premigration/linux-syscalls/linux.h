#pragma once
#define __NR_dup 32
#define __NR_dup2 33
#define __NR_fcntl 72
#define __NR_fsync 74
#define LINUX_EINVAL (-22)
long test_linux_syscall_unexpected(void);
#define LINUX_SYSCALL1(n, a) test_linux_syscall_unexpected()
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall_unexpected()
#define LINUX_SYSCALL(n, ...) test_linux_syscall_unexpected()
