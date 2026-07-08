#pragma once
#define __NR_lstat 6
#define __NR_statfs 137
#define __NR_utimes 235
#define __NR_chdir 80
#define __NR_chmod 90
#define __NR_fchmodat 268
#define __NR_fchownat 260
#define __NR_lchown 94
#define __NR_linkat 265
#define __NR_mknod 133
#define __NR_readlinkat 267
#define __NR_truncate 76
#define __NR_unlinkat 263
#define __NR_getxattr 191
#define __NR_listxattr 194
#define __NR_llistxattr 195
#define LINUX_AT_FDCWD (-100)
#define LINUX_AT_SYMLINK_NOFOLLOW 0x100
long test_linux_syscall_unexpected(void);
#define LINUX_SYSCALL(n, ...) test_linux_syscall_unexpected()
#define LINUX_SYSCALL1(n, a) test_linux_syscall_unexpected()
#define LINUX_SYSCALL2(n, a, b) test_linux_syscall_unexpected()
