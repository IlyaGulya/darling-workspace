#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static const char *fallback_fault_path = "/private/var/tmp/dserver-processcall-fault";

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

	return fopen(fallback_fault_path, "w");
}

static void write_fault(void)
{
	FILE *file = open_fault_file();
	if (!file) {
		perror("fopen fault");
		_exit(20);
	}
	fputs("processcall.uidgid_throw\n", file);
	fclose(file);
}

int main(void)
{
	uid_t uid = getuid();

	write_fault();

	errno = 0;
	int rc = setuid(uid);
	if (rc == 0) {
		fprintf(stderr, "expected injected uidgid RPC to fail\n");
		return 2;
	}

	rc = setuid(uid);
	if (rc != 0) {
		perror("setuid after injected processCall failure");
		return 3;
	}

	unlink(fallback_fault_path);
	puts("PROCESSCALL_UIDGID_FAULT_OK");
	return 0;
}
