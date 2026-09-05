/* Real ownership syscall sites + real EUNION copy-up, no modeled filesystem. */
#define TEST 1
#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include <time.h>

#define LINUX_ENOTDIR ENOTDIR
#define LINUX_EIO EIO
#define LINUX_EMFILE EMFILE
int eunion_test_perthread_wd = AT_FDCWD;
int dserver_rpc_vchroot_path(char *path, unsigned long size, uint64_t *token)
{
	(void)path; (void)size; (void)token;
	return -1;
}
void __simple_abort(void) { __builtin_trap(); }
int __simple_printf(const char *format, ...) { (void)format; return 0; }
#define main vchroot_unit_main
#include "vchroot_userspace.c"
#undef main

#include <darling/emulation/xnu_syscall/bsd/impl/unistd/chown.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/fchown.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/fchownat.h>
#include <darling/emulation/xnu_syscall/bsd/impl/unistd/lchown.h>
/* Darwin ABI constants; host headers intentionally retain Linux values. */
#define GUEST_AT_FDCWD (-2)
#define GUEST_AT_SYMLINK_NOFOLLOW 0x20

/* Only ABI transport is adapted: every operation reaches the host kernel. */
long linux_syscall(long a, long b, long c, long d, long e, long f, int number)
{
	long result = syscall(number, a, b, c, d, e, f);
	return result < 0 ? -errno : result;
}

static void require(int condition, const char *message)
{
	if (!condition) {
		fprintf(stderr, "FAIL ownership: %s (errno=%d)\n", message, errno);
		exit(1);
	}
}
static void expect(const char *message, long got, long expected)
{
	if (got != expected) {
		fprintf(stderr, "FAIL ownership: %s: got %ld expected %ld\n", message, got, expected);
		exit(1);
	}
}
static void path_join(char *out, const char *root, const char *name)
{
	require(snprintf(out, 4096, "%s/%s", root, name) < 4096, "fixture path fits");
}
static void make_file(const char *root, const char *name)
{
	char path[4096];
	path_join(path, root, name);
	int fd = open(path, O_CREAT | O_EXCL | O_WRONLY, 0700);
	require(fd >= 0, "create fixture file");
	require(write(fd, "LOWER ownership sentinel\n", 25) == 25, "write fixture payload");
	require(close(fd) == 0, "close fixture file");
}
static void make_link(const char *root, const char *name, const char *target)
{
	char path[4096];
	path_join(path, root, name);
	require(symlink(target, path) == 0, "create fixture symlink");
}
static struct stat status(const char *root, const char *name)
{
	char path[4096];
	struct stat st;
	path_join(path, root, name);
	require(lstat(path, &st) == 0, name);
	return st;
}
static int same_time(struct timespec a, struct timespec b)
{
	return a.tv_sec == b.tv_sec && a.tv_nsec == b.tv_nsec;
}
static void unchanged(const char *root, const char *name, struct stat before)
{
	struct stat after = status(root, name);
	require(after.st_dev == before.st_dev && after.st_ino == before.st_ino &&
		after.st_uid == before.st_uid && after.st_gid == before.st_gid &&
		after.st_mode == before.st_mode && after.st_nlink == before.st_nlink &&
		after.st_size == before.st_size && same_time(after.st_mtim, before.st_mtim) &&
		same_time(after.st_ctim, before.st_ctim), name);
}
static void upper_owned(const char *name, mode_t type)
{
	struct stat st = status(prefix_path, name);
	require((st.st_mode & S_IFMT) == type && st.st_uid == getuid() &&
		st.st_gid == getgid(), "upper object has requested ownership and type");
}
static void absent_upper(const char *name)
{
	char path[4096]; struct stat st;
	path_join(path, prefix_path, name);
	require(lstat(path, &st) == -1 && errno == ENOENT, "nofollow leaves target uncopied");
}
static void check_link(const char *name, const char *target)
{
	char path[4096], text[4096];
	path_join(path, prefix_path, name);
	ssize_t size = readlink(path, text, sizeof(text));
	require(size == (ssize_t)strlen(target) && memcmp(text, target, size) == 0,
		"copy-up preserves symlink text");
	upper_owned(name, S_IFLNK);
}
static int open_host(const char *root, const char *name, int flags)
{
	char path[4096]; path_join(path, root, name);
	int fd = open(path, flags);
	require(fd >= 0, "open fixture descriptor");
	return fd;
}
static int descriptor_count(void)
{
	DIR *dir = opendir("/proc/self/fd");
	require(dir != NULL, "open descriptor inventory");
	int count = 0;
	struct dirent *entry;
	while ((entry = readdir(dir)) != NULL)
		if (entry->d_name[0] != '.') count++;
	require(closedir(dir) == 0, "close descriptor inventory");
	return count;
}

