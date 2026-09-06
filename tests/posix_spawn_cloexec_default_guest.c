#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <spawn.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

extern char **environ;

static int
spawn_child(const char *name, const char *self, short flags, char *const argv[])
{
	posix_spawnattr_t attr;
	posix_spawn_file_actions_t actions;
	int error = posix_spawnattr_init(&attr);
	if (error) return 1;
	error = posix_spawn_file_actions_init(&actions);
	if (error) { posix_spawnattr_destroy(&attr); return 1; }
	error = posix_spawnattr_setflags(&attr, flags);
	for (int fd = 0; !error && fd < 3; ++fd)
		error = posix_spawn_file_actions_addinherit_np(&actions, fd);
	pid_t pid = -1;
	if (!error)
		error = posix_spawn(&pid, self, &actions, &attr, argv, environ);
	posix_spawn_file_actions_destroy(&actions);
	posix_spawnattr_destroy(&attr);
	if (error) {
		fprintf(stderr, "%s: posix_spawn returned %d\n", name, error);
		return 1;
	}
	int status = 0;
	pid_t waited;
	do { waited = waitpid(pid, &status, 0); } while (waited < 0 && errno == EINTR);
	if (waited != pid || !WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		fprintf(stderr, "%s: child=%ld waited=%ld status=%d\n",
			name, (long)pid, (long)waited, status);
		return 1;
	}
	return 0;
}

static int
descriptor_boundary(const char *self)
{
	struct stat legacy, ordinary;
	int owned = 0;
	if (fstat(1023, &legacy) != 0) {
		if (errno != EBADF) return 1;
		int fd = open("/dev/null", O_RDONLY);
		if (fd < 0) return 1;
		if (fd != 1023) {
			int result = dup2(fd, 1023);
			close(fd);
			if (result != 1023) return 1;
		}
		owned = 1;
		if (fstat(1023, &legacy) != 0) { close(1023); return 1; }
	}
	char path[] = "/private/var/tmp/spawn-cloexec-XXXXXX";
	int fd = mkstemp(path);
	if (fd < 0) { if (owned) close(1023); return 1; }
	if (unlink(path) != 0) {
		close(fd);
		if (owned) close(1023);
		return 1;
	}
	int ordinary_fd = fcntl(fd, F_DUPFD, 64);
	close(fd);
	if (ordinary_fd < 0 || fstat(ordinary_fd, &ordinary) != 0) {
		if (ordinary_fd >= 0) close(ordinary_fd);
		if (owned) close(1023);
		return 1;
	}
	char legacy_dev[32], legacy_ino[32], fd_text[16], ordinary_dev[32], ordinary_ino[32];
	snprintf(legacy_dev, sizeof(legacy_dev), "%ju", (uintmax_t)legacy.st_dev);
	snprintf(legacy_ino, sizeof(legacy_ino), "%ju", (uintmax_t)legacy.st_ino);
	snprintf(fd_text, sizeof(fd_text), "%d", ordinary_fd);
	snprintf(ordinary_dev, sizeof(ordinary_dev), "%ju", (uintmax_t)ordinary.st_dev);
	snprintf(ordinary_ino, sizeof(ordinary_ino), "%ju", (uintmax_t)ordinary.st_ino);
	char *argv[] = {(char *)self, "--descriptors", legacy_dev, legacy_ino,
		fd_text, ordinary_dev, ordinary_ino, NULL};
	int failed = spawn_child("descriptor boundary", self, POSIX_SPAWN_CLOEXEC_DEFAULT, argv);
	close(ordinary_fd);
	if (owned) close(1023);
	return failed;
}

int
main(int argc, char **argv)
{
	if (argc == 2 && strcmp(argv[1], "--child") == 0) return 0;
	if (argc == 7 && strcmp(argv[1], "--descriptors") == 0) {
		struct stat actual;
		if (fstat(1023, &actual) != 0 ||
			(uintmax_t)actual.st_dev != strtoumax(argv[2], NULL, 10) ||
			(uintmax_t)actual.st_ino != strtoumax(argv[3], NULL, 10)) {
			fputs("Legacy FD 1023 did not retain its object identity\n", stderr);
			return 1;
		}
		int result = fstat(atoi(argv[4]), &actual);
		if ((result == 0 &&
			(uintmax_t)actual.st_dev == strtoumax(argv[5], NULL, 10) &&
			(uintmax_t)actual.st_ino == strtoumax(argv[6], NULL, 10)) ||
			(result != 0 && errno != EBADF)) {
			fputs("Ordinary descriptor was not closed on exec\n", stderr);
			return 1;
		}
		puts("SPAWN_DESCRIPTOR_BOUNDARY_OK");
		return 0;
	}
	char *child_argv[] = {argv[0], "--child", NULL};
	if (spawn_child("plain spawn", argv[0], 0, child_argv) ||
		spawn_child("CLOEXEC_DEFAULT spawn", argv[0], POSIX_SPAWN_CLOEXEC_DEFAULT, child_argv) ||
		descriptor_boundary(argv[0])) return 1;
	puts("POSIX_SPAWN_CLOEXEC_DEFAULT_GUEST_OK");
	return 0;
}
