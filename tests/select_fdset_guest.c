#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <time.h>
#include <unistd.h>

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	exit(2);
}

static void
make_high_fd_pipe(int pipefd[2])
{
	int keep[64];
	size_t keep_count = 0;

	while (keep_count < sizeof(keep) / sizeof(keep[0])) {
		if (pipe(pipefd) != 0) {
			fail_errno("pipe");
		}
		if (pipefd[0] >= 40 && pipefd[1] >= 40) {
			for (size_t i = 0; i < keep_count; i++) {
				close(keep[i]);
			}
			return;
		}
		keep[keep_count++] = pipefd[0];
		keep[keep_count++] = pipefd[1];
	}

	fprintf(stderr, "could not allocate fd above 40\n");
	exit(2);
}

static void
write_one(int fd)
{
	char byte = 'x';
	if (write(fd, &byte, 1) != 1) {
		fail_errno("write");
	}
}

static void
drain_one(int fd)
{
	char byte;
	if (read(fd, &byte, 1) != 1) {
		fail_errno("read");
	}
}

static void
check_select_high_fd(int read_fd)
{
	fd_set readfds;
	struct timeval tv = { .tv_sec = 0, .tv_usec = 500000 };

	FD_ZERO(&readfds);
	FD_SET(read_fd, &readfds);

	int rc = select(read_fd + 1, &readfds, NULL, NULL, &tv);
	if (rc != 1 || !FD_ISSET(read_fd, &readfds)) {
		fprintf(stderr, "select high fd failed rc=%d isset=%d errno=%d\n",
			rc, FD_ISSET(read_fd, &readfds), errno);
		exit(1);
	}
}

static void
check_pselect_high_fd(int read_fd)
{
	fd_set readfds;
	struct timespec ts = { .tv_sec = 0, .tv_nsec = 500000000 };

	FD_ZERO(&readfds);
	FD_SET(read_fd, &readfds);

	int rc = pselect(read_fd + 1, &readfds, NULL, NULL, &ts, NULL);
	if (rc != 1 || !FD_ISSET(read_fd, &readfds)) {
		fprintf(stderr, "pselect high fd failed rc=%d isset=%d errno=%d\n",
			rc, FD_ISSET(read_fd, &readfds), errno);
		exit(1);
	}
}

int
main(void)
{
	int pipefd[2];
	make_high_fd_pipe(pipefd);

	write_one(pipefd[1]);
	check_select_high_fd(pipefd[0]);
	drain_one(pipefd[0]);

	write_one(pipefd[1]);
	check_pselect_high_fd(pipefd[0]);
	drain_one(pipefd[0]);

	close(pipefd[0]);
	close(pipefd[1]);

	puts("SELECT_FDSET_GUEST_OK");
	return 0;
}