int main(int argc, char **argv)
{
	require(argc == 3, "usage: fixture UPPER LOWER (precreated absolute directories)");
	require(getuid() != 0 && geteuid() == getuid(), "run as ordinary non-root user");
	strcpy(prefix_path, argv[1]); prefix_path_len = strlen(prefix_path);
	set_libexec_path(argv[2]);
	const char *files[] = {"absolute", "relative", "cwd", "fd", "follow-target",
		"at-follow-target", "nofollow-target", "at-nofollow-target", "denied", "invalid",
		"absolute-link-target", "case-target"};
	const char *links[] = {"follow", "at-follow", "nofollow", "at-nofollow", "dangling",
		"absolute-link"};
	const char *targets[] = {"follow-target", "at-follow-target", "nofollow-target",
		"at-nofollow-target", "missing-target", "/absolute-link-target"};
	struct stat file_before[sizeof(files) / sizeof(*files)];
	struct stat link_before[sizeof(links) / sizeof(*links)];
	for (size_t i = 0; i < sizeof(files) / sizeof(*files); i++) {
		make_file(libexec_path, files[i]); file_before[i] = status(libexec_path, files[i]);
	}
	for (size_t i = 0; i < sizeof(links) / sizeof(*links); i++) {
		make_link(libexec_path, links[i], targets[i]); link_before[i] = status(libexec_path, links[i]);
	}
	char path[4096]; path_join(path, libexec_path, "directory");
	require(mkdir(path, 0700) == 0, "create lower directory");
	struct stat directory_before = status(libexec_path, "directory");
	make_file(prefix_path, "upper");
	make_link(prefix_path, "CaseLink", "case-target");
	make_link(prefix_path, "CaseDangling", "missing-case-target");
	path_join(path, prefix_path, "upper");
	require(chmod(path, 04700) == 0, "set upper setuid bit");
	struct stat lower_root_before = status(libexec_path, ".");
	int dfd = open_host(libexec_path, ".", O_RDONLY | O_DIRECTORY);
	int fd = open_host(libexec_path, "fd", O_RDONLY);
	int descriptors_before = descriptor_count();

	expect("NULL chown", sys_chown(NULL, -1, getgid()), -EFAULT);
	expect("NULL lchown", sys_lchown(NULL, -1, getgid()), -EFAULT);
	expect("NULL fchownat", sys_fchownat(dfd, NULL, -1, getgid(), 0), -EFAULT);
	/* The earlier NULL-guard patch can reuse this production closure without
	 * asserting ownership support that its historical fixed source did not have. */
	if (getenv("CHOWN_NULL_GUARD_ONLY")) {
		require(close(fd) == 0 && close(dfd) == 0, "close NULL-guard fixture descriptors");
		puts("EUNION_OWNERSHIP_NULL_GUARD_OK");
		return 0;
	}
	/* First supported call is the source-base RED: old ENOTSUP is not a link failure. */
	expect("upper chown must reach Linux", sys_chown("/upper", -1, getgid()), 0);
	require((status(prefix_path, "upper").st_mode & S_ISUID) == 0,
		"real chown clears setuid even when uid/gid are unchanged");
	icase_enabled = 1;
	expect("case-folded chown follows target", sys_chown("/caselink", -1, getgid()), 0);
	upper_owned("case-target", S_IFREG);
	expect("case-folded dangling chown fails", sys_chown("/casedangling", -1, getgid()), -ENOENT);
	expect("case-folded lchown keeps link", sys_lchown("/casedangling", -1, getgid()), 0);
	check_link("CaseDangling", "missing-case-target");
	icase_enabled = 0;
	expect("absolute LOWER chown", sys_chown("/absolute", -1, getgid()), 0);
	upper_owned("absolute", S_IFREG);
	expect("dirfd-relative chown", sys_fchownat(dfd, "relative", -1, getgid(), 0), 0);
	upper_owned("relative", S_IFREG);
	require(chdir(libexec_path) == 0, "enter lower guest cwd");
	expect("guest cwd chown", sys_chown("cwd", -1, getgid()), 0);
	upper_owned("cwd", S_IFREG);
	expect("absolute path ignores bad dirfd", sys_fchownat(-1, "/directory", -1, getgid(), 0), 0);
	upper_owned("directory", S_IFDIR);
	expect("lower fd ownership", sys_fchown(fd, -1, getgid()), 0);
	upper_owned("fd", S_IFREG);
	require(fcntl(fd, F_GETFD) >= 0, "fchown preserves caller fd");
	struct stat fd_after; require(fstat(fd, &fd_after) == 0, "original fd still valid");
	require(fd_after.st_ino == file_before[3].st_ino &&
		same_time(fd_after.st_ctim, file_before[3].st_ctim), "original lower fd inode unchanged");

	expect("chown follows symlink", sys_chown("/follow", -1, getgid()), 0);
	upper_owned("follow-target", S_IFREG);
	expect("fchownat follows symlink", sys_fchownat(dfd, "at-follow", -1, getgid(), 0), 0);
	upper_owned("at-follow-target", S_IFREG);
	expect("absolute guest symlink target", sys_chown("/absolute-link", -1, getgid()), 0);
	upper_owned("absolute-link-target", S_IFREG);
	expect("lchown copies link not target", sys_lchown("/nofollow", -1, getgid()), 0);
	check_link("nofollow", "nofollow-target"); absent_upper("nofollow-target");
	expect("fchownat nofollow copies link", sys_fchownat(dfd, "at-nofollow", -1, getgid(), GUEST_AT_SYMLINK_NOFOLLOW), 0);
	check_link("at-nofollow", "at-nofollow-target"); absent_upper("at-nofollow-target");
	expect("dangling lchown succeeds", sys_lchown("/dangling", -1, getgid()), 0);
	check_link("dangling", "missing-target");
	expect("dangling chown fails", sys_chown("/dangling", -1, getgid()), -ENOENT);

	/* Once copied, same-owner lchown must still execute, not return canned success. */
	struct stat link_upper_before = status(prefix_path, "nofollow");
	struct stat at_link_before = status(prefix_path, "at-nofollow");
	struct stat directory_upper_before = status(prefix_path, "directory");
	struct timespec delay = {1, 100000000};
	while (nanosleep(&delay, &delay) < 0) require(errno == EINTR, "wait for distinct ctime");
	expect("upper lchown executes", sys_lchown("/nofollow", -1, getgid()), 0);
	require(!same_time(status(prefix_path, "nofollow").st_ctim, link_upper_before.st_ctim),
		"real lchown changes link ctime");
	expect("upper nofollow fchownat executes",
		sys_fchownat(dfd, "at-nofollow", -1, getgid(), GUEST_AT_SYMLINK_NOFOLLOW), 0);
	require(!same_time(status(prefix_path, "at-nofollow").st_ctim, at_link_before.st_ctim),
		"real nofollow fchownat changes link ctime");
	int directory_fd = open_host(prefix_path, "directory", O_RDONLY | O_DIRECTORY);
	expect("upper directory fchown executes", sys_fchown(directory_fd, -1, getgid()), 0);
	require(!same_time(status(prefix_path, "directory").st_ctim, directory_upper_before.st_ctim),
		"real fchown changes directory ctime");
	require(close(directory_fd) == 0, "fchown preserves upper caller descriptor");

	/* The native control establishes a genuine kernel denial, not assumed credentials. */
	path_join(path, prefix_path, "upper");
	errno = 0; require(chown(path, 0, (gid_t)-1) == -1 && errno == EPERM, "native ownership denial");
	struct stat denied_upper_before = status(prefix_path, "upper");
	expect("denied upper owner change", sys_chown("/upper", 0, -1), -EPERM);
	unchanged(prefix_path, "upper", denied_upper_before);
	expect("denied lower owner change", sys_chown("/denied", 0, -1), -EPERM);
	upper_owned("denied", S_IFREG);
	expect("denied fd owner change", sys_fchown(fd, 0, -1), -EPERM);
	require(fcntl(fd, F_GETFD) >= 0, "denied fchown preserves caller fd");
	expect("invalid descriptor", sys_fchown(-1, -1, getgid()), -EBADF);
	expect("invalid relative dirfd", sys_fchownat(-1, "invalid", -1, getgid(), 0), -EBADF);
	expect("non-directory dirfd", sys_fchownat(fd, "invalid", -1, getgid(), 0), -ENOTDIR);
	expect("invalid flags", sys_fchownat(dfd, "invalid", -1, getgid(), 0x40000000), -EINVAL);
	absent_upper("invalid");
	expect("missing path", sys_chown("/missing", -1, getgid()), -ENOENT);
	expect("empty path", sys_fchownat(dfd, "", -1, getgid(), 0), -ENOENT);
	require(descriptor_count() == descriptors_before, "fchown closes fresh descriptors on success and failure");

	for (size_t i = 0; i < sizeof(files) / sizeof(*files); i++) unchanged(libexec_path, files[i], file_before[i]);
	for (size_t i = 0; i < sizeof(links) / sizeof(*links); i++) unchanged(libexec_path, links[i], link_before[i]);
	unchanged(libexec_path, "directory", directory_before);
	unchanged(libexec_path, ".", lower_root_before);
	require(close(fd) == 0 && close(dfd) == 0, "close caller descriptors");
	puts("EUNION_OWNERSHIP_HOST_OK");
	return 0;
}
