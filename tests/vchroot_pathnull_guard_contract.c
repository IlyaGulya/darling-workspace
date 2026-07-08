#include <errno.h>
#include <fcntl.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include <darling/emulation/conversion/stat/common.h>
#include <darling/emulation/linux_premigration/vchroot_expand.h>
#include <darling/emulation/common/simple.h>

int get_perthread_wd(void) { return -100; }
int atfd(int fd) { return fd; }
int atflags_bsd_to_linux(int flags) { return flags; }
int errno_linux_to_bsd(int ret) { return ret; }

long test_linux_syscall_unexpected(void)
{
	fprintf(stderr, "NULL path reached Linux syscall fallback\n");
	_exit(4);
}

int vchroot_expand(struct vchroot_expand_args *args)
{
	(void)args;
	fprintf(stderr, "NULL path reached vchroot_expand\n");
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
int __simple_readline(int fd, struct simple_readline_buf *buf, char *line, unsigned long size)
{
	(void)fd;
	(void)buf;
	(void)line;
	(void)size;
	return 0;
}

static void expect_efault(const char *name, long got)
{
	if (got != -EFAULT) {
		fprintf(stderr, "%s returned %ld, expected -EFAULT\n", name, got);
		_exit(1);
	}
}

static void run_case(const char *name)
{
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

int main(void)
{
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
			fprintf(stderr, "case %s died with signal %d\n", cases[i], WTERMSIG(status));
			return 1;
		}
		if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
			fprintf(stderr, "case %s exited with status 0x%x\n", cases[i], status);
			return 1;
		}
	}

	puts("GREEN: vchroot NULL path guard contract");
	return 0;
}
