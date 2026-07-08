#include <errno.h>
#include <fcntl.h>
#include <stdbool.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

#include <darling/emulation/xnu_syscall/bsd/impl/unistd/dup.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/dup2.h>
#include <darling/emulation/xnu_syscall/bsd/impl/fcntl/fcntl.h>

static const int guarded_fd = 77;

bool guard_table_check(int fd, int flag)
{
	(void)flag;
	return fd == guarded_fd;
}

int errno_linux_to_bsd(int ret) { return ret; }
int oflags_bsd_to_linux(int flags) { return flags; }
int oflags_linux_to_bsd(int flags) { return flags; }

long fdpath(int fd, long buf, int len)
{
	(void)fd;
	(void)buf;
	(void)len;
	return -EBADF;
}

long sys_readlink(const char *path, char *buf, unsigned long bsize)
{
	(void)path;
	(void)buf;
	(void)bsize;
	return -EINVAL;
}

long test_linux_syscall_unexpected(void)
{
	fprintf(stderr, "guarded fd path reached Linux syscall fallback\n");
	_exit(3);
}

void __simple_kprintf(const char *fmt, ...) { (void)fmt; }
void __simple_abort(void) { abort(); }
void kqueue_dup(int oldfd, int newfd) { (void)oldfd; (void)newfd; }

static void expect_ebadf(const char *name, long got)
{
	if (got != -EBADF) {
		fprintf(stderr, "%s returned %ld, expected -EBADF\n", name, got);
		exit(1);
	}
}

static void child_dup2_to_guarded(void)
{
	expect_ebadf("sys_dup2(fd, guarded)", sys_dup2(STDOUT_FILENO, guarded_fd));
}

int main(void)
{
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
		fprintf(stderr, "sys_dup2(fd, guarded) aborted with signal %d\n", WTERMSIG(status));
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "sys_dup2(fd, guarded) child status 0x%x\n", status);
		return 1;
	}

	expect_ebadf("sys_dup(guarded)", sys_dup(guarded_fd));
	expect_ebadf("sys_dup2(guarded, fd)", sys_dup2(guarded_fd, STDOUT_FILENO));
	expect_ebadf("sys_fcntl_nocancel(guarded)", sys_fcntl_nocancel(guarded_fd, F_GETFD, 0));
	puts("GREEN: fd guard EBADF contract");
	return 0;
}
