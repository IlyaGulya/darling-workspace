#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t parent_saw_segv;

static void
parent_segv_handler(int signo)
{
	(void)signo;
	parent_saw_segv = 1;
}

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	exit(2);
}

static void
crash_with_default_sigsegv(void)
{
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = SIG_DFL;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGSEGV, &sa, NULL) != 0) {
		_exit(3);
	}

	volatile int *null_int = (volatile int *)0;
	*null_int = 1;
	_exit(4);
}

int
main(void)
{
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = parent_segv_handler;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGSEGV, &sa, NULL) != 0) {
		fail_errno("sigaction");
	}

	pid_t child = fork();
	if (child < 0) {
		fail_errno("fork");
	}
	if (child == 0) {
		crash_with_default_sigsegv();
	}

	int status = 0;
	for (;;) {
		pid_t waited = waitpid(child, &status, 0);
		if (waited == child) {
			break;
		}
		if (waited < 0 && errno == EINTR) {
			continue;
		}
		fail_errno("waitpid");
	}

	if (!WIFSIGNALED(status) || WTERMSIG(status) != SIGSEGV) {
		fprintf(stderr, "child status=0x%x, expected SIGSEGV\n", status);
		return 1;
	}
	if (parent_saw_segv) {
		fprintf(stderr, "parent received child's default SIGSEGV broadcast\n");
		return 1;
	}

	puts("SIGEXC_DEFAULT_RESEND_SELF_GUEST_OK");
	return 0;
}
