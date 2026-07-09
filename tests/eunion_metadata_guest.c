#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
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

int
main(void)
{
	const char* setid = "/private/var/tmp/west-eunion-meta/setid.txt";
	const char* fdmeta = "/private/var/tmp/west-eunion-meta/fdmeta.txt";
	const char* fdxattr = "/private/var/tmp/west-eunion-meta/fdxattr.txt";
	const char* pathxattr = "/private/var/tmp/west-eunion-meta/pathxattr.txt";
	const char* ftrunc = "/private/var/tmp/west-eunion-meta/ftrunc.txt";

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
