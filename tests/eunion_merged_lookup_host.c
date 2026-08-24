#define TEST 1
#define EUNION 1
#define _GNU_SOURCE 1
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/xattr.h>
#include <unistd.h>

#define LINUX_EEXIST EEXIST
#define LINUX_EPERM EPERM
#define LINUX_ENOTDIR ENOTDIR
#define LINUX_EINVAL EINVAL
#define LINUX_EMFILE EMFILE
#define LINUX_EIO EIO
#define LINUX_AT_FDCWD AT_FDCWD

int dserver_rpc_vchroot_path(char* path, unsigned long size, uint64_t* token)
{
	(void)path; (void)size; (void)token;
	return -1;
}
void __simple_abort(void) { __builtin_trap(); }
int __simple_printf(const char* format, ...) { (void)format; return 0; }

#define main vchroot_unit_main
#include "vchroot_userspace.c"
#undef main

static void require(int value, const char* message)
{
	if (!value) {
		fprintf(stderr, "FAIL %s\n", message);
		exit(1);
	}
}

static void make_dir(const char* path)
{
	require(mkdir(path, 0700) == 0, path);
}

static void make_file(const char* path, const char* contents)
{
	int fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600);
	require(fd >= 0, path);
	require(write(fd, contents, strlen(contents)) == (ssize_t)strlen(contents), "write file");
	require(close(fd) == 0, "close file");
}

static int merged_contains(const char* names, int count, const char* expected)
{
	int offset = 0;
	for (int index = 0; index < count; ++index) {
		const char* name = names + offset;
		size_t size = strlen(name);
		if (strcmp(name, expected) == 0)
			return 1;
		offset += (int)size + 1;
	}
	return 0;
}

static void expect_layer(const char* guest, const char* expected)
{
	struct vchroot_expand_args args = { .dfd = AT_FDCWD, .flags = VCHROOT_FOLLOW };
	require(strlen(guest) < sizeof(args.path), "guest path length");
	strcpy(args.path, guest);
	require(vchroot_expand(&args) == 0, "vchroot expand");
	if (strcmp(args.path, expected) != 0) {
		fprintf(stderr, "resolved=%s expected=%s\n", args.path, expected);
		require(0, "unexpected resolved layer");
	}
}

int main(int argc, char** argv)
{
	require(argc == 2, "usage: fixture ROOT");
	char upper[4096], lower[4096], path[4096], expected[4096];
	require(snprintf(upper, sizeof(upper), "%s/u", argv[1]) > 0, "upper path");
	require(snprintf(lower, sizeof(lower), "%s/lower-root-with-different-length", argv[1]) > 0, "lower path");
	make_dir(upper); make_dir(lower);
	for (const char* suffix = "/private"; suffix; suffix = strcmp(suffix, "/private") == 0 ? "/private/var" : strcmp(suffix, "/private/var") == 0 ? "/private/var/db" : NULL) {
		require(snprintf(path, sizeof(path), "%s%s", upper, suffix) > 0, "upper component");
		make_dir(path);
		require(snprintf(path, sizeof(path), "%s%s", lower, suffix) > 0, "lower component");
		make_dir(path);
	}
	require(snprintf(path, sizeof(path), "%s/private/var/db/lower-only", lower) > 0, "lower leaf");
	make_dir(path);
	require(snprintf(path, sizeof(path), "%s/private/var/db/upper-created", upper) > 0, "upper leaf");
	make_dir(path);
	require(snprintf(path, sizeof(path), "%s/private/tmp", upper) > 0, "upper symlink target");
	make_dir(path);
	require(snprintf(path, sizeof(path), "%s/private/tmp", lower) > 0, "lower symlink target");
	make_dir(path);
	require(snprintf(path, sizeof(path), "%s/tmp", lower) > 0, "lower symlink");
	require(symlink("private/tmp", path) == 0, "create lower relative symlink");
	char upper_file[4096], lower_file[4096];
	require(snprintf(lower_file, sizeof(lower_file), "%s/private/var/db/lower-entry", lower) > 0, "lower entry");
	make_file(lower_file, "lower");
	require(snprintf(upper_file, sizeof(upper_file), "%s/private/var/db/upper-entry", upper) > 0, "upper entry");
	make_file(upper_file, "upper");
	require(snprintf(lower_file, sizeof(lower_file), "%s/private/var/db/shadow-entry", lower) > 0, "lower shadow");
	make_file(lower_file, "lower-shadow");
	require(snprintf(upper_file, sizeof(upper_file), "%s/private/var/db/shadow-entry", upper) > 0, "upper shadow");
	make_file(upper_file, "upper-shadow");
	require(snprintf(lower_file, sizeof(lower_file), "%s/private/var/db/hidden-entry", lower) > 0, "lower hidden");
	make_file(lower_file, "hidden");
	require(snprintf(upper_file, sizeof(upper_file), "%s/private/var/db/hidden-entry", upper) > 0, "upper whiteout");
	make_file(upper_file, "");
	require(setxattr(upper_file, "user.union.whiteout", "y", 1, 0) == 0, "mark whiteout");
	strcpy(prefix_path, upper); prefix_path_len = (int)strlen(upper);
	strcpy(libexec_path, lower); libexec_path_len = (int)strlen(lower);
	require(snprintf(expected, sizeof(expected), "%s/private/var/db/lower-only", lower) > 0, "expected lower");
	expect_layer("/private/var/db/lower-only", expected);
	require(snprintf(expected, sizeof(expected), "%s/private/var/db/upper-created", upper) > 0, "expected upper");
	expect_layer("/private/var/db/upper-created", expected);
	require(snprintf(expected, sizeof(expected), "%s/private/tmp", upper) > 0, "expected rebased target");
	expect_layer("/tmp", expected);
	char merged[65536] = {};
	int merged_length = vchroot_readdir_merge("/private/var/db", merged, sizeof(merged));
	require(merged_length > 0, "merged readdir");
	require(merged_contains(merged, merged_length, "lower-entry"), "lower-only enumeration");
	require(merged_contains(merged, merged_length, "upper-entry"), "upper enumeration");
	require(merged_contains(merged, merged_length, "shadow-entry"), "upper shadow enumeration");
	require(!merged_contains(merged, merged_length, "hidden-entry"), "whiteout hides lower entry");
	puts("EUNION_MERGED_LOOKUP_OK");
	return 0;
}
