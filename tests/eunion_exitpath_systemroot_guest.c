#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>

static int
expect_dir(const char* path)
{
	struct stat st;
	if (stat(path, &st) != 0) {
		printf("stat %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (!S_ISDIR(st.st_mode)) {
		printf("stat %s mode is not directory: %o\n", path, st.st_mode);
		return 1;
	}
	return 0;
}

int
main(void)
{
	if (expect_dir("/Volumes/SystemRoot") != 0)
		return 1;
	if (expect_dir("/volumes/systemroot") != 0)
		return 1;

	puts("WEST_EUNION_EXITPATH_SYSTEMROOT_OK");
	return 0;
}
