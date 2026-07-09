#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/resource.h>
#include <sys/wait.h>
#include <unistd.h>

enum {
	INTERNAL_FD_TOP = 4096,
	INTERNAL_FD_BAND = 512,
	INTERNAL_FD_LOW = INTERNAL_FD_TOP - INTERNAL_FD_BAND,
};

static int
scan_proc_fds(int* saw_internal_band, int* saw_above_top)
{
	DIR* d = opendir("/proc/self/fd");
	if (!d) {
		printf("opendir /proc/self/fd failed: %s\n", strerror(errno));
		return 1;
	}

	for (;;) {
		errno = 0;
		struct dirent* ent = readdir(d);
		if (!ent)
			break;
		char* end = NULL;
		long fd = strtol(ent->d_name, &end, 10);
		if (end == ent->d_name || *end != '\0' || fd < 0 || fd > INT_MAX)
			continue;
		if (fd >= INTERNAL_FD_LOW && fd < INTERNAL_FD_TOP)
			*saw_internal_band = 1;
		if (fd >= INTERNAL_FD_TOP)
			*saw_above_top = 1;
	}
	if (errno != 0) {
		printf("readdir /proc/self/fd failed: %s\n", strerror(errno));
		closedir(d);
		return 1;
	}
	closedir(d);
	return 0;
}

static int
check_guest_opens_stay_below_band(void)
{
	int fds[128];
	for (size_t i = 0; i < sizeof(fds) / sizeof(fds[0]); i++)
		fds[i] = -1;

	for (size_t i = 0; i < sizeof(fds) / sizeof(fds[0]); i++) {
		fds[i] = open("/dev/null", O_RDONLY);
		if (fds[i] < 0) {
			printf("open /dev/null[%zu] failed: %s\n", i, strerror(errno));
			goto fail;
		}
		if (fds[i] >= INTERNAL_FD_LOW) {
			printf("guest open entered internal fd band: fd=%d low=%d\n", fds[i], INTERNAL_FD_LOW);
			goto fail;
		}
	}

	for (size_t i = 0; i < sizeof(fds) / sizeof(fds[0]); i++)
		close(fds[i]);
	return 0;

fail:
	for (size_t i = 0; i < sizeof(fds) / sizeof(fds[0]); i++) {
		if (fds[i] >= 0)
			close(fds[i]);
	}
	return 1;
}

static int
check_fork_exec(void)
{
	pid_t pid = fork();
	if (pid < 0) {
		printf("fork failed: %s\n", strerror(errno));
		return 1;
	}
	if (pid == 0) {
		execl("/usr/bin/true", "true", NULL);
		_exit(127);
	}

	int status = 0;
	if (waitpid(pid, &status, 0) != pid) {
		printf("waitpid failed: %s\n", strerror(errno));
		return 1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		printf("child true failed: status=%d\n", status);
		return 1;
	}
	return 0;
}

int
main(void)
{
	struct rlimit rl;
	if (getrlimit(RLIMIT_NOFILE, &rl) != 0) {
		printf("getrlimit RLIMIT_NOFILE failed: %s\n", strerror(errno));
		return 1;
	}
	if (rl.rlim_cur != INTERNAL_FD_TOP - 1) {
		printf("unexpected guest NOFILE cur=%llu expected=%d\n",
		       (unsigned long long)rl.rlim_cur, INTERNAL_FD_TOP - 1);
		return 1;
	}

	int saw_internal_band = 0;
	int saw_above_top = 0;
	if (scan_proc_fds(&saw_internal_band, &saw_above_top) != 0)
		return 1;
	if (!saw_internal_band) {
		printf("no internal fd observed in compact band [%d,%d)\n", INTERNAL_FD_LOW, INTERNAL_FD_TOP);
		return 1;
	}
	if (saw_above_top) {
		printf("observed fd at or above compact top %d\n", INTERNAL_FD_TOP);
		return 1;
	}

	if (check_guest_opens_stay_below_band() != 0)
		return 1;
	if (check_fork_exec() != 0)
		return 1;

	puts("WEST_MLDR_COMPACT_FD_BAND_OK");
	return 0;
}
