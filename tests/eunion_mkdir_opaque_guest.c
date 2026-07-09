#include <dirent.h>
#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int
expect_absent(const char* path)
{
	if (access(path, F_OK) == 0) {
		printf("expected absent but exists: %s\n", path);
		return 1;
	}
	if (errno != ENOENT) {
		printf("access %s failed with unexpected errno: %s\n", path, strerror(errno));
		return 1;
	}
	return 0;
}

static int
expect_no_dir_entry(const char* dir, const char* name)
{
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
		if (strcmp(ent->d_name, name) == 0) {
			printf("unexpected entry %s in %s\n", name, dir);
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
	return 0;
}

int
main(void)
{
	const char* dir = "/private/var/tmp/west-eunion-mkdir-opaque/recreate";
	const char* stale_a = "/private/var/tmp/west-eunion-mkdir-opaque/recreate/stale_a.txt";
	const char* stale_b = "/private/var/tmp/west-eunion-mkdir-opaque/recreate/stale_b.txt";

	if (access(stale_a, F_OK) != 0 || access(stale_b, F_OK) != 0) {
		printf("fixture stale children missing before rmdir: %s\n", strerror(errno));
		return 1;
	}

	if (rmdir(dir) != 0) {
		printf("rmdir %s failed: %s\n", dir, strerror(errno));
		return 1;
	}
	if (expect_absent(dir) != 0)
		return 1;

	if (mkdir(dir, 0755) != 0) {
		printf("mkdir %s failed: %s\n", dir, strerror(errno));
		return 1;
	}
	if (expect_absent(stale_a) != 0 || expect_absent(stale_b) != 0)
		return 1;
	if (expect_no_dir_entry(dir, "stale_a.txt") != 0)
		return 1;
	if (expect_no_dir_entry(dir, "stale_b.txt") != 0)
		return 1;

	puts("WEST_EUNION_MKDIR_OPAQUE_OK");
	return 0;
}
