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
is_absent_xattr_errno(int error)
{
	return error == ENOATTR || error == ENODATA;
}

static ssize_t
guest_getxattr(const char* path, const char* name, void* value, size_t size)
{
#ifdef __APPLE__
	return getxattr(path, name, value, size, 0, 0);
#else
	return getxattr(path, name, value, size);
#endif
}

static ssize_t
guest_listxattr(const char* path, char* names, size_t size)
{
#ifdef __APPLE__
	return listxattr(path, names, size, 0);
#else
	return listxattr(path, names, size);
#endif
}

static int
guest_removexattr(const char* path, const char* name)
{
#ifdef __APPLE__
	return removexattr(path, name, 0);
#else
	return removexattr(path, name);
#endif
}

static int
guest_fremovexattr(int fd, const char* name)
{
#ifdef __APPLE__
	return fremovexattr(fd, name, 0);
#else
	return fremovexattr(fd, name);
#endif
}

static int
expect_marker_hidden(const char* path)
{
	char value[32];
	errno = 0;
	if (guest_getxattr(path, "user.union.opaque", value, sizeof(value)) >= 0) {
		printf("union marker unexpectedly visible on %s\n", path);
		return 1;
	}
	if (!is_absent_xattr_errno(errno)) {
		printf("getxattr marker errno mismatch on %s: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

static int
expect_no_marker_in_list(const char* path)
{
	char names[1024] = {0};
	ssize_t length = guest_listxattr(path, names, sizeof(names));
	if (length < 0) {
		printf("listxattr %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	for (ssize_t offset = 0; offset < length;) {
		const char* name = names + offset;
		size_t name_length = strlen(name);
		if (strncmp(name, "user.union.", strlen("user.union.")) == 0) {
			printf("union marker leaked from %s: %s\n", path, name);
			return 1;
		}
		offset += (ssize_t)name_length + 1;
	}
	return 0;
}

static int
expect_marker_rejected(const char* path)
{
	errno = 0;
	if (guest_removexattr(path, "user.union.opaque") == 0) {
		printf("removexattr marker unexpectedly succeeded on %s\n", path);
		return 1;
	}
	if (errno != EPERM) {
		printf("removexattr marker errno mismatch on %s: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

static int
expect_absent_xattr(const char* path, const char* name)
{
	char value[64];
	errno = 0;
	if (guest_getxattr(path, name, value, sizeof(value)) >= 0) {
		printf("xattr unexpectedly remains on %s: %s\n", path, name);
		return 1;
	}
	if (!is_absent_xattr_errno(errno)) {
		printf("xattr absence errno mismatch on %s %s: %s\n",
		    path, name, strerror(errno));
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* opaque = "/private/var/tmp/west-eunion-xattr/opaque";
	const char* path_remove = "/private/var/tmp/west-eunion-xattr/path_remove.txt";
	const char* fd_remove = "/private/var/tmp/west-eunion-xattr/fd_remove.txt";

	if (rmdir(opaque) != 0) {
		printf("rmdir lower opaque directory failed: %s\n", strerror(errno));
		return 1;
	}
	if (mkdir(opaque, 0755) != 0) {
		printf("mkdir opaque directory failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_marker_hidden(opaque) != 0 ||
	    expect_no_marker_in_list(opaque) != 0 ||
	    expect_marker_rejected(opaque) != 0)
		return 1;

	if (guest_removexattr(path_remove, "user.test.remove") != 0) {
		printf("path removexattr failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_absent_xattr(path_remove, "user.test.remove") != 0)
		return 1;

	int fd = open(fd_remove, O_RDONLY);
	if (fd < 0) {
		printf("open fd_remove failed: %s\n", strerror(errno));
		return 1;
	}
	if (guest_fremovexattr(fd, "user.test.fdremove") != 0) {
		printf("fremovexattr failed: %s\n", strerror(errno));
		close(fd);
		return 1;
	}
	close(fd);
	if (expect_absent_xattr(fd_remove, "user.test.fdremove") != 0)
		return 1;

	puts("WEST_EUNION_XATTR_POLICY_OK");
	return 0;
}
