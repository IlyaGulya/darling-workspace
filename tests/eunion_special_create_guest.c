#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int
expect_kind(const char* path, mode_t kind)
{
	struct stat st;
	if (lstat(path, &st) != 0) {
		printf("lstat %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if ((st.st_mode & S_IFMT) != kind) {
		printf("kind mismatch for %s: %o\n", path, st.st_mode);
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* link = "/private/var/tmp/west-eunion-special/link_parent/new-link";
	const char* fifo = "/private/var/tmp/west-eunion-special/link_parent/new-fifo";
	const char* node = "/private/var/tmp/west-eunion-special/link_parent/new-node";
	char target[64] = {0};

	if (symlinkat("target", AT_FDCWD, link) != 0) {
		printf("symlinkat through lower parent failed: %s\n", strerror(errno));
		return 1;
	}
	ssize_t length = readlink(link, target, sizeof(target) - 1);
	if (length < 0) {
		printf("readlink new symlink failed: %s\n", strerror(errno));
		return 1;
	}
	target[length] = '\0';
	if (strcmp(target, "target") != 0) {
		printf("symlink target mismatch: %s\n", target);
		return 1;
	}

	if (mkfifo(fifo, 0644) != 0) {
		printf("mkfifo through lower parent failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_kind(fifo, S_IFIFO) != 0)
		return 1;

	if (mknod(node, S_IFIFO | 0600, 0) != 0) {
		printf("mknod through lower parent failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_kind(node, S_IFIFO) != 0)
		return 1;

	puts("WEST_EUNION_SPECIAL_CREATE_OK");
	return 0;
}
