#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
impl="$src/darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl"
for file in \
	"$impl/stat/lstat.c" \
	"$impl/stat/statfs.c" \
	"$impl/time/utimes.c" \
	"$impl/unistd/chdir.c" \
	"$impl/unistd/chmod_extended.c" \
	"$impl/unistd/fchmodat.c" \
	"$impl/unistd/linkat.c" \
	"$impl/unistd/mknod.c" \
	"$impl/unistd/readlinkat.c" \
	"$impl/unistd/truncate.c" \
	"$impl/unistd/unlinkat.c" \
	"$impl/xattr/getxattr.c" \
	"$impl/xattr/listxattr.c"
do
	test -f "$file"
done

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common/bsdthread" \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion/stat" \
	"$tmp/include/darling/emulation/conversion/xattr" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/linux_premigration" \
	"$tmp/include/darling/emulation/other/mach" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/stat" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/time" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/xattr"

cat > "$tmp/include/darling/emulation/common/base.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/common/simple.h" <<'H_EOF'
#pragma once
struct simple_readline_buf { int unused; };
void __simple_readline_init(struct simple_readline_buf *buf);
int __simple_readline(int fd, struct simple_readline_buf *buf, char *line, unsigned long size);
H_EOF

cat > "$tmp/include/darling/emulation/common/bsdthread/per_thread_wd.h" <<'H_EOF'
#pragma once
int get_perthread_wd(void);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/errno.h" <<'H_EOF'
#pragma once
int errno_linux_to_bsd(int ret);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/common_at.h" <<'H_EOF'
#pragma once
#define BSD_AT_FDCWD -2
#define BSD_AT_SYMLINK_NOFOLLOW 0x20
#define BSD_AT_REMOVEDIR 0x80
#define BSD_AT_SYMLINK_FOLLOW 0x40
#define LINUX_AT_INVALID (-1)
int atfd(int fd);
int atflags_bsd_to_linux(int flags);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/stat/common.h" <<'H_EOF'
#pragma once
struct linux_stat { int st_mode; };
struct linux_statfs64 { int unused; };
struct stat64 { int unused; };
struct linux_timeval { long tv_sec; long tv_usec; };
struct bsd_statfs { char f_mntonname[1024]; char f_fstypename[32]; char f_mntfromname[1024]; };
struct bsd_statfs64 { char f_mntonname[1024]; char f_fstypename[32]; char f_mntfromname[1024]; };
void stat_linux_to_bsd(struct linux_stat *src, struct stat *dst);
void stat_linux_to_bsd64(struct linux_stat *src, struct stat64 *dst);
void statfs_linux_to_bsd64(struct linux_statfs64 *src, struct bsd_statfs64 *dst);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/xattr/getxattr.h" <<'H_EOF'
#pragma once
#define ENOATTR 93
#define XATTR_NOFOLLOW 1
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/vchroot_expand.h" <<'H_EOF'
#pragma once
#define VCHROOT_FOLLOW 1
struct vchroot_expand_args {
	int flags;
	int dfd;
	char path[4096];
};
int vchroot_expand(struct vchroot_expand_args *args);
int vchroot_prepare_write(const char *path);
void vchroot_pre_mkdir(const char *path);
int vchroot_xattr_is_marker(const char *name);
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
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
H_EOF

cat > "$tmp/include/darling/emulation/other/mach/lkm.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl/open.h" <<'H_EOF'
#pragma once
long sys_open(const char *path, int flags, int mode);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/close.h" <<'H_EOF'
#pragma once
long close_internal(int fd);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/stat/statfs.h" <<'H_EOF'
#pragma once
struct bsd_statfs64;
long sys_statfs64(const char *path, struct bsd_statfs64 *buf);
H_EOF

for header in \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/link.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/stat/lstat.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/time/utimes.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/chdir.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/chmod_extended.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/chown.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/fchmodat.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/fchownat.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/lchown.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/mknod.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/readlink.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/readlinkat.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/truncate.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/unlinkat.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/xattr/getxattr.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/xattr/listxattr.h"
do
	cat > "$header" <<'H_EOF'
#pragma once
H_EOF
done

cat > "$tmp/harness.c" <<C_EOF
#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

struct bsd_timeval { long tv_sec; int tv_usec; };

#include "$impl/stat/lstat.c"
#include "$impl/stat/statfs.c"
#include "$impl/time/utimes.c"
#include "$impl/unistd/chdir.c"
#include "$impl/unistd/chmod_extended.c"
#include "$impl/unistd/fchmodat.c"
#include "$impl/unistd/linkat.c"
#include "$impl/unistd/mknod.c"
#include "$impl/unistd/readlinkat.c"
#include "$impl/unistd/truncate.c"
#include "$impl/unistd/unlinkat.c"
#include "$impl/xattr/getxattr.c"
#include "$impl/xattr/listxattr.c"

