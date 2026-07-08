#pragma once
#define __NR_fchownat 260
#define __NR_lchown 94
#define LINUX_SYSCALL(n, ...) test_linux_syscall_unexpected()
long test_linux_syscall_unexpected(void);
