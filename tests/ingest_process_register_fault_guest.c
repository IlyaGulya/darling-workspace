#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static const char *fault_path = "/private/var/tmp/dserver-test-fault";

static FILE *open_fault_file(void)
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

	return fopen(fault_path, "w");
}

static void write_fault(void)
{
	FILE *file = open_fault_file();
	if (!file) {
		perror("fopen fault");
		_exit(20);
	}
	fputs("ingest.force_missing_process\n", file);
	fclose(file);
}

static int child_main(void)
{
	write_fault();

	pid_t child = fork();
	if (child < 0) {
		perror("inner fork");
		return 21;
	}
	if (child == 0) {
		_exit(0);
	}

	const time_t deadline = time(NULL) + 8;
	for (;;) {
		int status = 0;
		pid_t got = waitpid(child, &status, WNOHANG);
		if (got == child) {
			break;
		}
		if (got < 0) {
			perror("inner waitpid");
			return 22;
		}
		if (time(NULL) >= deadline) {
			kill(child, SIGKILL);
			waitpid(child, NULL, 0);
			fprintf(stderr, "inner child wedged after injected missing-process ingest fault\n");
			return 23;
		}
		usleep(100000);
	}

	unlink(fault_path);
	puts("INGEST_FORCE_MISSING_PROCESS_CHILD_OK");
	return 0;
}

int main(void)
{
	pid_t child = fork();
	if (child < 0) {
		perror("fork");
		return 1;
	}
	if (child == 0) {
		_exit(child_main());
	}

	const time_t deadline = time(NULL) + 12;
	for (;;) {
		int status = 0;
		pid_t got = waitpid(child, &status, WNOHANG);
		if (got == child) {
			if (WIFEXITED(status) && WEXITSTATUS(status) == 0) {
				puts("INGEST_FORCE_MISSING_PROCESS_OK");
				return 0;
			}
			fprintf(stderr, "child failed: status=0x%x\n", status);
			return 2;
		}
		if (got < 0) {
			perror("waitpid");
			return 3;
		}
		if (time(NULL) >= deadline) {
			kill(child, SIGKILL);
			waitpid(child, NULL, 0);
			fprintf(stderr, "child wedged after injected missing-process ingest fault\n");
			return 4;
		}
		usleep(100000);
	}
}
