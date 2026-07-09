#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#ifndef F_GETPATH
#define F_GETPATH 50
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
		printf("content mismatch for %s: %s\n", path, buf);
		return 1;
	}
	return 0;
}

static int
expect_regular(const char* path)
{
	struct stat st;
	if (stat(path, &st) != 0) {
		printf("stat %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (!S_ISREG(st.st_mode)) {
		printf("stat %s mode is not regular: %o\n", path, st.st_mode);
		return 1;
	}
	return 0;
}

static int
expect_fgetpath(const char* path)
{
	char got[1024] = {0};
	int fd = open(path, O_RDONLY);
	if (fd < 0) {
		printf("open for F_GETPATH %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	int rc = fcntl(fd, F_GETPATH, got);
	close(fd);
	if (rc != 0) {
		printf("F_GETPATH %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (strcmp(got, path) != 0) {
		printf("F_GETPATH mismatch: got %s want %s\n", got, path);
		return 1;
	}
	return 0;
}

static int
expect_dir_merge(const char* dir)
{
	int lower = 0;
	int upper = 0;
	int shadow = 0;
	DIR* d = opendir(dir);
	if (!d) {
		printf("opendir %s failed: %s\n", dir, strerror(errno));
		return 1;
	}
	for (;;) {
		errno = 0;
		struct dirent* ent = readdir(d);
		if (!ent)
			break;
		if (strcmp(ent->d_name, "lower-only.txt") == 0) {
			lower++;
			if (ent->d_type != DT_REG) {
				printf("lower-only d_type is %u\n", ent->d_type);
				closedir(d);
				return 1;
			}
		} else if (strcmp(ent->d_name, "upper-only.txt") == 0) {
			upper++;
			if (ent->d_type != DT_REG) {
				printf("upper-only d_type is %u\n", ent->d_type);
				closedir(d);
				return 1;
			}
		} else if (strcmp(ent->d_name, "shadow.txt") == 0) {
			shadow++;
			if (ent->d_type != DT_REG) {
				printf("shadow d_type is %u\n", ent->d_type);
				closedir(d);
				return 1;
			}
		} else if (strncmp(ent->d_name, ".union", 6) == 0) {
			printf("union private marker leaked into readdir: %s\n", ent->d_name);
			closedir(d);
			return 1;
		}
	}
	if (errno != 0) {
		printf("readdir %s failed: %s\n", dir, strerror(errno));
		closedir(d);
		return 1;
	}
	closedir(d);

	if (lower != 1 || upper != 1 || shadow != 1) {
		printf("dir merge counts lower=%d upper=%d shadow=%d\n", lower, upper, shadow);
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* dir = "/private/var/tmp/west-eunion-direnum";
	const char* lower = "/private/var/tmp/west-eunion-direnum/lower-only.txt";
	const char* upper = "/private/var/tmp/west-eunion-direnum/upper-only.txt";
	const char* shadow = "/private/var/tmp/west-eunion-direnum/shadow.txt";

	if (read_exact(lower, "LOWER_ONLY\n") != 0)
		return 1;
	if (read_exact(upper, "UPPER_ONLY\n") != 0)
		return 1;
	if (read_exact(shadow, "UPPER_SHADOW\n") != 0)
		return 1;
	if (expect_regular(lower) != 0)
		return 1;
	if (expect_fgetpath(lower) != 0)
		return 1;
	if (expect_dir_merge(dir) != 0)
		return 1;

	puts("WEST_EUNION_PATH_DIRENUM_OK");
	return 0;
}
