#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int
expect_open_errno(const char* path, int flags, int expected)
{
	errno = 0;
	int fd = open(path, flags, 0644);
	if (fd >= 0) {
		printf("open unexpectedly succeeded: %s\n", path);
		close(fd);
		return 1;
	}
	if (errno != expected) {
		printf("open errno mismatch for %s: got %s want %s\n",
		    path, strerror(errno), strerror(expected));
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* shadowed_child =
	    "/private/var/tmp/west-eunion-path-errors/shadow_dir/lower.txt";
	const char* escape_child =
	    "/private/var/tmp/west-eunion-path-errors/escape_link/new.txt";

	if (expect_open_errno(shadowed_child, O_RDONLY, ENOTDIR) != 0)
		return 1;
	if (expect_open_errno(escape_child, O_CREAT | O_WRONLY, EINVAL) != 0)
		return 1;
	puts("WEST_EUNION_PATH_ERRORS_OK");
	return 0;
}
