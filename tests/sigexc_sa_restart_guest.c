#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t saw_signal;

static void
handler(int signo)
{
	(void)signo;
	saw_signal = 1;
}

static void
fail_errno(const char *what)
{
	fprintf(stderr, "%s: %s (%d)\n", what, strerror(errno), errno);
	_exit(2);
}

static void
child_signal_then_write(pid_t parent, int write_fd, int write_after_signal)
{
	usleep(100000);
	if (kill(parent, SIGUSR1) != 0) {
		fail_errno("kill");
	}
	if (!write_after_signal) {
		_exit(0);
	}
	usleep(100000);
	if (write(write_fd, "x", 1) != 1) {
		fail_errno("write");
	}
	_exit(0);
}

static int
run_read_case(int flags, int want_restart)
{
	int pipefd[2];
	if (pipe(pipefd) != 0) {
		fail_errno("pipe");
	}

	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = handler;
	sa.sa_flags = flags;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGUSR1, &sa, NULL) != 0) {
		fail_errno("sigaction");
	}
	saw_signal = 0;

	pid_t child = fork();
	if (child < 0) {
		fail_errno("fork");
	}
	if (child == 0) {
		close(pipefd[0]);
		child_signal_then_write(getppid(), pipefd[1], want_restart);
	}
	close(pipefd[1]);

	char byte = 0;
	errno = 0;
	ssize_t n = read(pipefd[0], &byte, 1);
	int saved_errno = errno;
	close(pipefd[0]);

	int status = 0;
	if (waitpid(child, &status, 0) != child) {
		fail_errno("waitpid");
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "child status=0x%x\n", status);
		return 1;
	}
	if (!saw_signal) {
		fprintf(stderr, "signal handler did not run\n");
		return 1;
	}

	if (want_restart) {
		if (n != 1 || byte != 'x') {
			fprintf(stderr, "SA_RESTART read n=%zd byte=%d errno=%d\n",
				n, (int)byte, saved_errno);
			return 1;
		}
	} else {
		if (n != -1 || saved_errno != EINTR) {
			fprintf(stderr, "non-restart read n=%zd errno=%d, expected EINTR\n",
				n, saved_errno);
			return 1;
		}
	}
	return 0;
}

int
main(void)
{
	if (run_read_case(0, 0) != 0) {
		return 1;
	}
	if (run_read_case(SA_RESTART, 1) != 0) {
		return 1;
	}
	puts("SIGEXC_SA_RESTART_GUEST_OK");
	return 0;
}
