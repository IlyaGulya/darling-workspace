#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int
read_exact(const char* path, const char* expected)
{
	char buf[64] = {0};

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

int
main(void)
{
	if (read_exact("/private/var/tmp/west-eunion-smoke/lower.txt", "LOWER\n") != 0)
		return 1;
	if (read_exact("/private/var/tmp/west-eunion-smoke/upper.txt", "UPPER\n") != 0)
		return 1;
	if (read_exact("/private/var/tmp/west-eunion-smoke/shadow.txt", "UPPER_SHADOW\n") != 0)
		return 1;

	puts("WEST_EUNION_PREFIX_SMOKE_OK");
	return 0;
}
