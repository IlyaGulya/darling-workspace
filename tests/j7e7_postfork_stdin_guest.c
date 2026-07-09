#include <errno.h>
#include <fcntl.h>
#include <mach/mach.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <unistd.h>

static int
restore_stdin(int saved_stdin)
{
	if (saved_stdin < 0)
		return 0;
	if (dup2(saved_stdin, STDIN_FILENO) < 0) {
		printf("restore stdin failed: %s\n", strerror(errno));
		close(saved_stdin);
		return 1;
	}
	close(saved_stdin);
	return 0;
}

int
main(void)
{
	for (int i = 0; i < 16; i++) {
		mach_port_t self = mach_task_self();
		if (self == MACH_PORT_NULL) {
			printf("mach_task_self returned MACH_PORT_NULL\n");
			return 1;
		}
	}

	int saved_stdin = fcntl(STDIN_FILENO, F_DUPFD_CLOEXEC, 10);
	if (saved_stdin < 0) {
		printf("save stdin failed: %s\n", strerror(errno));
		return 1;
	}

	int stdin_pipe[2] = {-1, -1};
	if (pipe(stdin_pipe) != 0) {
		printf("pipe failed: %s\n", strerror(errno));
		close(saved_stdin);
		return 1;
	}
	if (dup2(stdin_pipe[0], STDIN_FILENO) < 0) {
		printf("install pipe as stdin failed: %s\n", strerror(errno));
		close(stdin_pipe[0]);
		close(stdin_pipe[1]);
		close(saved_stdin);
		return 1;
	}
	close(stdin_pipe[0]);

	pid_t pid = fork();
	if (pid < 0) {
		printf("fork failed: %s\n", strerror(errno));
		close(stdin_pipe[1]);
		return restore_stdin(saved_stdin) || 1;
	}
	if (pid == 0) {
		close(stdin_pipe[1]);
		execl("/bin/sh", "sh", "-c",
		      "IFS= read -r line && [ \"$line\" = WEST_J7E7_STDIN_OK ]",
		      NULL);
		_exit(127);
	}

	if (restore_stdin(saved_stdin) != 0) {
		close(stdin_pipe[1]);
		waitpid(pid, NULL, 0);
		return 1;
	}

	const char payload[] = "WEST_J7E7_STDIN_OK\n";
	ssize_t written = write(stdin_pipe[1], payload, sizeof(payload) - 1);
	if (written != (ssize_t)(sizeof(payload) - 1)) {
		printf("write stdin payload failed: %s\n", strerror(errno));
		close(stdin_pipe[1]);
		waitpid(pid, NULL, 0);
		return 1;
	}
	close(stdin_pipe[1]);

	int status = 0;
	if (waitpid(pid, &status, 0) != pid) {
		printf("waitpid failed: %s\n", strerror(errno));
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		printf("child stdin probe failed: status=%d\n", status);
		return 1;
	}

	puts("WEST_J7E7_POSTFORK_STDIN_OK");
	return 0;
}
