#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
ksrc="$src/darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl"
test -f "$ksrc/unistd/dup.c"
test -f "$ksrc/unistd/dup2.c"
test -f "$ksrc/fcntl/fcntl.c"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common/guarded" \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion/fcntl" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/other/mach" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/helper/bsdthread" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/helper/misc" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd"

cat > "$tmp/include/darling/emulation/common/base.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/common/simple.h" <<'H_EOF'
#pragma once
void __simple_abort(void);
void __simple_kprintf(const char *fmt, ...);
H_EOF

cat > "$tmp/include/darling/emulation/common/guarded/table.h" <<'H_EOF'
#pragma once
#include <stdbool.h>
enum { guard_flag_prevent_close = 1 };
bool guard_table_check(int fd, int flag);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/errno.h" <<'H_EOF'
#pragma once
int errno_linux_to_bsd(int ret);
H_EOF

cat > "$tmp/include/darling/emulation/conversion/duct_errno.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/conversion/fcntl/fcntl.h" <<'H_EOF'
#pragma once
struct linux_flock {
	short l_type;
	short l_whence;
	long long l_start;
	long long l_len;
	int l_pid;
};
#define LINUX_F_RDLCK 0
#define LINUX_F_WRLCK 1
#define LINUX_F_UNLCK 2
#define LINUX_F_DUPFD 0
#define LINUX_F_GETFD 1
#define LINUX_F_SETFD 2
#define LINUX_F_GETFL 3
#define LINUX_F_SETFL 4
#define LINUX_F_GETOWN 9
#define LINUX_F_SETOWN 8
#define LINUX_F_SETLK 6
#define LINUX_F_SETLKW 7
#define LINUX_F_GETLK 5
#define LINUX_F_DUPFD_CLOEXEC 1030
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
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
H_EOF

cat > "$tmp/include/darling/emulation/other/mach/lkm.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/helper/bsdthread/cancelable.h" <<'H_EOF'
#pragma once
#define CANCELATION_POINT() ((void)0)
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/helper/misc/fdpath.h" <<'H_EOF'
#pragma once
long fdpath(int fd, long buf, int len);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl/fcntl.h" <<'H_EOF'
#pragma once
long sys_fcntl_nocancel(int fd, int cmd, long arg);
H_EOF

for header in \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/fcntl/open.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/dup.h" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/dup2.h"
do
	cat > "$header" <<'H_EOF'
#pragma once
H_EOF
done

cat > "$tmp/harness.c" <<C_EOF
#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

#ifndef F_CHECK_LV
#define F_CHECK_LV 98
#endif
#ifndef F_GETPATH
#define F_GETPATH 50
#endif
#ifndef F_FULLFSYNC
#define F_FULLFSYNC 51
#endif

struct bsd_flock {
	short l_type;
	short l_whence;
	long long l_start;
	long long l_len;
	int l_pid;
};

static const int guarded_fd = 77;

bool guard_table_check(int fd, int flag) {
	(void)flag;
	return fd == guarded_fd;
}

int errno_linux_to_bsd(int ret) {
	return ret;
}

int oflags_bsd_to_linux(int flags) {
	return flags;
}

int oflags_linux_to_bsd(int flags) {
	return flags;
}

long fdpath(int fd, long buf, int len) {
	(void)fd;
	(void)buf;
	(void)len;
	return -EBADF;
}

long sys_readlink(const char *path, char *buf, unsigned long bsize) {
	(void)path;
	(void)buf;
	(void)bsize;
	return -EINVAL;
}

long test_linux_syscall_unexpected(void) {
	fprintf(stderr, "guarded fd path reached Linux syscall fallback\\n");
	_exit(3);
}

void __simple_kprintf(const char *fmt, ...) {
	(void)fmt;
}

void __simple_abort(void) {
	abort();
}

#include "$ksrc/unistd/dup.c"
#include "$ksrc/unistd/dup2.c"
#include "$ksrc/fcntl/fcntl.c"

static void expect_ebadf(const char *name, long got) {
	if (got != -EBADF) {
		fprintf(stderr, "%s returned %ld, expected -EBADF\\n", name, got);
		exit(1);
	}
}

static void child_dup2_to_guarded(void) {
	expect_ebadf("sys_dup2(fd, guarded)", sys_dup2(STDOUT_FILENO, guarded_fd));
}

int main(void) {
	pid_t pid = fork();
	if (pid < 0) {
		perror("fork");
		return 2;
	}
	if (pid == 0) {
		child_dup2_to_guarded();
		_exit(0);
	}

	int status = 0;
	if (waitpid(pid, &status, 0) != pid) {
		perror("waitpid");
		return 2;
	}
	if (WIFSIGNALED(status)) {
		fprintf(stderr, "sys_dup2(fd, guarded) aborted with signal %d\\n", WTERMSIG(status));
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "sys_dup2(fd, guarded) child status 0x%x\\n", status);
		return 1;
	}

	expect_ebadf("sys_dup(guarded)", sys_dup(guarded_fd));
	expect_ebadf("sys_dup2(guarded, fd)", sys_dup2(guarded_fd, STDOUT_FILENO));
	expect_ebadf("sys_fcntl_nocancel(guarded)", sys_fcntl_nocancel(guarded_fd, F_GETFD, 0));
	return 0;
}
C_EOF

cc -std=gnu11 -w -Ulinux -I "$tmp/include" "$tmp/harness.c" -o "$tmp/fd-guard-contract"
"$tmp/fd-guard-contract"
