/* Stock Homebrew mktemp.rb calls directory.chown(nil, current_group). */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int failed;
static void expect(const char *name, int rc, int error)
{
	int saved = errno;
	if ((error == 0 && rc == 0) || (error != 0 && rc == -1 && saved == error))
		return;
	fprintf(stderr, "%s: rc=%d errno=%d (%s), expected %d\n",
		name, rc, saved, strerror(saved), error);
	failed = 1;
}
static void owned(const char *path, mode_t type, int nofollow)
{
	struct stat st;
	int rc = nofollow ? lstat(path, &st) : stat(path, &st);
	if (rc != 0 || (st.st_mode & S_IFMT) != type ||
		st.st_uid != getuid() || st.st_gid != getgid()) {
		fprintf(stderr, "ownership/type mismatch: %s\n", path);
		failed = 1;
	}
}
int main(void)
{
	char dir[] = "/private/var/tmp/chown-ownership.XXXXXX";
	char file[sizeof(dir) + 16], link[sizeof(dir) + 16];
	if (getuid() == 0 || geteuid() != getuid()) {
		fprintf(stderr, "fixture requires an ordinary rootless user\n");
		return 1;
	}
	if (!mkdtemp(dir)) { perror("mkdtemp"); return 1; }
	snprintf(file, sizeof(file), "%s/file", dir);
	snprintf(link, sizeof(link), "%s/link", dir);
	int dfd = open(dir, O_RDONLY);
	int fd = open(file, O_CREAT | O_EXCL | O_RDWR, 0600);
	if (dfd < 0 || fd < 0 || symlink("file", link) != 0) {
		perror("fixture setup"); failed = 1; goto cleanup;
	}
	/* NULL guard coverage survives the disabled-ownership contract retirement. */
	expect("chown(NULL)", chown((const char *)0, -1, getgid()), EFAULT);
	expect("lchown(NULL)", lchown((const char *)0, -1, getgid()), EFAULT);
	expect("fchownat(NULL)", fchownat(AT_FDCWD, (const char *)0, -1, getgid(), 0), EFAULT);
	expect("mktemp directory chown(nil, group)", chown(dir, (uid_t)-1, getgid()), 0);
	owned(dir, S_IFDIR, 0);
	expect("directory fchown", fchown(dfd, (uid_t)-1, getgid()), 0);
	expect("relative fchownat", fchownat(dfd, "file", (uid_t)-1, getgid(), 0), 0);
	owned(file, S_IFREG, 0);
	expect("fd fchown", fchown(fd, (uid_t)-1, getgid()), 0);
	expect("lchown link", lchown(link, (uid_t)-1, getgid()), 0);
	expect("nofollow fchownat", fchownat(dfd, "link", (uid_t)-1, getgid(), AT_SYMLINK_NOFOLLOW), 0);
	owned(link, S_IFLNK, 1);
	expect("follow chown", chown(link, (uid_t)-1, getgid()), 0);
	expect("denied owner change", chown(file, 0, (gid_t)-1), EPERM);
	expect("denied fd owner change", fchown(fd, 0, (gid_t)-1), EPERM);
	owned(file, S_IFREG, 0);
	expect("invalid fd", fchown(-1, (uid_t)-1, getgid()), EBADF);
	expect("invalid dirfd", fchownat(-1, "file", (uid_t)-1, getgid(), 0), EBADF);
	expect("non-directory dirfd", fchownat(fd, "file", (uid_t)-1, getgid(), 0), ENOTDIR);
	expect("invalid flags", fchownat(dfd, "file", (uid_t)-1, getgid(), 0x40000000), EINVAL);
	if (fcntl(fd, F_GETFD) < 0 || fcntl(dfd, F_GETFD) < 0) {
		fprintf(stderr, "ownership syscall closed caller descriptor\n"); failed = 1;
	}
	/* A dangling link has ownership; following it must still report ENOENT. */
	expect("unlink target", unlink(file), 0);
	expect("dangling lchown", lchown(link, (uid_t)-1, getgid()), 0);
	expect("dangling chown", chown(link, (uid_t)-1, getgid()), ENOENT);
cleanup:
	if (fd >= 0) expect("close file", close(fd), 0);
	if (dfd >= 0) expect("close directory", close(dfd), 0);
	if (unlink(link) != 0 && errno != ENOENT) { perror("unlink link"); failed = 1; }
	if (unlink(file) != 0 && errno != ENOENT) { perror("unlink file"); failed = 1; }
	expect("rmdir fixture", rmdir(dir), 0);
	if (failed) return 1;
	puts("CHOWN_OWNERSHIP_GUEST_OK");
	return 0;
}
