#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/types.h>
#include <sys/xattr.h>
#include <unistd.h>

static int
expect_efault(const char *name, int rc)
{
	if (rc == -1 && errno == EFAULT) {
		return 0;
	}
	fprintf(stderr, "%s: rc=%d errno=%d (%s), expected EFAULT\n",
		name, rc, errno, strerror(errno));
	return 1;
}

int
main(void)
{
	int failed = 0;
	struct stat st;
	struct statfs sfs;
	struct timeval tv[2] = {{0, 0}, {0, 0}};
	char buf[32];

	errno = 0;
	failed |= expect_efault("lstat", lstat(NULL, &st));

	errno = 0;
	failed |= expect_efault("statfs", statfs(NULL, &sfs));

	errno = 0;
	failed |= expect_efault("utimes", utimes(NULL, tv));

	errno = 0;
	failed |= expect_efault("chdir", chdir(NULL));

	errno = 0;
	failed |= expect_efault("chmod", chmod(NULL, 0600));

	errno = 0;
	failed |= expect_efault("fchmodat", fchmodat(AT_FDCWD, NULL, 0600, 0));

	errno = 0;
	failed |= expect_efault("link-src", link(NULL, "/tmp/vchroot-pathnull-link"));

	errno = 0;
	failed |= expect_efault("link-dst", link("/tmp/vchroot-pathnull-missing", NULL));

	errno = 0;
	failed |= expect_efault("mknod", mknod(NULL, S_IFIFO | 0600, 0));

	errno = 0;
	failed |= expect_efault("readlinkat",
		(int)readlinkat(AT_FDCWD, NULL, buf, sizeof(buf)));

	errno = 0;
	failed |= expect_efault("truncate", truncate(NULL, 0));

	errno = 0;
	failed |= expect_efault("unlink", unlink(NULL));

	errno = 0;
	failed |= expect_efault("getxattr",
		(int)getxattr(NULL, "user.vchroot_pathnull", buf, sizeof(buf), 0, 0));

	errno = 0;
	failed |= expect_efault("listxattr",
		(int)listxattr(NULL, buf, sizeof(buf), 0));

	if (failed) {
		return 1;
	}
	puts("VCHROOT_PATHNULL_GUARD_GUEST_OK");
	return 0;
}
