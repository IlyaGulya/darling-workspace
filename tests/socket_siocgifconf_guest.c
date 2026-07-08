#include <errno.h>
#include <net/if.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	exit(2);
}

int
main(void)
{
	int fd = socket(AF_INET, SOCK_DGRAM, 0);
	if (fd < 0) {
		fail_errno("socket");
	}

	char buffer[sizeof(struct ifreq) * 4];
	struct ifconf ifc;
	memset(&ifc, 0, sizeof(ifc));
	ifc.ifc_len = sizeof(buffer);
	ifc.ifc_buf = buffer;

	if (ioctl(fd, SIOCGIFCONF, &ifc) != 0) {
		fail_errno("ioctl SIOCGIFCONF");
	}
	if (ifc.ifc_len != 0) {
		fprintf(stderr, "SIOCGIFCONF ifc_len=%d, expected 0\n", ifc.ifc_len);
		return 1;
	}

	errno = 0;
	if (ioctl(fd, SIOCGIFCONF, NULL) != -1 || errno != EFAULT) {
		fprintf(stderr, "SIOCGIFCONF NULL errno=%d, expected EFAULT\n", errno);
		return 1;
	}

	close(fd);
	puts("SOCKET_SIOCGIFCONF_GUEST_OK");
	return 0;
}
