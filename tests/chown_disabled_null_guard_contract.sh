#!/usr/bin/env bash
set -euo pipefail

src="${XNU_SRC_ROOT:?set XNU_SRC_ROOT}"
impl="$src/darling/src/libsystem_kernel/emulation/src/xnu_syscall/bsd/impl"
fchownat_c="$impl/unistd/fchownat.c"
fchown_c="$impl/unistd/fchown.c"
lchown_c="$impl/unistd/lchown.c"
chown_c="$impl/unistd/chown.c"
test -f "$fchownat_c"
test -f "$fchown_c"
test -f "$lchown_c"
test -f "$chown_c"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p \
	"$tmp/include/darling/emulation/common/bsdthread" \
	"$tmp/include/darling/emulation/common" \
	"$tmp/include/darling/emulation/conversion" \
	"$tmp/include/darling/emulation/linux_premigration/linux-syscalls" \
	"$tmp/include/darling/emulation/linux_premigration" \
	"$tmp/include/darling/emulation/other/mach" \
	"$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd"

cat > "$tmp/include/darling/emulation/common/base.h" <<'H_EOF'
#pragma once
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
#define LINUX_AT_INVALID (-1)
int atfd(int fd);
int atflags_bsd_to_linux(int flags);
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
H_EOF

cat > "$tmp/include/darling/emulation/linux_premigration/linux-syscalls/linux.h" <<'H_EOF'
#pragma once
#define __NR_fchownat 260
#define __NR_lchown 94
#define LINUX_SYSCALL(n, ...) test_linux_syscall_unexpected()
long test_linux_syscall_unexpected(void);
H_EOF

cat > "$tmp/include/darling/emulation/other/mach/lkm.h" <<'H_EOF'
#pragma once
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/chown.h" <<'H_EOF'
#pragma once
long sys_chown(const char* path, int uid, int gid);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/fchown.h" <<'H_EOF'
#pragma once
long sys_fchown(int fd, int uid, int gid);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/fchownat.h" <<'H_EOF'
#pragma once
long sys_fchownat(int fd, const char* path, int uid, int gid, int flag);
H_EOF

cat > "$tmp/include/darling/emulation/xnu_syscall/bsd/impl/unistd/lchown.h" <<'H_EOF'
#pragma once
long sys_lchown(const char* path, int uid, int gid);
H_EOF

cat > "$tmp/harness.c" <<C_EOF
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

#include "$fchown_c"
#include "$fchownat_c"
#include "$lchown_c"
#include "$chown_c"

int get_perthread_wd(void) { return -100; }
int atfd(int fd) { return fd; }
int atflags_bsd_to_linux(int flags) { return flags; }
int errno_linux_to_bsd(int ret) { return ret; }
int vchroot_expand(struct vchroot_expand_args *args) {
	(void)args;
	fprintf(stderr, "NULL path reached vchroot_expand\\n");
	exit(3);
}
long test_linux_syscall_unexpected(void) {
	fprintf(stderr, "NULL path reached Linux syscall fallback\\n");
	exit(4);
}

static int expect_efault(const char *name, long got) {
	if (got == -EFAULT) {
		return 0;
	}
	fprintf(stderr, "%s returned %ld, expected -EFAULT\\n", name, got);
	return 1;
}

static int expect_enotsup(const char *name, long got) {
	if (got == -ENOTSUP) {
		return 0;
	}
	fprintf(stderr, "%s returned %ld, expected -ENOTSUP\\n", name, got);
	return 1;
}

int main(void) {
	int failed = 0;
	failed |= expect_enotsup("sys_fchown", sys_fchown(3, 0, 0));
	failed |= expect_efault("sys_fchownat", sys_fchownat(0, NULL, 0, 0, 0));
	failed |= expect_enotsup("sys_fchownat", sys_fchownat(0, "some-path", 0, 0, 0));
	failed |= expect_efault("sys_lchown", sys_lchown(NULL, 0, 0));
	failed |= expect_enotsup("sys_lchown", sys_lchown("some-path", 0, 0));
	failed |= expect_efault("sys_chown", sys_chown(NULL, 0, 0));
	failed |= expect_enotsup("sys_chown", sys_chown("some-path", 0, 0));
	return failed ? 1 : 0;
}
C_EOF

cc -std=gnu11 -Wall -Wextra -Werror -Wno-unused-parameter \
	-I "$tmp/include" \
	"$tmp/harness.c" \
	-o "$tmp/chown_disabled_null_guard_contract"
"$tmp/chown_disabled_null_guard_contract"

echo "PASS chown-disabled-null-guard-contract"
