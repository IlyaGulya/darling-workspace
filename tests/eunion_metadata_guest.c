#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

#ifndef ENOATTR
#define ENOATTR 93
#endif

static int
expect_mode(const char* path, mode_t expected)
{
	struct stat st;
	if (stat(path, &st) != 0) {
		printf("stat %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	mode_t got = st.st_mode & 07777;
	if (got != expected) {
		printf("mode mismatch for %s: got %o want %o\n", path, got, expected);
		return 1;
	}
	return 0;
}

static int
expect_size(const char* path, off_t expected)
{
	struct stat st;
	if (stat(path, &st) != 0) {
		printf("stat %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (st.st_size != expected) {
		printf("size mismatch for %s: got %lld want %lld\n",
		    path, (long long)st.st_size, (long long)expected);
		return 1;
	}
	return 0;
}

static int
expect_xattr(const char* path, const char* name, const char* expected)
{
	char got[128] = {0};
	ssize_t n = getxattr(path, name, got, sizeof(got) - 1, 0, 0);
	if (n < 0) {
		printf("getxattr %s %s failed: %s\n", path, name, strerror(errno));
		return 1;
	}
	(void)n;
	if (strcmp(got, expected) != 0) {
		printf("xattr mismatch for %s %s: got %s want %s\n", path, name, got, expected);
		return 1;
	}
	return 0;
}

static int
append_byte(const char* path)
{
	int fd = open(path, O_WRONLY | O_APPEND);
	if (fd < 0) {
		printf("open append %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (write(fd, "X", 1) != 1) {
		printf("append %s failed: %s\n", path, strerror(errno));
		close(fd);
		return 1;
	}
	if (close(fd) != 0) {
		printf("close append %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

static int
expect_fd_times(const char* path, int fd)
{
	struct timeval times[2] = {{111222333, 123456}, {111222334, 654321}};
	struct stat st;
	int failed = 0;
	int alias = dup(fd);
	if (alias < 0) {
		printf("duplicate metadata fd failed: %s\n", strerror(errno));
		return 1;
	}
	if (futimes(fd, times) != 0) {
		printf("futimes exact timestamps failed: %s\n", strerror(errno));
		failed = 1;
	}
	if (stat(path, &st) != 0 || st.st_atimespec.tv_sec != times[0].tv_sec ||
	    st.st_atimespec.tv_nsec != times[0].tv_usec * 1000L ||
	    st.st_mtimespec.tv_sec != times[1].tv_sec ||
	    st.st_mtimespec.tv_nsec != times[1].tv_usec * 1000L) {
		printf("futimes must preserve microsecond precision in pathname metadata\n");
		failed = 1;
	}
	if (fstat(fd, &st) != 0 || st.st_mtimespec.tv_sec != times[1].tv_sec ||
	    st.st_mtimespec.tv_nsec != times[1].tv_usec * 1000L ||
	    fstat(alias, &st) != 0 || st.st_mtimespec.tv_sec != times[1].tv_sec ||
	    st.st_mtimespec.tv_nsec != times[1].tv_usec * 1000L) {
		printf("futimes must update the same opened inode through fd aliases\n");
		failed = 1;
	}
	struct timeval before, after;
	if (gettimeofday(&before, NULL) != 0) {
		close(alias);
		return 1;
	}
	/* A bad NULL implementation must fail this case, not prevent the parent
	 * from reporting the exact-timestamp and opened-description failures. */
	pid_t child = fork();
	if (child == 0)
		_exit(futimes(alias, NULL) == 0 ? 0 : 2);
	int status = 0;
	pid_t waited = -1;
	if (child > 0) {
		do {
			waited = waitpid(child, &status, 0);
		} while (waited < 0 && errno == EINTR);
	}
	if (child < 0 || waited != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		printf("futimes NULL must set current time without terminating the process\n");
		failed = 1;
	}
	if (gettimeofday(&after, NULL) != 0 || stat(path, &st) != 0 ||
	    st.st_mtimespec.tv_sec < before.tv_sec - 1 ||
	    st.st_mtimespec.tv_sec > after.tv_sec + 1 ||
	    fstat(fd, &st) != 0 || st.st_mtimespec.tv_sec < before.tv_sec - 1 ||
	    st.st_mtimespec.tv_sec > after.tv_sec + 1) {
		printf("futimes NULL must publish current time to path and original fd\n");
		failed = 1;
	}
	close(alias);
	return failed;
}

int
main(void)
{
	const char* setid = "/usr/share/darling/tests/eunion/metadata/setid.txt";
	const char* fdmeta = "/usr/share/darling/tests/eunion/metadata/fdmeta.txt";
	const char* fdxattr = "/usr/share/darling/tests/eunion/metadata/fdxattr.txt";
	const char* pathxattr = "/usr/share/darling/tests/eunion/metadata/pathxattr.txt";
	const char* ftrunc = "/usr/share/darling/tests/eunion/metadata/ftrunc.txt";

	if (append_byte(setid) != 0)
		return 1;
	if (expect_mode(setid, 0755) != 0)
		return 1;
	if (expect_xattr(setid, "user.test.tag", "hello") != 0)
		return 1;

	int fd = open(fdmeta, O_RDONLY);
	if (fd < 0) {
		printf("open fdmeta failed: %s\n", strerror(errno));
		return 1;
	}
	if (fchmod(fd, 0600) != 0) {
		printf("fchmod fdmeta failed: %s\n", strerror(errno));
		close(fd);
		return 1;
	}
	close(fd);
	if (expect_mode(fdmeta, 0600) != 0)
		return 1;

	fd = open(fdxattr, O_RDONLY);
	if (fd < 0) {
		printf("open fdxattr failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_fd_times(fdxattr, fd) != 0) {
		close(fd);
		return 1;
	}
	if (fsetxattr(fd, "user.test.fd", "FDVALUE", strlen("FDVALUE"), 0, 0) != 0) {
		printf("fsetxattr failed: %s\n", strerror(errno));
		close(fd);
		return 1;
	}
	close(fd);
	if (expect_xattr(fdxattr, "user.test.fd", "FDVALUE") != 0)
		return 1;

	if (setxattr(pathxattr, "user.test.path", "PATHVALUE", strlen("PATHVALUE"), 0, 0) != 0) {
		printf("setxattr path failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_xattr(pathxattr, "user.test.path", "PATHVALUE") != 0)
		return 1;

	errno = 0;
	if (setxattr(pathxattr, "user.union.whiteout", "y", 1, 0, 0) == 0) {
		printf("marker setxattr unexpectedly succeeded\n");
		return 1;
	}
	if (errno != EPERM) {
		printf("marker setxattr errno mismatch: %s\n", strerror(errno));
		return 1;
	}

	fd = open(ftrunc, O_RDWR);
	if (fd < 0) {
		printf("open ftrunc failed: %s\n", strerror(errno));
		return 1;
	}
	if (ftruncate(fd, 4) != 0) {
		printf("ftruncate failed: %s\n", strerror(errno));
		close(fd);
		return 1;
	}
	close(fd);
	if (expect_size(ftrunc, 4) != 0)
		return 1;

	puts("WEST_EUNION_METADATA_OK");
	return 0;
}
