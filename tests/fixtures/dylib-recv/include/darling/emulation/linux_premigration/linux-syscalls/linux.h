#pragma once
#define __NR_getpid 39
#define __NR_gettid 186
#define __NR_recvmsg 47
#define __NR_sendmsg 46
#define __NR_sendto 44
#define __NR_mmap 9
#define __NR_munmap 11
#define __NR_mprotect 10
#define __NR_msync 26
#define __NR_close 3
#define __NR_sched_yield 24
#define LINUX_EAGAIN 11
#define LINUX_EIO 5
#define LINUX_EBADMSG 74
#define LINUX_ECOMM 70
#define LINUX_EPIPE 32
#define LINUX_EINTR 4
long test_linux_syscall(long nr, ...);
#define LINUX_SYSCALL0(n) test_linux_syscall((n))
#define LINUX_SYSCALL1(n, a) test_linux_syscall((n), (a))
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall((n), (a), (b))
#define LINUX_SYSCALL3(n, a, b, c) test_linux_syscall((n), (a), (b), (c))
#define LINUX_SYSCALL(n, ...) test_linux_syscall((n), ##__VA_ARGS__)
