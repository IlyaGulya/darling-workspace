#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static void
timeout_handler(int signo)
{
	(void)signo;
	_exit(124);
}

int
main(int argc, char **argv)
{
	if (argc == 2 && strcmp(argv[1], "exec-child") == 0) {
		return 0;
	}

	signal(SIGALRM, timeout_handler);
	alarm(20);

	for (int round = 0; round < 3; ++round) {
		pid_t pid = fork();
		if (pid < 0) {
			perror("fork");
			return 2;
		}

		if (pid == 0) {
			execl(argv[0], argv[0], "exec-child", (char *)NULL);
			perror("execl self");
			_exit(127);
		}

		int status = 0;
		while (waitpid(pid, &status, 0) < 0) {
			if (errno == EINTR) {
				continue;
			}
			perror("waitpid");
			return 3;
		}
		if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
			fprintf(stderr, "exec child failed: status=%d\n", status);
			return 4;
		}
	}

	alarm(0);
	puts("CHECKIN_EXEC_TRACE_GUEST_OK");
	return 0;
}
