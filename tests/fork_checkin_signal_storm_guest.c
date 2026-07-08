#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <unistd.h>

static volatile sig_atomic_t signals_seen;

static void on_alarm(int signo) {
	(void)signo;
	++signals_seen;
}

static void fail_errno(const char* what) {
	perror(what);
	exit(1);
}

int main(void) {
	struct sigaction sa;
	memset(&sa, 0, sizeof(sa));
	sa.sa_handler = on_alarm;
	sigemptyset(&sa.sa_mask);
	if (sigaction(SIGALRM, &sa, NULL) != 0) {
		fail_errno("sigaction");
	}

	struct itimerval timer;
	memset(&timer, 0, sizeof(timer));
	timer.it_interval.tv_usec = 1000;
	timer.it_value.tv_usec = 1000;
	if (setitimer(ITIMER_REAL, &timer, NULL) != 0) {
		fail_errno("setitimer");
	}

	for (int i = 0; i < 256; ++i) {
		pid_t pid = fork();
		if (pid < 0) {
			fail_errno("fork");
		}
		if (pid == 0) {
			_exit(0);
		}

		int status = 0;
		while (waitpid(pid, &status, 0) < 0) {
			if (errno == EINTR) {
				continue;
			}
			fail_errno("waitpid");
		}
		if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
			fprintf(stderr, "child exited unexpectedly: status=%d\n", status);
			return 1;
		}
	}

	timer.it_interval.tv_usec = 0;
	timer.it_value.tv_usec = 0;
	(void)setitimer(ITIMER_REAL, &timer, NULL);

	if (signals_seen == 0) {
		fprintf(stderr, "signal storm did not run\n");
		return 1;
	}

	puts("FORK_CHECKIN_SIGNAL_STORM_OK");
	return 0;
}
