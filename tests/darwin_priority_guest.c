#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <unistd.h>

#ifndef PRIO_DARWIN_THREAD
#define PRIO_DARWIN_THREAD 3
#endif
#ifndef PRIO_DARWIN_PROCESS
#define PRIO_DARWIN_PROCESS 4
#endif
#ifndef PRIO_DARWIN_BG
#define PRIO_DARWIN_BG 0x1000
#endif
#ifndef PRIO_DARWIN_NONUI
#define PRIO_DARWIN_NONUI 0x1001
#endif

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	exit(2);
}

static void
expect_return(const char *what, int actual, int expected)
{
	if (actual != expected) {
		fprintf(stderr, "%s: return=%d errno=%d, expected return=%d\n",
			what, actual, errno, expected);
		exit(1);
	}
}

int
main(void)
{
	errno = 0;
	if (getpriority(PRIO_DARWIN_THREAD, 0) != 0) {
		fail_errno("getpriority PRIO_DARWIN_THREAD");
	}

	errno = 0;
	expect_return("getpriority PRIO_DARWIN_THREAD nonzero who",
		getpriority(PRIO_DARWIN_THREAD, 7), -EINVAL);

	errno = 0;
	if (getpriority(PRIO_DARWIN_PROCESS, getpid()) != 0) {
		fail_errno("getpriority PRIO_DARWIN_PROCESS");
	}

	if (setpriority(PRIO_DARWIN_THREAD, 0, 0) != 0) {
		fail_errno("setpriority PRIO_DARWIN_THREAD 0");
	}
	if (setpriority(PRIO_DARWIN_THREAD, 0, PRIO_DARWIN_BG) != 0) {
		fail_errno("setpriority PRIO_DARWIN_THREAD BG");
	}
	if (setpriority(PRIO_DARWIN_THREAD, 0, PRIO_DARWIN_NONUI) != 0) {
		fail_errno("setpriority PRIO_DARWIN_THREAD NONUI");
	}

	errno = 0;
	expect_return("setpriority PRIO_DARWIN_THREAD nonzero who",
		setpriority(PRIO_DARWIN_THREAD, 1, 0), -EINVAL);

	errno = 0;
	expect_return("setpriority PRIO_DARWIN_THREAD invalid prio",
		setpriority(PRIO_DARWIN_THREAD, 0, 123), -EINVAL);

	if (setpriority(PRIO_DARWIN_PROCESS, getpid(), PRIO_DARWIN_BG) != 0) {
		fail_errno("setpriority PRIO_DARWIN_PROCESS BG");
	}

	errno = 0;
	expect_return("setpriority PRIO_DARWIN_PROCESS invalid prio",
		setpriority(PRIO_DARWIN_PROCESS, getpid(), 123), -EINVAL);

	puts("DARWIN_PRIORITY_GUEST_OK");
	return 0;
}