int get_perthread_wd(void) { return -100; }
int atfd(int fd) { return fd; }
int atflags_bsd_to_linux(int flags) { return flags; }
int errno_linux_to_bsd(int ret) { return ret; }
long test_linux_syscall_unexpected(void) {
	fprintf(stderr, "NULL path reached Linux syscall fallback\\n");
	_exit(4);
}
int vchroot_expand(struct vchroot_expand_args *args) {
	(void)args;
	fprintf(stderr, "NULL path reached vchroot_expand\\n");
	_exit(5);
}
int vchroot_prepare_write(const char *path) { (void)path; return 0; }
void vchroot_pre_mkdir(const char *path) { (void)path; }
int vchroot_xattr_is_marker(const char *name) { (void)name; return 0; }
void stat_linux_to_bsd(struct linux_stat *src, struct stat *dst) { (void)src; (void)dst; }
void stat_linux_to_bsd64(struct linux_stat *src, struct stat64 *dst) { (void)src; (void)dst; }
void statfs_linux_to_bsd64(struct linux_statfs64 *src, struct bsd_statfs64 *dst) { (void)src; (void)dst; }
long sys_open(const char *path, int flags, int mode) { (void)path; (void)flags; (void)mode; return -ENOENT; }
long close_internal(int fd) { (void)fd; return 0; }
void __simple_readline_init(struct simple_readline_buf *buf) { (void)buf; }
int __simple_readline(int fd, struct simple_readline_buf *buf, char *line, unsigned long size) {
	(void)fd; (void)buf; (void)line; (void)size; return 0;
}

static void expect_efault(const char *name, long got) {
	if (got != -EFAULT) {
		fprintf(stderr, "%s returned %ld, expected -EFAULT\\n", name, got);
		_exit(1);
	}
}

static void run_case(const char *name) {
	if (name[0] == '0') expect_efault("sys_lstat64", sys_lstat64(NULL, NULL));
	else if (name[0] == '1') expect_efault("sys_statfs64", sys_statfs64(NULL, NULL));
	else if (name[0] == '2') expect_efault("sys_utimes", sys_utimes(NULL, NULL));
	else if (name[0] == '3') expect_efault("sys_chdir", sys_chdir(NULL));
	else if (name[0] == '4') expect_efault("sys_chmod_extended", sys_chmod_extended(NULL, 0, 0, 0, NULL));
	else if (name[0] == '5') expect_efault("sys_fchmodat", sys_fchmodat(0, NULL, 0, 0));
	else if (name[0] == '6') expect_efault("sys_linkat left", sys_linkat(0, NULL, 0, "dst", 0));
	else if (name[0] == '7') expect_efault("sys_linkat right", sys_linkat(0, "src", 0, NULL, 0));
	else if (name[0] == 'a') expect_efault("sys_mknod", sys_mknod(NULL, 0, 0));
	else if (name[0] == 'b') expect_efault("sys_readlinkat", sys_readlinkat(0, NULL, NULL, 0));
	else if (name[0] == 'c') expect_efault("sys_truncate", sys_truncate(NULL, 0));
	else if (name[0] == 'd') expect_efault("sys_unlinkat", sys_unlinkat(0, NULL, 0));
	else if (name[0] == 'e') expect_efault("sys_getxattr", sys_getxattr(NULL, "name", NULL, 0, 0, 0));
	else if (name[0] == 'f') expect_efault("sys_listxattr", sys_listxattr(NULL, NULL, 0, 0));
	else _exit(2);
}

int main(void) {
	const char *cases[] = {
		"0", "1", "2", "3", "4", "5", "6", "7",
		"a", "b", "c", "d", "e", "f",
	};
	for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
		pid_t pid = fork();
		if (pid < 0) {
			perror("fork");
			return 2;
		}
		if (pid == 0) {
			run_case(cases[i]);
			_exit(0);
		}
		int status = 0;
		if (waitpid(pid, &status, 0) != pid) {
			perror("waitpid");
			return 2;
		}
		if (WIFSIGNALED(status)) {
			fprintf(stderr, "case %s died with signal %d\\n", cases[i], WTERMSIG(status));
			return 1;
		}
		if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
			fprintf(stderr, "case %s exited with status 0x%x\\n", cases[i], status);
			return 1;
		}
	}
	return 0;
}
C_EOF

cc -std=gnu11 -w -Ulinux -I "$tmp/include" "$tmp/harness.c" -o "$tmp/vchroot-pathnull-contract"
"$tmp/vchroot-pathnull-contract"
