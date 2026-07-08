#include <signal.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
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

int main(void)
{
	uid_t uid = getuid();

	write_fault();

	int rc = setuid(uid);
	unlink(fault_path);

	if (rc == 0) {
		fprintf(stderr, "expected injected setuid RPC to fail\n");
		return 2;
	}

	puts("INGEST_FORCE_MISSING_PROCESS_OK");
	return 0;
}
