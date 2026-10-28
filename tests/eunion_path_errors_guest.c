#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define PATH_ERRORS "/usr/share/darling/tests/eunion/path-errors"

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

static int
expect_same_object(const char* canonical, const char* alias)
{
	struct stat direct;
	struct stat rooted;
	if (stat(canonical, &direct) != 0 || stat(alias, &rooted) != 0) {
		printf("stat namespace identity failed: %s\n", strerror(errno));
		return 1;
	}
	if (direct.st_dev != rooted.st_dev || direct.st_ino != rooted.st_ino ||
	    (direct.st_mode & S_IFMT) != (rooted.st_mode & S_IFMT)) {
		printf("namespace identity mismatch: %s versus %s\n", canonical, alias);
		return 1;
	}
	return 0;
}

static int
expect_contents(const char* path, const char* expected)
{
	char contents[128];
	int fd = open(path, O_RDONLY);
	if (fd < 0) {
		printf("open %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	ssize_t length = read(fd, contents, sizeof(contents));
	int closed = close(fd);
	if (length != (ssize_t)strlen(expected) ||
	    (length >= 0 && memcmp(contents, expected, (size_t)length) != 0)) {
		printf("contents mismatch: %s\n", path);
		return 1;
	}
	if (closed != 0) {
		printf("close %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* shadow = PATH_ERRORS "/shadow_dir";
	const char* shadowed_child = PATH_ERRORS "/shadow_dir/lower.txt";
	const char* root_link = PATH_ERRORS "/guest-root";
	const char* canonical_created = PATH_ERRORS "/root-created.txt";
	const char* rooted_created =
	    PATH_ERRORS "/guest-root" PATH_ERRORS "/root-created.txt";
	const char* rooted_shadowed_child =
	    PATH_ERRORS "/guest-root" PATH_ERRORS "/shadow_dir/lower.txt";
	const char payload[] = "ROOTED_GUEST_WRITE\n";

	if (expect_open_errno(shadowed_child, O_RDONLY, ENOTDIR) != 0 ||
	    expect_open_errno(shadowed_child, O_CREAT | O_WRONLY | O_TRUNC, ENOTDIR) != 0)
		return 1;

	/*
	 * An absolute symlink restarts at the process's guest root, not the
	 * native host root. Prove the directory identity before attempting a
	 * write, then prove both spellings name the same writable guest object.
	 * This does not depend on which errno rejects an above-root "..".
	 */
	if (symlink("/", root_link) != 0) {
		printf("create guest-root symlink failed: %s\n", strerror(errno));
		return 1;
	}
	if (expect_same_object("/", root_link) != 0)
		return 1;

	int fd = open(rooted_created, O_CREAT | O_EXCL | O_WRONLY, 0600);
	if (fd < 0) {
		printf("create through guest root failed: %s\n", strerror(errno));
		return 1;
	}
	ssize_t written = write(fd, payload, sizeof(payload) - 1);
	int closed = close(fd);
	if (written != (ssize_t)(sizeof(payload) - 1) || closed != 0) {
		printf("write through guest root failed\n");
		return 1;
	}
	if (expect_same_object(canonical_created, rooted_created) != 0 ||
	    expect_contents(canonical_created, payload) != 0 ||
	    expect_contents(rooted_created, payload) != 0 ||
	    expect_open_errno(canonical_created, O_CREAT | O_EXCL | O_WRONLY, EEXIST) != 0)
		return 1;

	/* Rejected writes must preserve the upper blocker and the hidden lower child. */
	if (expect_open_errno(rooted_shadowed_child, O_CREAT | O_WRONLY | O_TRUNC, ENOTDIR) != 0 ||
	    expect_contents(shadow, "UPPER_NON_DIRECTORY\n") != 0)
		return 1;
	puts("WEST_EUNION_PATH_ERRORS_OK");
	return 0;
}
