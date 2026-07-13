#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static int
bind_socket(const char* bind_path)
{
	struct sockaddr_un address;
	int fd;

	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0) {
		printf("socket failed: %s\n", strerror(errno));
		return 1;
	}
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	if (strlen(bind_path) >= sizeof(address.sun_path)) {
		printf("socket path too long: %s\n", bind_path);
		close(fd);
		return 1;
	}
	strcpy(address.sun_path, bind_path);
	if (bind(fd, (struct sockaddr*)&address, sizeof(address)) != 0) {
		printf("bind %s failed: %s\n", bind_path, strerror(errno));
		close(fd);
		return 1;
	}
	/* Leave the node in place so the fixture can prove it was not written to lower. */
	close(fd);
	return 0;
}

static int
expect_socket(const char* path)
{
	struct stat st;

	if (lstat(path, &st) != 0) {
		printf("lstat %s failed: %s\n", path, strerror(errno));
		return 1;
	}
	if (!S_ISSOCK(st.st_mode)) {
		printf("bound path is not a socket: %s\n", path);
		return 1;
	}
	return 0;
}

int
main(void)
{
	const char* direct_path =
	    "/private/var/tmp/west-b/d/s";
	const char* nested_path =
	    "/private/var/tmp/west-b/l/s";

	if (bind_socket(direct_path) != 0)
		return 1;
	if (expect_socket(direct_path) != 0)
		return 1;
	if (bind_socket(nested_path) != 0)
		return 1;
	if (expect_socket(nested_path) != 0)
		return 1;
	puts("WEST_EUNION_NESTED_BIND_OK");
	return 0;
}
