#include <errno.h>
#include <stdio.h>
#include <stdlib.h>

#include <darling/emulation/linux_premigration/vchroot_expand.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/chown.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/fchown.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/fchownat.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/lchown.h>

int get_perthread_wd(void) { return -100; }
int atfd(int fd) { return fd; }
int atflags_bsd_to_linux(int flags) { return flags; }
int errno_linux_to_bsd(int ret) { return ret; }

int vchroot_expand(struct vchroot_expand_args *args)
{
	(void)args;
	fprintf(stderr, "NULL path reached vchroot_expand\n");
	exit(3);
}

long test_linux_syscall_unexpected(void)
{
	fprintf(stderr, "NULL path reached Linux syscall fallback\n");
	exit(4);
}

static int expect_efault(const char *name, long got)
{
	if (got == -EFAULT)
		return 0;
	fprintf(stderr, "%s returned %ld, expected -EFAULT\n", name, got);
	return 1;
}

static int expect_enotsup(const char *name, long got)
{
	if (got == -ENOTSUP)
		return 0;
	fprintf(stderr, "%s returned %ld, expected -ENOTSUP\n", name, got);
	return 1;
}

int main(void)
{
	int failed = 0;
	failed |= expect_enotsup("sys_fchown", sys_fchown(3, 0, 0));
	failed |= expect_efault("sys_fchownat", sys_fchownat(0, NULL, 0, 0, 0));
	failed |= expect_enotsup("sys_fchownat", sys_fchownat(0, "some-path", 0, 0, 0));
	failed |= expect_efault("sys_lchown", sys_lchown(NULL, 0, 0));
	failed |= expect_enotsup("sys_lchown", sys_lchown("some-path", 0, 0));
	failed |= expect_efault("sys_chown", sys_chown(NULL, 0, 0));
	failed |= expect_enotsup("sys_chown", sys_chown("some-path", 0, 0));
	if (!failed)
		puts("GREEN: chown disabled NULL guard contract");
	return failed ? 1 : 0;
}
