#include <errno.h>
#include <signal.h>
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
	int keep[160];
	size_t keep_count = 0;

	while (keep_count < sizeof(keep) / sizeof(keep[0])) {
		if (pipe(pipefd) != 0) {
			fail_errno("pipe");
		}
		if (pipefd[0] >= 70 && pipefd[1] >= 70) {
			for (size_t i = 0; i < keep_count; i++) {
				close(keep[i]);
			}
			return;
		}
		keep[keep_count++] = pipefd[0];
		keep[keep_count++] = pipefd[1];
	}

	fprintf(stderr, "could not allocate fd above 70\n");
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
	sigset_t mask;

	FD_ZERO(&readfds);
	FD_SET(read_fd, &readfds);
	sigemptyset(&mask);
	sigaddset(&mask, SIGUSR1);

	int rc = pselect(read_fd + 1, &readfds, NULL, NULL, &ts, &mask);
	if (rc != 1 || !FD_ISSET(read_fd, &readfds)) {
		fprintf(stderr, "pselect high fd failed rc=%d isset=%d errno=%d\n",
			rc, FD_ISSET(read_fd, &readfds), errno);
		exit(1);
	}
}

static void
check_fdset_boundaries(void)
{
	struct timeval tv = { .tv_sec = 0, .tv_usec = 1000 };
	struct timespec ts = { .tv_sec = 0, .tv_nsec = 1000000 };
	struct timeval original_tv = tv;
	struct timespec original_ts = ts;

	errno = 0;
	if (select(-1, NULL, NULL, NULL, &tv) != -1 || errno != EINVAL) {
		fprintf(stderr, "select negative nfds did not return EINVAL: errno=%d\n", errno);
		exit(1);
	}

	errno = 0;
	if (pselect(FD_SETSIZE + 1, NULL, NULL, NULL, &ts, NULL) != -1 || errno != EINVAL) {
		fprintf(stderr, "pselect oversized nfds did not return EINVAL: errno=%d\n", errno);
		exit(1);
	}

	tv = original_tv;
	errno = 0;
	if (select(0, NULL, NULL, NULL, &tv) != 0) {
		fail_errno("select zero nfds");
	}
	if (tv.tv_sec != original_tv.tv_sec || tv.tv_usec != original_tv.tv_usec) {
		fprintf(stderr, "select mutated timeout: %ld.%06d\n",
			(long)tv.tv_sec, tv.tv_usec);
		exit(1);
	}

	ts = original_ts;
	errno = 0;
	if (pselect(0, NULL, NULL, NULL, &ts, NULL) != 0) {
		fail_errno("pselect zero nfds");
	}
	if (ts.tv_sec != original_ts.tv_sec || ts.tv_nsec != original_ts.tv_nsec) {
		fprintf(stderr, "pselect mutated timeout: %ld.%09ld\n",
			(long)ts.tv_sec, ts.tv_nsec);
		exit(1);
	}
}

int
main(void)
{
	int pipefd[2];
	check_fdset_boundaries();
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
