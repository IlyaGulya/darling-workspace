#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

int
main(void)
{
	const char* path = "/private/var/tmp/west-eunion-lower-smoke.txt";
	const char* expected = "WEST_EUNION_LOWER_OK\n";
	char buf[64] = {0};

	int fd = open(path, O_RDONLY);
	if (fd < 0) {
		printf("open failed: %s\n", strerror(errno));
		return 1;
	}

	ssize_t n = read(fd, buf, sizeof(buf) - 1);
	close(fd);
	if (n < 0) {
		printf("read failed: %s\n", strerror(errno));
		return 1;
	}
	if (strcmp(buf, expected) != 0) {
		printf("content mismatch: %s\n", buf);
		return 1;
	}

	puts("WEST_EUNION_PREFIX_SMOKE_OK");
	return 0;
}
