#include <errno.h>
#include <stddef.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

static int fail(const char* message)
{
	fprintf(stderr, "FAIL %s errno=%d (%s)\n", message, errno, strerror(errno));
	return 1;
}

static socklen_t address_length(struct sockaddr_un* address)
{
	size_t length = offsetof(struct sockaddr_un, sun_path) +
		strlen(address->sun_path) + 1;
	address->sun_len = (unsigned char)length;
	return (socklen_t)length;
}

int main(void)
{
	const char* directory = "/private/var/tmp/west-eunion-afunix";
	const char* short_path = "/private/var/tmp/west-eunion-afunix/short.sock";
	if (mkdir(directory, 0700) != 0 && errno != EEXIST)
		return fail("create short-path directory");
	unlink(short_path);

	struct sockaddr_un address;
	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	strcpy(address.sun_path, short_path);
	int fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
		return fail("create short-path socket");
	if (bind(fd, (const struct sockaddr*)&address, address_length(&address)) != 0)
		return fail("bind short path");
	struct stat status;
	if (lstat(short_path, &status) != 0 || !S_ISSOCK(status.st_mode))
		return fail("short socket is not guest-visible");
	close(fd);
	if (unlink(short_path) != 0)
		return fail("remove short socket");

	memset(&address, 0, sizeof(address));
	address.sun_family = AF_UNIX;
	const char* parent = "/private/var/tmp/";
	size_t parent_length = strlen(parent);
	memcpy(address.sun_path, parent, parent_length);
	memset(address.sun_path + parent_length, 'l',
		sizeof(address.sun_path) - 1 - parent_length);
	address.sun_path[sizeof(address.sun_path) - 1] = '\0';
	fd = socket(AF_UNIX, SOCK_STREAM, 0);
	if (fd < 0)
		return fail("create overlong-path socket");
	errno = 0;
	int result = bind(fd, (const struct sockaddr*)&address, address_length(&address));
	int bind_errno = errno;
	close(fd);
	if (result != -1 || bind_errno != ENAMETOOLONG) {
		errno = bind_errno;
		return fail("overlong expanded path did not return ENAMETOOLONG");
	}
	errno = 0;
	if (lstat(address.sun_path, &status) == 0 || errno != ENOENT)
		return fail("overlong bind created a guest-visible socket");

	if (rmdir(directory) != 0)
		return fail("remove short-path directory");
	printf("WEST_EUNION_AF_UNIX_PATH_LENGTH_OK\n");
	return 0;
}
