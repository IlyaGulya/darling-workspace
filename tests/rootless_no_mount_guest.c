#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

int main(void) {
	if (getuid() == 0) {
		fputs("rootless guest unexpectedly reports uid 0\n", stderr);
		return 1;
	}

	pid_t child = fork();
	if (child < 0) {
		perror("fork");
		return 1;
	}
	if (child == 0) {
		_exit(getpid() > 0 ? 0 : 1);
	}

	int status = 0;
	if (waitpid(child, &status, 0) != child || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fputs("rootless guest child did not exit cleanly\n", stderr);
		return 1;
	}

	puts("ROOTLESS_NO_MOUNT_GUEST_OK");
	return 0;
}
