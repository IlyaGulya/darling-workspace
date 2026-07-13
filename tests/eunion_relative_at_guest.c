#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef AT_REMOVEDIR
#define AT_REMOVEDIR 0x200
#endif

static int
read_exact(const char* path, const char* expected)
{
	char buf[128] = {0};
	int fd = open(path, O_RDONLY);
	if (fd < 0) {
		printf("open %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n < 0) {
		printf("read %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (strcmp(buf, expected) != 0) {
		printf("content mismatch for %s: got %s\n", path, buf);
		return 1;
	}
	return 0;
}

static int
expect_absent(const char* path)
{
	if (access(path, F_OK) == 0) {
		printf("expected absent but exists: %s\n", path);
		return 1;
	}
	if (errno != ENOENT) {
		printf("access %s failed with unexpected errno: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

static int
write_exact(int fd, const char* text)
{
	size_t length = strlen(text);
	ssize_t written = write(fd, text, length);
	if (written != (ssize_t)length) {
		printf("short write: %zd/%zu (%s)\n", written, length, strerror(errno));
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* root = "/private/var/tmp/west-eunion-relative-at";
	const char* unlink_path = "/private/var/tmp/west-eunion-relative-at/unlink.txt";
	const char* rename_src = "/private/var/tmp/west-eunion-relative-at/rename_src/child.txt";
	const char* rename_dst = "/private/var/tmp/west-eunion-relative-at/rename_dst/child.txt";
	const char* created = "/private/var/tmp/west-eunion-relative-at/created/new.txt";
	const char* rmdir_target = "/private/var/tmp/west-eunion-relative-at/rmdir_target";

	int dirfd = open(root, O_RDONLY);
	if (dirfd < 0) {
		printf("open directory %s failed: %s\n", root, strerror(errno));
		return 1;
	}

	int fd = openat(dirfd, "open_existing.txt", O_RDONLY);
	if (fd < 0) {
		printf("openat existing file failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	close(fd);
	if (read_exact("/private/var/tmp/west-eunion-relative-at/open_existing.txt",
	    "OPENAT_EXISTING\n") != 0) {
		close(dirfd);
		return 1;
	}

	if (unlinkat(dirfd, "unlink.txt", 0) != 0) {
		printf("unlinkat lower-only file failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	if (expect_absent(unlink_path) != 0) {
		close(dirfd);
		return 1;
	}

	if (mkdirat(dirfd, "created", 0755) != 0) {
		printf("mkdirat created failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	fd = openat(dirfd, "created/new.txt", O_CREAT | O_EXCL | O_WRONLY, 0644);
	if (fd < 0) {
		printf("openat create failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	if (write_exact(fd, "OPENAT_CREATED\n") != 0 || close(fd) != 0) {
		printf("writing openat-created file failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	if (read_exact(created, "OPENAT_CREATED\n") != 0) {
		close(dirfd);
		return 1;
	}

	if (renameat(dirfd, "rename_src", dirfd, "rename_dst") != 0) {
		printf("renameat lower-only directory failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	if (expect_absent(rename_src) != 0 || read_exact(rename_dst, "RENAMEAT_CHILD\n") != 0) {
		close(dirfd);
		return 1;
	}

	if (mkdirat(dirfd, "rmdir_target", 0755) != 0) {
		printf("mkdirat rmdir target failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	if (unlinkat(dirfd, "rmdir_target", AT_REMOVEDIR) != 0) {
		printf("unlinkat AT_REMOVEDIR failed: %s\n", strerror(errno));
		close(dirfd);
		return 1;
	}
	if (expect_absent(rmdir_target) != 0) {
		close(dirfd);
		return 1;
	}

	close(dirfd);
	puts("WEST_EUNION_RELATIVE_AT_OK");
	return 0;
}
