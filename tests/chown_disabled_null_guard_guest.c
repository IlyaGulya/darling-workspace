#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>

static int expect_errno(const char *name, int rc, int expected)
{
	if (rc == -1 && errno == expected)
		return 0;

	fprintf(stderr, "%s: rc=%d errno=%d (%s), expected errno=%d (%s)\n",
			name, rc, errno, strerror(errno), expected, strerror(expected));
	return 1;
}

int main(void)
{
	char template[] = "/tmp/chown-disabled-null-guard.XXXXXX";
	int fd = mkstemp(template);
	int failed = 0;

	if (fd < 0) {
		perror("mkstemp");
		return 1;
	}

	errno = 0;
	failed |= expect_errno("chown(NULL)", chown((const char *)0, 0, 0), EFAULT);

	errno = 0;
	failed |= expect_errno("lchown(NULL)", lchown((const char *)0, 0, 0), EFAULT);

	errno = 0;
	failed |= expect_errno("fchownat(NULL)", fchownat(AT_FDCWD, (const char *)0, 0, 0, 0), EFAULT);

	errno = 0;
	failed |= expect_errno("chown(path)", chown(template, 0, 0), ENOTSUP);

	errno = 0;
	failed |= expect_errno("lchown(path)", lchown(template, 0, 0), ENOTSUP);

	errno = 0;
	failed |= expect_errno("fchownat(path)", fchownat(AT_FDCWD, template, 0, 0, 0), ENOTSUP);

	errno = 0;
	failed |= expect_errno("fchown(fd)", fchown(fd, 0, 0), ENOTSUP);

	close(fd);
	unlink(template);

	if (failed)
		return 1;

	puts("CHOWN_DISABLED_NULL_GUARD_GUEST_OK");
	return 0;
}
