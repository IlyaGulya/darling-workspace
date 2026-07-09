#include <errno.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static const char *fallback_fault_path = "/private/var/tmp/dserver-fork-checkin-bound-fault";

static void
timeout_handler(int signo)
{
	(void)signo;
	_exit(124);
}

static FILE *
open_fault_file(void)
{
	const char *env_path = getenv("DSERVER_TEST_FAULT_FILE");
	if (env_path && env_path[0] == '/') {
		FILE *file = fopen(env_path, "w");
		if (file) {
			return file;
		}

		char system_root_path[4096];
		snprintf(system_root_path, sizeof(system_root_path),
		    "/Volumes/SystemRoot%s", env_path);
		file = fopen(system_root_path, "w");
		if (file) {
			return file;
		}
	}

	return fopen(fallback_fault_path, "w");
}

static void
write_fault(void)
{
	FILE *file = open_fault_file();
	if (!file) {
		perror("fopen fault");
		exit(20);
	}
	fputs("fork.drop_checkin\n", file);
	fclose(file);
}

int
main(void)
{
	signal(SIGALRM, timeout_handler);
	alarm(15);
	write_fault();

	errno = 0;
	pid_t pid = fork();
	if (pid == 0) {
		_exit(0);
	}
	if (pid < 0) {
		perror("fork");
		return 2;
	}

	alarm(0);
	puts("FORK_CHECKIN_DROP_TIMEOUT_OK");
	return 0;
}
