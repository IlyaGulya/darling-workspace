#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <sys/uio.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

struct dserver_replyhdr_for_size {
	unsigned int number;
	int code;
};

struct dserver_push_reply_call {
	struct {
		unsigned int number;
		pid_t pid;
		pid_t tid;
		unsigned int architecture;
	} header;
	uint64_t reply;
	uint64_t reply_size;
};

extern int mach_driver_get_fd(void);

int main(void)
{
	struct dserver_push_reply_call call = {
	    .header = {
	        .number = 0xbadca11,
	        .pid = getpid(),
	        .tid = getpid(),
	        .architecture = 1,
	    },
	    .reply = 1,
	    .reply_size = sizeof(struct dserver_replyhdr_for_size),
	};
	struct iovec iov = {
	    .iov_base = &call,
	    .iov_len = sizeof(call),
	};
	struct sockaddr_un address = {
	    .sun_family = AF_UNIX,
	};
	int pipe_ends[2];
	char prefix[4096];
	const char *marker = getenv("DSERVER_TEST_PREFIX_MARKER");
	const char *suffix = "/private/var/tmp/dserver-pushreply-prefix-marker";
	if (!marker || marker[0] == '\0') {
		fprintf(stderr, "DSERVER_TEST_PREFIX_MARKER is not set\n");
		return 2;
	}
	size_t marker_len = strlen(marker);
	size_t suffix_len = strlen(suffix);
	if (marker_len <= suffix_len ||
	    strcmp(marker + marker_len - suffix_len, suffix) != 0) {
		fprintf(stderr, "unexpected prefix marker path: %s\n", marker);
		return 2;
	}
	size_t prefix_len = marker_len - suffix_len;
	if (prefix_len >= sizeof(prefix)) {
		fprintf(stderr, "prefix path is too long\n");
		return 2;
	}
	memcpy(prefix, marker, prefix_len);
	prefix[prefix_len] = '\0';
	snprintf(address.sun_path, sizeof(address.sun_path),
	    "%s/.darlingserver.sock", prefix);
	address.sun_len = sizeof(address);

	int socket = mach_driver_get_fd();
	if (socket < 0) {
		fprintf(stderr, "mach_driver_get_fd failed: %d\n", socket);
		return 3;
	}

	if (pipe(pipe_ends) != 0) {
		perror("pipe");
		return 4;
	}

	char control[CMSG_SPACE(sizeof(int))] = {};
	struct cmsghdr *cmsg = (struct cmsghdr *)control;
	cmsg->cmsg_len = CMSG_LEN(sizeof(int));
	cmsg->cmsg_level = SOL_SOCKET;
	cmsg->cmsg_type = SCM_RIGHTS;
	*(int *)CMSG_DATA(cmsg) = pipe_ends[1];

	struct msghdr message = {
	    .msg_name = &address,
	    .msg_namelen = sizeof(address),
	    .msg_iov = &iov,
	    .msg_iovlen = 1,
	    .msg_control = control,
	    .msg_controllen = sizeof(control),
	};

	ssize_t sent = sendmsg(socket, &message, 0);
	close(pipe_ends[1]);
	if (sent != (ssize_t)sizeof(call)) {
		perror("sendmsg push_reply");
		return 5;
	}

	char byte = 0;
	(void)read(pipe_ends[0], &byte, sizeof(byte));
	close(pipe_ends[0]);

	uid_t uid = getuid();
	if (setuid(uid) != 0) {
		perror("setuid after push_reply EOF");
		return 6;
	}

	puts("PUSHREPLY_BAD_POINTER_OK");
	return 0;
}
